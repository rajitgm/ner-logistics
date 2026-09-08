"""Apply the Phase 1 reference data in ``app.db.seed_data`` to the database.

Run it as often as you like: every group is upserted on a natural key, so a
second run reports zero changes. That property is what makes this script safe to
put in ``make bootstrap`` and in a container entrypoint, and it is why the
seeder — not a migration — owns reference data. Migrations describe *shape*;
this describes *content*, and content gets corrected between releases.

Two things are refreshed unconditionally on every run rather than only on
insert: ``roles.permissions_snapshot`` and ``roles.scope``. The permission
matrix in ``app.core.rbac`` is authoritative for authorisation, and the row is
only a mirror for display, so a stale mirror would be a UI that promises access
the API will refuse. Rewriting it on every run means the mirror cannot drift
behind a code change.

What the seeder deliberately does not do:

* **Invent geometry.** States and districts are inserted as attribute rows with
  ``geom`` null and labelled ``REAL`` with a source string that says boundaries
  are pending. Phase 2 loads published boundary datasets. A plausible-looking
  polygon here would be indistinguishable from a real one later.
* **Overwrite an existing password.** If the bootstrap administrator already
  exists the seeder leaves the credential alone and only re-asserts the role
  binding, so re-running it after an operator rotated the password does not
  silently reset it.
* **Generate a password in production.** A generated secret has to be printed to
  be usable, and in a deployed environment that means a secret in a log
  aggregator. There, ``BOOTSTRAP_ADMIN_PASSWORD`` must be supplied explicitly.

Usage::

    python scripts/seed.py                  # apply everything
    python scripts/seed.py --dry-run        # report changes, commit nothing
    python scripts/seed.py --only roles     # one group
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Allows ``python scripts/seed.py`` from the service root without installing the
# package first, which is what the Makefile and the container entrypoint do.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.core import rbac  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.enums import AuditAction, DataProvenance, UserRole  # noqa: E402
from app.core.security import (  # noqa: E402
    PasswordPolicyError,
    generate_password,
    hash_password,
)
from app.db.seed_data import (  # noqa: E402
    COMMODITIES,
    DISTRICT_SOURCE,
    DISTRICTS,
    NER_STATES,
    RISK_FACTOR_WEIGHTS,
    RISK_WEIGHT_SET_VERSION,
    ROLE_PROFILES,
    ROUTING_WEIGHT_PROFILES,
    STATE_SOURCE,
)
from app.db.session import get_sync_session  # noqa: E402
from app.models.geography import District, State  # noqa: E402
from app.models.shipment import Commodity  # noqa: E402
from app.models.system import (  # noqa: E402
    AuditLog,
    RiskFactorWeight,
    RoutingWeightProfile,
)
from app.models.user import Role, User  # noqa: E402

GROUPS: Tuple[str, ...] = (
    "roles",
    "commodities",
    "routing_weights",
    "risk_weights",
    "states",
    "districts",
    "admin",
)


@dataclass
class Tally:
    """Per-group outcome, printed as the run summary."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    #: ``table.column`` names that were rewritten, for the --verbose listing.
    changes: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.created + self.updated + self.unchanged

    def __str__(self) -> str:
        return (
            f"{self.total:>4} rows  "
            f"created={self.created:<4} updated={self.updated:<4} "
            f"unchanged={self.unchanged}"
        )


def _differs(current: Any, wanted: Any) -> bool:
    """Whether ``current`` needs to be rewritten to become ``wanted``.

    Floats get a tolerance because a weight round-tripped through ``numeric`` or
    ``double precision`` can come back as 0.30000000000000004, and reporting
    that as a change every run would make the "no changes on re-run" guarantee
    useless as a signal.
    """

    if isinstance(current, float) and isinstance(wanted, (int, float)):
        return abs(current - float(wanted)) > 1e-9
    # Enum columns come back as enum members; comparing to a member or its value
    # both work because the project's enums subclass ``str``.
    return current != wanted


def _upsert(
    session: Session,
    model: type,
    *,
    match: Dict[str, Any],
    values: Dict[str, Any],
    tally: Tally,
    label: str,
    immutable: Sequence[str] = (),
) -> Any:
    """Insert ``model(**match, **values)`` or update the row matching ``match``.

    ``immutable`` names fields that are only applied on insert — used for the
    bootstrap password hash, which an operator may legitimately have rotated
    since the last run.
    """

    row = session.execute(sa.select(model).filter_by(**match)).scalar_one_or_none()
    if row is None:
        row = model(**match, **values)
        session.add(row)
        tally.created += 1
        return row

    rewritten: List[str] = []
    for key, wanted in values.items():
        if key in immutable:
            continue
        if _differs(getattr(row, key), wanted):
            setattr(row, key, wanted)
            rewritten.append(key)
    if rewritten:
        tally.updated += 1
        tally.changes.append(f"{label}: {', '.join(sorted(rewritten))}")
    else:
        tally.unchanged += 1
    return row


# ------------------------------------------------------------------- preflight


def _assert_schema_present(session: Session) -> str:
    """Fail loudly if migrations have not been applied.

    Without this the first failure is a confusing ``UndefinedTable`` from deep
    inside the ORM. The Alembic revision is returned so the summary can state
    which schema version the data was written against.
    """

    inspector = sa.inspect(session.get_bind())
    tables = set(inspector.get_table_names())
    missing = sorted({"alembic_version", "roles", "users", "states"} - tables)
    if missing:
        raise SystemExit(
            "database schema is not ready — missing table(s): "
            f"{', '.join(missing)}.\nRun `alembic upgrade head` first."
        )
    revision = session.execute(
        sa.text("SELECT version_num FROM alembic_version")
    ).scalar_one_or_none()
    if revision is None:
        raise SystemExit(
            "alembic_version is empty — the schema was created outside Alembic. "
            "Run `alembic upgrade head` against a clean database."
        )
    return str(revision)


# ---------------------------------------------------------------------- groups


def seed_roles(session: Session, tally: Tally) -> Dict[UserRole, Role]:
    """The six roles, with the permission mirror refreshed from code."""

    rows: Dict[UserRole, Role] = {}
    for spec in ROLE_PROFILES:
        role: UserRole = spec["role"]
        rows[role] = _upsert(
            session,
            Role,
            match={"name": role},
            values={
                "display_name": spec["display_name"],
                "description": spec["description"],
                # Mirror only. Authorisation always reads rbac.ROLE_PERMISSIONS.
                "permissions_snapshot": rbac.describe_role(role),
                "scope": rbac.scope_for(role).name,
            },
            tally=tally,
            label=f"role {role}",
        )
    session.flush()
    return rows


def seed_commodities(session: Session, tally: Tally) -> None:
    """Default commodity priorities — configuration, not measurement.

    The spec's priorities are prototype defaults that an administrator can
    change, so the seeder writes them once and then keeps them in step with the
    file; an operator edit made through the API is overwritten on the next run
    only if it disagrees with the checked-in default, which is the trade-off
    that keeps "the defaults are in version control" true.
    """

    for spec in COMMODITIES:
        values = {k: v for k, v in spec.items() if k != "name"}
        _upsert(
            session,
            Commodity,
            match={"name": spec["name"]},
            values=values,
            tally=tally,
            label=f"commodity {spec['name']}",
        )


def seed_routing_weights(session: Session, tally: Tally) -> None:
    """Routing weight profiles, keyed by (profile, version).

    Versioned rather than mutated: a route recommendation records which profile
    version scored it, so an old decision stays explainable after the policy
    changes. A new balance of weights is therefore a new row, not an edit.
    """

    for spec in ROUTING_WEIGHT_PROFILES:
        values = {k: v for k, v in spec.items() if k not in {"profile", "version"}}
        values["is_active"] = True
        _upsert(
            session,
            RoutingWeightProfile,
            match={"profile": spec["profile"], "version": spec["version"]},
            values=values,
            tally=tally,
            label=f"weights {spec['profile']} v{spec['version']}",
        )


def seed_risk_weights(session: Session, tally: Tally) -> None:
    """Per-factor risk ceilings for weight set ``baseline-v1``.

    The ceilings intentionally sum to more than 100; the engine clamps the total
    to 0-100. Storing them uncapped keeps each factor's ceiling meaningful on
    its own instead of being an artefact of how many factors exist.
    """

    for spec in RISK_FACTOR_WEIGHTS:
        values = {k: v for k, v in spec.items() if k != "factor"}
        values["is_active"] = True
        _upsert(
            session,
            RiskFactorWeight,
            match={
                "factor": spec["factor"],
                "weight_set_version": RISK_WEIGHT_SET_VERSION,
            },
            values=values,
            tally=tally,
            label=f"risk factor {spec['factor']}",
        )


def seed_states(session: Session, tally: Tally) -> Dict[str, State]:
    """The eight NER states, geometry left null.

    ``provenance=REAL`` is accurate: these are published administrative facts.
    ``source`` says boundaries are pending so nothing downstream can mistake a
    null ``geom`` for a load failure.
    """

    rows: Dict[str, State] = {}
    for spec in NER_STATES:
        rows[spec["iso_code"]] = _upsert(
            session,
            State,
            match={"iso_code": spec["iso_code"]},
            values={
                "name": spec["name"],
                "census_code": spec["census_code"],
                "capital": spec["capital"],
                "is_ner": True,
                "provenance": DataProvenance.REAL,
                "source": STATE_SOURCE,
            },
            tally=tally,
            label=f"state {spec['iso_code']}",
        )
    session.flush()
    return rows


def seed_districts(
    session: Session, states: Dict[str, State], tally: Tally
) -> None:
    """Districts per state, matched on (state_id, name).

    Only the name and headquarters are asserted. ``population`` and
    ``is_remote`` are left at their defaults rather than filled with plausible
    numbers — ``is_remote`` in particular changes the routing reliability
    penalty, so guessing it would quietly change recommendations.
    """

    for iso_code, districts in DISTRICTS.items():
        state = states.get(iso_code)
        if state is None:
            raise SystemExit(
                f"DISTRICTS references unknown state {iso_code!r}; "
                "seed_data.NER_STATES and DISTRICTS have diverged."
            )
        for name, headquarters in districts:
            _upsert(
                session,
                District,
                match={"state_id": state.id, "name": name},
                values={
                    "headquarters": headquarters,
                    "provenance": DataProvenance.REAL,
                    "source": DISTRICT_SOURCE,
                },
                tally=tally,
                label=f"district {iso_code}/{name}",
            )


def _available_username(session: Session, email: str) -> str:
    """A free ``users.username`` derived from the bootstrap email.

    The column is unique and 64 characters wide, so the local part is truncated
    and, if some earlier account already took it, a numeric suffix is appended.
    Failing here instead would turn a name clash into an ``IntegrityError`` from
    the flush, which reads like a bug rather than a naming collision.
    """

    base = (email.split("@")[0] or "admin")[:56]
    candidate = base
    for suffix in range(1, 100):
        taken = session.execute(
            sa.select(User.id).filter_by(username=candidate)
        ).scalar_one_or_none()
        if taken is None:
            return candidate
        candidate = f"{base}{suffix}"
    raise SystemExit(f"cannot derive a free username from {email!r}")


def seed_bootstrap_admin(
    session: Session,
    roles: Dict[UserRole, Role],
    tally: Tally,
) -> Tuple[Optional[User], Optional[str]]:
    """Create or repair the single account needed to log in the first time.

    Returns the user and, when one was generated, the plaintext password so the
    caller can print it exactly once. An existing account keeps its credential:
    the seeder only guarantees that the account is active and holds ADMIN.
    """

    settings = get_settings()
    email = (settings.BOOTSTRAP_ADMIN_EMAIL or "").strip().lower()
    if not email:
        raise SystemExit(
            "BOOTSTRAP_ADMIN_EMAIL is not set; cannot create the first "
            "administrator. Set it in .env and re-run."
        )

    admin_role = roles.get(UserRole.ADMIN)
    if admin_role is None:
        admin_role = session.execute(
            sa.select(Role).filter_by(name=UserRole.ADMIN)
        ).scalar_one()

    existing = session.execute(
        sa.select(User).filter_by(email=email)
    ).scalar_one_or_none()

    generated: Optional[str] = None
    if existing is None:
        supplied = settings.BOOTSTRAP_ADMIN_PASSWORD
        if not supplied:
            if settings.ENV in {"production", "staging"}:
                raise SystemExit(
                    f"refusing to generate a password for {email} while "
                    f"ENV={settings.ENV}: it would have to be printed, and a "
                    "printed secret ends up in the deployment log. Set "
                    "BOOTSTRAP_ADMIN_PASSWORD explicitly."
                )
            generated = generate_password()
            supplied = generated
        try:
            hashed = hash_password(supplied)
        except PasswordPolicyError as exc:
            raise SystemExit(f"BOOTSTRAP_ADMIN_PASSWORD rejected: {exc}") from exc
        user = User(
            email=email,
            username=_available_username(session, email),
            hashed_password=hashed,
            full_name="Platform Administrator",
            designation="System Administrator",
            is_active=True,
            is_superuser=True,
            token_version=1,
        )
        session.add(user)
        tally.created += 1
    else:
        user = existing
        rewritten = []
        if not user.is_active:
            user.is_active = True
            rewritten.append("is_active")
        if not user.is_superuser:
            user.is_superuser = True
            rewritten.append("is_superuser")
        if user.locked_until is not None:
            user.locked_until = None
            rewritten.append("locked_until")
        if rewritten:
            tally.updated += 1
            tally.changes.append(f"admin {email}: {', '.join(rewritten)}")
        else:
            tally.unchanged += 1

    if admin_role not in user.roles:
        user.roles.append(admin_role)
        tally.changes.append(f"admin {email}: granted {UserRole.ADMIN}")
    session.flush()
    return user, generated


def record_audit(
    session: Session,
    actor: Optional[User],
    revision: str,
    report: Dict[str, Tally],
) -> None:
    """One CONFIG_CHANGE row per run that actually changed something.

    Reference data is policy — priorities and risk ceilings steer routing
    decisions — so a change to it belongs in the same audit trail as an operator
    editing it through the API. Runs that changed nothing are not logged, so the
    trail stays a record of changes rather than of invocations.
    """

    changed = {
        group: {"created": t.created, "updated": t.updated}
        for group, t in report.items()
        if t.created or t.updated
    }
    if not changed:
        return
    session.add(
        AuditLog(
            # ``occurred_at`` has no server default on purpose: the audit trail
            # records when the action happened, not when the row was written.
            occurred_at=datetime.now(timezone.utc),
            action=AuditAction.CONFIG_CHANGE,
            actor_id=actor.id if actor is not None else None,
            actor_label="scripts/seed.py",
            actor_roles=[str(UserRole.ADMIN)],
            entity_type="reference_data",
            entity_label=f"seed_data @ alembic {revision}",
            new_value=changed,
            reason="Reference data applied by the seeder.",
            succeeded=True,
            metadata_json={
                "risk_weight_set_version": RISK_WEIGHT_SET_VERSION,
                "alembic_revision": revision,
            },
        )
    )


# ------------------------------------------------------------------ entrypoint


def run(session: Session, groups: Sequence[str], dry_run: bool) -> Dict[str, Tally]:
    """Apply the selected groups in dependency order.

    Roles precede the administrator and states precede districts; within a group
    order does not matter. Everything happens in one transaction, so a failure
    half way through leaves the database as it was.
    """

    revision = _assert_schema_present(session)
    report: Dict[str, Tally] = {g: Tally() for g in groups}
    roles: Dict[UserRole, Role] = {}
    states: Dict[str, State] = {}
    admin: Optional[User] = None
    generated: Optional[str] = None

    if "roles" in report:
        roles = seed_roles(session, report["roles"])
    if "commodities" in report:
        seed_commodities(session, report["commodities"])
    if "routing_weights" in report:
        seed_routing_weights(session, report["routing_weights"])
    if "risk_weights" in report:
        seed_risk_weights(session, report["risk_weights"])
    if "states" in report:
        states = seed_states(session, report["states"])
    if "districts" in report:
        if not states:
            states = {
                s.iso_code: s
                for s in session.execute(sa.select(State)).scalars().all()
            }
        seed_districts(session, states, report["districts"])
    if "admin" in report:
        admin, generated = seed_bootstrap_admin(session, roles, report["admin"])

    record_audit(session, admin, revision, report)

    if dry_run:
        session.rollback()
        print("\n-- dry run: transaction rolled back, nothing was written --")
    else:
        session.commit()

    print(f"\nschema revision: {revision}")
    if generated is not None and not dry_run:
        _print_generated_password(get_settings().BOOTSTRAP_ADMIN_EMAIL, generated)
    return report


def _print_generated_password(email: Optional[str], password: str) -> None:
    """Show a generated bootstrap password once, with the caveats attached."""

    bar = "=" * 68
    print(
        f"\n{bar}\n"
        "  BOOTSTRAP ADMINISTRATOR CREATED — this password is shown once\n"
        f"{bar}\n"
        f"  email    : {email}\n"
        f"  password : {password}\n"
        f"{bar}\n"
        "  It was generated because BOOTSTRAP_ADMIN_PASSWORD was unset. Only\n"
        "  the bcrypt hash is stored, so it cannot be recovered — sign in and\n"
        "  change it, and do not leave it in your shell history.\n"
        f"{bar}"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Apply Phase 1 reference data. Safe to run repeatedly."
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=GROUPS,
        metavar="GROUP",
        help=f"seed one group only; repeatable. one of: {', '.join(GROUPS)}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change, then roll back",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every field that was rewritten",
    )
    args = parser.parse_args(argv)

    groups = tuple(g for g in GROUPS if not args.only or g in args.only)
    settings = get_settings()
    print(f"seeding {settings.PROJECT_NAME} [ENV={settings.ENV}]")
    print(f"groups: {', '.join(groups)}")

    session = get_sync_session()
    try:
        report = run(session, groups, args.dry_run)
    finally:
        session.close()

    print("\nsummary")
    for group in groups:
        print(f"  {group:<16} {report[group]}")
    if args.verbose:
        details = [line for t in report.values() for line in t.changes]
        if details:
            print("\nchanges")
            for line in details:
                print(f"  {line}")
    total_changed = sum(t.created + t.updated for t in report.values())
    print(
        f"\n{'would change' if args.dry_run else 'changed'} {total_changed} row(s); "
        f"{sum(t.unchanged for t in report.values())} already current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

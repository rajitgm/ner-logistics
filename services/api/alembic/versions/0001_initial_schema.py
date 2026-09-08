"""Initial schema: PostGIS, native enum types, and all 30 tables.

One revision rather than several because there is no prior state to migrate from
— this is the schema the platform starts life with. It is a frozen snapshot, not
a projection of the current models: replaying it on an empty database must always
produce the schema as of this revision, even after later phases add columns.
A test asserts that the tables and columns here still match ``app.models``, so
the two cannot silently diverge while this is still the only revision.

Order of operations matters. Extensions first, because ``gen_random_uuid()`` is a
pgcrypto function and every primary key defaults to it. Then the enum types,
created once each and referenced with ``create_type=False`` thereafter, since a
type shared by several tables would otherwise be created repeatedly and fail.
Then tables in dependency order, with the single ``shipments`` to ``routes``
cycle closed by an ``ALTER TABLE`` afterwards. Indexes last, including the GIST
indexes for every geometry column — created explicitly here rather than by
GeoAlchemy2's listeners, so the DDL is visible in the migration and downgrade can
drop them by name.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Frozen enum values as of this revision. Adding a member later is a new
#: migration with ``ALTER TYPE ... ADD VALUE``, never an edit to this tuple.
ENUM_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("alert_severity", ("INFO", "WARNING", "CRITICAL", "EMERGENCY")),
    (
        "alert_type",
        (
            "HIGH_RISK_ROAD",
            "ROAD_CLOSURE",
            "PREDICTED_DISRUPTION",
            "SHIPMENT_DELAY",
            "ROUTE_CHANGE",
            "WEATHER_WARNING",
            "FIELD_INCIDENT",
            "EMERGENCY_REROUTE",
        ),
    ),
    (
        "audit_action",
        (
            "LOGIN_SUCCESS",
            "LOGIN_FAILURE",
            "LOGOUT",
            "TOKEN_REFRESH",
            "ACCESS_DENIED",
            "ROAD_STATUS_CHANGE",
            "INCIDENT_VERIFICATION",
            "ROUTE_OVERRIDE",
            "SHIPMENT_MODIFICATION",
            "ALERT_CREATION",
            "USER_PERMISSION_CHANGE",
            "CONFIG_CHANGE",
            "MODEL_PROMOTION",
            "SIMULATION_RUN",
            "AI_TOOL_CALL",
            "ENTITY_CREATE",
            "ENTITY_UPDATE",
            "ENTITY_DELETE",
        ),
    ),
    ("data_provenance", ("REAL", "SYNTHETIC", "SIMULATED", "PREDICTED", "DERIVED")),
    (
        "hazard_type",
        (
            "FLOOD_PLAIN",
            "LANDSLIDE_SUSCEPTIBLE",
            "SEISMIC",
            "EROSION",
            "HIGH_RAINFALL",
        ),
    ),
    ("incident_severity", ("LOW", "MODERATE", "HIGH", "SEVERE")),
    (
        "incident_type",
        (
            "LANDSLIDE",
            "FLOOD",
            "ROAD_BLOCK",
            "BRIDGE_DAMAGE",
            "ACCIDENT",
            "DEBRIS",
            "HEAVY_RAIN",
            "TRAFFIC",
            "ROAD_DAMAGE",
            "OTHER",
        ),
    ),
    ("model_stage", ("TRAINING", "VALIDATED", "SHADOW", "ACTIVE", "RETIRED")),
    ("notification_channel", ("IN_APP", "WEBSOCKET", "EMAIL", "SMS", "PUSH")),
    ("notification_status", ("QUEUED", "SENT", "DELIVERED", "READ", "FAILED")),
    (
        # Lowercase on purpose: these are the JSON keys of a risk breakdown, and
        # the API returns them verbatim.
        "risk_factor",
        (
            "weather",
            "rainfall",
            "flood",
            "landslide",
            "terrain",
            "road_condition",
            "historical_incident",
            "current_incident",
            "traffic",
            "vehicle_compatibility",
        ),
    ),
    ("risk_level", ("LOW", "MODERATE", "HIGH", "CRITICAL")),
    ("road_status", ("OPEN", "CAUTION", "HIGH_RISK", "BLOCKED", "UNKNOWN")),
    (
        "road_type",
        (
            "NATIONAL_HIGHWAY",
            "STATE_HIGHWAY",
            "DISTRICT_ROAD",
            "RURAL_ROAD",
            "URBAN_ROAD",
            "TRACK",
            "BRIDGE",
            "TUNNEL",
            "OTHER",
        ),
    ),
    (
        "route_event_type",
        (
            "CREATED",
            "SELECTED",
            "RISK_INCREASED",
            "RISK_DECREASED",
            "SEGMENT_BLOCKED",
            "REROUTED",
            "ETA_REVISED",
            "MANUAL_OVERRIDE",
            "COMPLETED",
        ),
    ),
    ("route_status", ("PROPOSED", "ACTIVE", "SUPERSEDED", "COMPLETED", "ABANDONED")),
    ("shipment_priority_profile", ("NORMAL", "EMERGENCY", "BULK_ECONOMY")),
    (
        "shipment_status",
        ("PLANNED", "IN_TRANSIT", "DELAYED", "REROUTING", "DELIVERED", "CANCELLED"),
    ),
    (
        "surface_type",
        (
            "ASPHALT",
            "CONCRETE",
            "PAVED_OTHER",
            "GRAVEL",
            "EARTH",
            "UNPAVED_OTHER",
            "UNKNOWN",
        ),
    ),
    ("sync_status", ("PENDING", "SYNCING", "SYNCED", "FAILED")),
    ("terrain_class", ("PLAIN", "ROLLING", "HILLY", "MOUNTAINOUS", "STEEP")),
    (
        "user_role",
        (
            "ADMIN",
            "STATE_OFFICER",
            "DISTRICT_OFFICER",
            "FIELD_OFFICER",
            "DRIVER",
            "VIEWER",
        ),
    ),
    (
        "vehicle_operational_status",
        ("AVAILABLE", "ASSIGNED", "EN_ROUTE", "MAINTENANCE", "OUT_OF_SERVICE"),
    ),
    (
        "vehicle_type",
        (
            "LIGHT_VEHICLE",
            "TRUCK",
            "HEAVY_TRUCK",
            "AMBULANCE",
            "EMERGENCY_VEHICLE",
        ),
    ),
    ("verification_status", ("PENDING", "VERIFIED", "REJECTED")),
)

#: Tables in creation order; downgrade drops them in reverse.
TABLES: tuple[str, ...] = (
    "commodities",
    "hazard_zones",
    "roles",
    "states",
    "weather_alerts",
    "districts",
    "roads",
    "road_segments",
    "users",
    "weather_forecasts",
    "weather_observations",
    "audit_logs",
    "incidents",
    "model_versions",
    "risk_factor_weights",
    "routing_weight_profiles",
    "sync_queue",
    "user_roles",
    "vehicles",
    "field_reports",
    "risk_scores",
    "shipments",
    "incident_photos",
    "route_candidates",
    "vehicle_positions",
    "routes",
    "alerts",
    "eta_predictions",
    "route_events",
    "notifications",
)

def upgrade() -> None:
    _create_extensions()
    _create_enum_types()
    _create_tables()
    _close_shipment_route_cycle()
    _create_indexes()


def downgrade() -> None:
    _drop_indexes()
    op.drop_constraint("fk_shipments_active_route_id_routes", "shipments", type_="foreignkey")
    for table in reversed(TABLES):
        op.drop_table(table)
    bind = op.get_bind()
    for name, _values in ENUM_TYPES:
        postgresql.ENUM(name=name).drop(bind, checkfirst=True)
    # Extensions are intentionally not dropped: another database object may
    # depend on PostGIS, and dropping it is not this migration's business.


def _create_extensions() -> None:
    """PostGIS for geometry, pgcrypto for ``gen_random_uuid()``.

    ``IF NOT EXISTS`` because a managed Postgres instance often has PostGIS
    pre-installed, and because re-running against a partially built database
    should not fail on the first statement.
    """

    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def _create_enum_types() -> None:
    """Create every native enum type once.

    Several types are used by more than one table — ``risk_level`` by four. Left
    to SQLAlchemy's per-column DDL that becomes a duplicate ``CREATE TYPE``, so
    the types are created here and every column below references them with
    ``create_type=False``.
    """

    bind = op.get_bind()
    for name, values in ENUM_TYPES:
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)


def _create_tables() -> None:
    """Create all 30 tables in dependency order.

    The order is a topological sort of the foreign keys, which is why
    ``commodities`` and ``hazard_zones`` come first (nothing they reference) and
    ``notifications`` last. ``user_roles`` is an association table with a
    composite primary key rather than a mapped class, and appears here like any
    other. One edge is deliberately missing: ``shipments.active_route_id`` to
    ``routes.id``, which would make the graph cyclic — see
    ``_close_shipment_route_cycle``.

    Geometry columns are emitted with ``spatial_index=False``. GeoAlchemy2 would
    otherwise attach its own ``CREATE INDEX`` listener and every GIST index would
    be created twice, once invisibly.
    """

    op.create_table(
        "commodities",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("category", sa.String(60), nullable=False),
        sa.Column("default_priority", sa.SmallInteger, nullable=False),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("is_perishable", sa.Boolean, nullable=False),
        sa.Column("is_hazardous", sa.Boolean, nullable=False),
        sa.Column("requires_cold_chain", sa.Boolean, nullable=False),
        sa.Column("is_life_critical", sa.Boolean, nullable=False),
        sa.Column("max_acceptable_delay_hours", sa.Float, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.CheckConstraint("default_priority >= 1 AND default_priority <= 10", name="ck_commodities_default_priority_range"),
        sa.PrimaryKeyConstraint("id", name="pk_commodities"),
    )
    op.create_table(
        "hazard_zones",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("hazard_type", postgresql.ENUM(name="hazard_type", create_type=False), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("severity_index", sa.Float, nullable=False),
        sa.Column("return_period_years", sa.Integer, nullable=True),
        sa.Column("active_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("active_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dataset_name", sa.String(160), nullable=True),
        sa.Column("dataset_version", sa.String(40), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.Column("geom", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=False),
        sa.CheckConstraint("severity_index >= 0 AND severity_index <= 100", name="ck_hazard_zones_severity_index_range"),
        sa.PrimaryKeyConstraint("id", name="pk_hazard_zones"),
    )
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", postgresql.ENUM(name="user_role", create_type=False), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("permissions_snapshot", postgresql.JSONB, nullable=True),
        sa.Column("scope", sa.String(20), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_table(
        "states",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("iso_code", sa.String(10), nullable=False),
        sa.Column("census_code", sa.String(10), nullable=True),
        sa.Column("capital", sa.String(120), nullable=True),
        sa.Column("is_ner", sa.Boolean, nullable=False),
        sa.Column("geom", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_states"),
        sa.UniqueConstraint("name", name="uq_states_name"),
        sa.UniqueConstraint("iso_code", name="uq_states_iso_code"),
    )
    op.create_table(
        "weather_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("issuing_authority", sa.String(120), nullable=True),
        sa.Column("external_id", sa.String(120), nullable=True),
        sa.Column("alert_type", sa.String(60), nullable=False),
        sa.Column("severity", postgresql.ENUM(name="alert_severity", create_type=False), nullable=False),
        sa.Column("headline", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB, nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_weather_alerts"),
        sa.UniqueConstraint("issuing_authority", "external_id", name="uq_weather_alerts_authority_ext"),
    )
    op.create_table(
        "districts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("state_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("census_code", sa.String(10), nullable=True),
        sa.Column("headquarters", sa.String(120), nullable=True),
        sa.Column("population", sa.Integer, nullable=True),
        sa.Column("is_remote", sa.Boolean, nullable=False),
        sa.Column("geom", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("centroid", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.ForeignKeyConstraint(["state_id"], ["states.id"], name="fk_districts_state_id_states", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_districts"),
        sa.UniqueConstraint("state_id", "name", name="uq_districts_state_id_name"),
    )
    op.create_table(
        "roads",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("ref", sa.String(40), nullable=True),
        sa.Column("road_type", postgresql.ENUM(name="road_type", create_type=False), nullable=False),
        sa.Column("managing_authority", sa.String(120), nullable=True),
        sa.Column("state_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("total_length_km", sa.Float, nullable=True),
        sa.Column("osm_relation_id", sa.BigInteger, nullable=True),
        sa.Column("geom", Geometry(geometry_type="MULTILINESTRING", srid=4326, spatial_index=False), nullable=True),
        sa.ForeignKeyConstraint(["state_id"], ["states.id"], name="fk_roads_state_id_states", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_roads"),
    )
    op.create_table(
        "road_segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("road_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("osm_way_id", sa.BigInteger, nullable=True),
        sa.Column("state_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False), nullable=False),
        sa.Column("start_point", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("end_point", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("length_m", sa.Float, nullable=False),
        sa.Column("road_type", postgresql.ENUM(name="road_type", create_type=False), nullable=False),
        sa.Column("surface", postgresql.ENUM(name="surface_type", create_type=False), nullable=False),
        sa.Column("lanes", sa.SmallInteger, nullable=True),
        sa.Column("width_m", sa.Float, nullable=True),
        sa.Column("is_bridge", sa.Boolean, nullable=False),
        sa.Column("is_tunnel", sa.Boolean, nullable=False),
        sa.Column("is_oneway", sa.Boolean, nullable=False),
        sa.Column("free_flow_speed_kmph", sa.Float, nullable=False),
        sa.Column("condition_index", sa.Float, nullable=True),
        sa.Column("max_weight_tonnes", sa.Float, nullable=True),
        sa.Column("max_height_m", sa.Float, nullable=True),
        sa.Column("max_width_m", sa.Float, nullable=True),
        sa.Column("vehicle_restrictions", postgresql.JSONB, nullable=True),
        sa.Column("elevation_min_m", sa.Float, nullable=True),
        sa.Column("elevation_max_m", sa.Float, nullable=True),
        sa.Column("elevation_mean_m", sa.Float, nullable=True),
        sa.Column("slope_mean_deg", sa.Float, nullable=True),
        sa.Column("slope_max_deg", sa.Float, nullable=True),
        sa.Column("terrain_class", postgresql.ENUM(name="terrain_class", create_type=False), nullable=False),
        sa.Column("distance_to_water_m", sa.Float, nullable=True),
        sa.Column("status", postgresql.ENUM(name="road_status", create_type=False), nullable=False),
        sa.Column("official_closure", sa.Boolean, nullable=False),
        sa.Column("official_closure_reason", sa.Text, nullable=True),
        sa.Column("official_closure_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status_source", sa.String(120), nullable=True),
        sa.Column("status_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=False),
        sa.Column("risk_confidence", sa.Float, nullable=False),
        sa.Column("risk_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("risk_factors", postgresql.JSONB, nullable=True),
        sa.Column("weather_exposure", sa.Float, nullable=False),
        sa.Column("flood_exposure", sa.Float, nullable=False),
        sa.Column("landslide_exposure", sa.Float, nullable=False),
        sa.CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_road_segments_risk_score_range"),
        sa.CheckConstraint("risk_confidence >= 0 AND risk_confidence <= 1", name="ck_road_segments_risk_confidence_range"),
        sa.CheckConstraint("length_m > 0", name="ck_road_segments_length_positive"),
        sa.ForeignKeyConstraint(["road_id"], ["roads.id"], name="fk_road_segments_road_id_roads", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["state_id"], ["states.id"], name="fk_road_segments_state_id_states", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_road_segments_district_id_districts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_road_segments"),
    )
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("phone", sa.String(20), nullable=True),
        sa.Column("designation", sa.String(120), nullable=True),
        sa.Column("preferred_language", sa.String(10), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("is_superuser", sa.Boolean, nullable=False),
        sa.Column("state_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer, nullable=False),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer, nullable=False),
        sa.CheckConstraint("failed_login_count >= 0", name="ck_users_failed_login_count_non_negative"),
        sa.ForeignKeyConstraint(["state_id"], ["states.id"], name="fk_users_state_id_states", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_users_district_id_districts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
    )
    op.create_table(
        "weather_forecasts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("issued_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("valid_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("valid_to", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("horizon_hours", sa.Integer, nullable=False),
        sa.Column("model_name", sa.String(80), nullable=True),
        sa.Column("rainfall_mm", sa.Float, nullable=False),
        sa.Column("precipitation_probability", sa.Float, nullable=True),
        sa.Column("temperature_c", sa.Float, nullable=True),
        sa.Column("humidity_pct", sa.Float, nullable=True),
        sa.Column("wind_speed_kmph", sa.Float, nullable=True),
        sa.Column("condition_code", sa.String(40), nullable=True),
        sa.CheckConstraint("valid_to > valid_from", name="ck_weather_forecasts_valid_window_ordered"),
        sa.CheckConstraint("horizon_hours >= 0", name="ck_weather_forecasts_horizon_non_negative"),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_weather_forecasts_district_id_districts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_weather_forecasts"),
    )
    op.create_table(
        "weather_observations",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("station_id", sa.String(64), nullable=True),
        sa.Column("station_name", sa.String(160), nullable=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("temperature_c", sa.Float, nullable=True),
        sa.Column("humidity_pct", sa.Float, nullable=True),
        sa.Column("rainfall_mm", sa.Float, nullable=False),
        sa.Column("rainfall_24h_mm", sa.Float, nullable=True),
        sa.Column("wind_speed_kmph", sa.Float, nullable=True),
        sa.Column("wind_direction_deg", sa.Float, nullable=True),
        sa.Column("pressure_hpa", sa.Float, nullable=True),
        sa.Column("visibility_m", sa.Float, nullable=True),
        sa.Column("condition_code", sa.String(40), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("rainfall_mm >= 0", name="ck_weather_observations_rainfall_non_negative"),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_weather_observations_district_id_districts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_weather_observations"),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("action", postgresql.ENUM(name="audit_action", create_type=False), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_label", sa.String(160), nullable=True),
        sa.Column("actor_roles", postgresql.JSONB, nullable=True),
        sa.Column("entity_type", sa.String(80), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_label", sa.String(255), nullable=True),
        sa.Column("old_value", postgresql.JSONB, nullable=True),
        sa.Column("new_value", postgresql.JSONB, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("succeeded", sa.Boolean, nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("http_method", sa.String(10), nullable=True),
        sa.Column("http_path", sa.String(400), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("ai_tool_name", sa.String(80), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB, nullable=True),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_audit_logs_actor_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_audit_logs"),
    )
    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("incident_type", postgresql.ENUM(name="incident_type", create_type=False), nullable=False),
        sa.Column("severity", postgresql.ENUM(name="incident_severity", create_type=False), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("affected_area", Geometry(geometry_type="MULTIPOLYGON", srid=4326, spatial_index=False), nullable=True),
        sa.Column("road_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verification_status", postgresql.ENUM(name="verification_status", create_type=False), nullable=False),
        sa.Column("reported_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("verified_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("estimated_clearance_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("causes_closure", sa.Boolean, nullable=False),
        sa.Column("lanes_blocked", sa.SmallInteger, nullable=True),
        sa.Column("delay_minutes", sa.Float, nullable=True),
        sa.Column("speed_factor", sa.Float, nullable=True),
        sa.Column("is_simulated", sa.Boolean, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("resolved_at IS NULL OR resolved_at >= started_at", name="ck_incidents_resolution_after_start"),
        sa.ForeignKeyConstraint(["road_segment_id"], ["road_segments.id"], name="fk_incidents_road_segment_id_road_segments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_incidents_district_id_districts", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reported_by_id"], ["users.id"], name="fk_incidents_reported_by_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["verified_by_id"], ["users.id"], name="fk_incidents_verified_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_incidents"),
    )
    op.create_table(
        "model_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("task", sa.String(40), nullable=False),
        sa.Column("algorithm", sa.String(60), nullable=False),
        sa.Column("stage", postgresql.ENUM(name="model_stage", create_type=False), nullable=False),
        sa.Column("feature_names", postgresql.JSONB, nullable=False),
        sa.Column("hyperparameters", postgresql.JSONB, nullable=True),
        sa.Column("dataset_version", sa.String(80), nullable=False),
        sa.Column("training_rows", sa.Integer, nullable=True),
        sa.Column("validation_rows", sa.Integer, nullable=True),
        sa.Column("random_seed", sa.Integer, nullable=True),
        sa.Column("trained_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("training_duration_seconds", sa.Float, nullable=True),
        sa.Column("artifact_path", sa.String(500), nullable=True),
        sa.Column("artifact_sha256", sa.String(64), nullable=True),
        sa.Column("runtime_versions", postgresql.JSONB, nullable=True),
        sa.Column("metrics", postgresql.JSONB, nullable=True),
        sa.Column("training_data_provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("metrics_data_provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("evaluation_protocol", sa.Text, nullable=True),
        sa.Column("feature_importances", postgresql.JSONB, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("promoted_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("promoted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["promoted_by_id"], ["users.id"], name="fk_model_versions_promoted_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_model_versions"),
        sa.UniqueConstraint("name", "version", name="uq_model_versions_name_version"),
    )
    op.create_table(
        "risk_factor_weights",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("factor", postgresql.ENUM(name="risk_factor", create_type=False), nullable=False),
        sa.Column("weight_set_version", sa.String(40), nullable=False),
        sa.Column("max_contribution", sa.Float, nullable=False),
        sa.Column("saturation_value", sa.Float, nullable=True),
        sa.Column("response_curve", sa.String(20), nullable=False),
        sa.Column("threshold_value", sa.Float, nullable=True),
        sa.Column("missing_data_confidence_penalty", sa.Float, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("max_contribution >= 0 AND max_contribution <= 100", name="ck_risk_factor_weights_max_contribution_range"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], name="fk_risk_factor_weights_updated_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_risk_factor_weights"),
        sa.UniqueConstraint("factor", "weight_set_version", name="uq_risk_factor_weights_version"),
    )
    op.create_table(
        "routing_weight_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("profile", postgresql.ENUM(name="shipment_priority_profile", create_type=False), nullable=False),
        sa.Column("version", sa.SmallInteger, nullable=False),
        sa.Column("label", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("weight_reliability", sa.Float, nullable=False),
        sa.Column("weight_eta", sa.Float, nullable=False),
        sa.Column("weight_risk", sa.Float, nullable=False),
        sa.Column("weight_distance", sa.Float, nullable=False),
        sa.Column("weight_cost", sa.Float, nullable=False),
        sa.Column("max_acceptable_risk_score", sa.Float, nullable=True),
        sa.Column("allows_high_risk_segments", sa.Boolean, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("weight_reliability >= 0 AND weight_eta >= 0 AND weight_risk >= 0 AND weight_distance >= 0 AND weight_cost >= 0", name="ck_routing_weight_profiles_weights_non_negative"),
        sa.CheckConstraint("abs((weight_reliability + weight_eta + weight_risk + weight_distance + weight_cost) - 1.0) <= 0.0001", name="ck_routing_weight_profiles_weights_sum_to_one"),
        sa.ForeignKeyConstraint(["updated_by_id"], ["users.id"], name="fk_routing_weight_profiles_updated_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_routing_weight_profiles"),
        sa.UniqueConstraint("profile", "version", name="uq_weight_profiles_version"),
    )
    op.create_table(
        "sync_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("device_id", sa.String(80), nullable=False),
        sa.Column("client_mutation_id", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(80), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("client_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("status", postgresql.ENUM(name="sync_status", create_type=False), nullable=False),
        sa.Column("client_created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("processed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.SmallInteger, nullable=False),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("server_entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conflict_detected", sa.Boolean, nullable=False),
        sa.Column("conflict_details", postgresql.JSONB, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sync_queue_user_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_sync_queue"),
        sa.UniqueConstraint("device_id", "client_mutation_id", name="uq_sync_queue_device_mutation"),
    )
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assigned_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=True),
        sa.Column("assigned_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_user_roles_user_id_users", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_user_roles_role_id_roles", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_by"], ["users.id"], name="fk_user_roles_assigned_by_users"),
        sa.PrimaryKeyConstraint("user_id", "role_id", name="pk_user_roles"),
    )
    op.create_table(
        "vehicles",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("registration_number", sa.String(20), nullable=False),
        sa.Column("vehicle_type", postgresql.ENUM(name="vehicle_type", create_type=False), nullable=False),
        sa.Column("make_model", sa.String(120), nullable=True),
        sa.Column("operator_name", sa.String(160), nullable=True),
        sa.Column("capacity_tonnes", sa.Float, nullable=True),
        sa.Column("gross_weight_tonnes", sa.Float, nullable=True),
        sa.Column("height_m", sa.Float, nullable=True),
        sa.Column("width_m", sa.Float, nullable=True),
        sa.Column("length_m", sa.Float, nullable=True),
        sa.Column("fuel_type", sa.String(30), nullable=True),
        sa.Column("fuel_consumption_l_per_100km", sa.Float, nullable=True),
        sa.Column("avg_speed_kmph", sa.Float, nullable=False),
        sa.Column("is_emergency_class", sa.Boolean, nullable=False),
        sa.Column("operational_status", postgresql.ENUM(name="vehicle_operational_status", create_type=False), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("home_district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("gps_device_id", sa.String(80), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("last_position", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("last_position_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_speed_kmph", sa.Float, nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("capacity_tonnes IS NULL OR capacity_tonnes > 0", name="ck_vehicles_capacity_positive"),
        sa.ForeignKeyConstraint(["driver_id"], ["users.id"], name="fk_vehicles_driver_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["home_district_id"], ["districts.id"], name="fk_vehicles_home_district_id_districts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_vehicles"),
    )
    op.create_table(
        "field_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("client_report_id", sa.String(64), nullable=True),
        sa.Column("reporter_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("incident_type", postgresql.ENUM(name="incident_type", create_type=False), nullable=False),
        sa.Column("severity", postgresql.ENUM(name="incident_severity", create_type=False), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("gps_accuracy_m", sa.Float, nullable=True),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("verification_status", postgresql.ENUM(name="verification_status", create_type=False), nullable=False),
        sa.Column("sync_status", postgresql.ENUM(name="sync_status", create_type=False), nullable=False),
        sa.Column("created_offline", sa.Boolean, nullable=False),
        sa.Column("device_info", postgresql.JSONB, nullable=True),
        sa.Column("language", sa.String(10), nullable=False),
        sa.ForeignKeyConstraint(["reporter_id"], ["users.id"], name="fk_field_reports_reporter_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name="fk_field_reports_incident_id_incidents", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_field_reports"),
        sa.UniqueConstraint("reporter_id", "client_report_id", name="uq_field_reports_reporter_client"),
    )
    op.create_table(
        "risk_scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("road_segment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vehicle_type", postgresql.ENUM(name="vehicle_type", create_type=False), nullable=True),
        sa.Column("computed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=False),
        sa.Column("factors", postgresql.JSONB, nullable=False),
        sa.Column("factor_inputs", postgresql.JSONB, nullable=True),
        sa.Column("weight_set_version", sa.String(40), nullable=True),
        sa.Column("model_probability", sa.Float, nullable=True),
        sa.Column("model_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rule_override_applied", sa.Boolean, nullable=False),
        sa.Column("override_reason", sa.Text, nullable=True),
        sa.Column("pre_override_score", sa.Float, nullable=True),
        sa.Column("valid_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("oldest_input_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_simulated", sa.Boolean, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_risk_scores_risk_score_range"),
        sa.ForeignKeyConstraint(["road_segment_id"], ["road_segments.id"], name="fk_risk_scores_road_segment_id_road_segments", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], name="fk_risk_scores_model_version_id_model_versions", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_risk_scores"),
    )
    op.create_table(
        "shipments",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reference_code", sa.String(40), nullable=False),
        sa.Column("commodity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Float, nullable=False),
        sa.Column("unit", sa.String(20), nullable=False),
        sa.Column("priority", sa.SmallInteger, nullable=False),
        sa.Column("priority_profile", postgresql.ENUM(name="shipment_priority_profile", create_type=False), nullable=False),
        sa.Column("status", postgresql.ENUM(name="shipment_status", create_type=False), nullable=False),
        sa.Column("origin_name", sa.String(200), nullable=False),
        sa.Column("origin_geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("origin_district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("destination_name", sa.String(200), nullable=False),
        sa.Column("destination_geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("destination_district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("vehicle_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("current_location", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("current_location_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("planned_departure_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("predicted_eta", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("actual_eta", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("eta_error_minutes", sa.Float, nullable=True),
        sa.Column("active_route_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("route_risk_score", sa.Float, nullable=True),
        sa.Column("route_risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=True),
        sa.Column("reroute_count", sa.Integer, nullable=False),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_shipments_quantity_positive"),
        sa.CheckConstraint("priority >= 1 AND priority <= 10", name="ck_shipments_shipment_priority_range"),
        sa.ForeignKeyConstraint(["commodity_id"], ["commodities.id"], name="fk_shipments_commodity_id_commodities", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["origin_district_id"], ["districts.id"], name="fk_shipments_origin_district_id_districts", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["destination_district_id"], ["districts.id"], name="fk_shipments_destination_district_id_districts", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], name="fk_shipments_vehicle_id_vehicles", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], name="fk_shipments_created_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
    )
    op.create_table(
        "incident_photos",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("field_report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("thumbnail_path", sa.String(500), nullable=True),
        sa.Column("mime_type", sa.String(60), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.Column("captured_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("exif", postgresql.JSONB, nullable=True),
        sa.Column("auto_classification", sa.String(60), nullable=True),
        sa.Column("auto_classification_confidence", sa.Float, nullable=True),
        sa.Column("classified_by_model", sa.String(120), nullable=True),
        sa.CheckConstraint("incident_id IS NOT NULL OR field_report_id IS NOT NULL", name="ck_incident_photos_photo_has_parent"),
        sa.CheckConstraint("size_bytes > 0", name="ck_incident_photos_size_positive"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name="fk_incident_photos_incident_id_incidents", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["field_report_id"], ["field_reports.id"], name="fk_incident_photos_field_report_id_field_reports", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_incident_photos"),
    )
    op.create_table(
        "route_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("label", sa.String(40), nullable=False),
        sa.Column("rank", sa.SmallInteger, nullable=False),
        sa.Column("geom", Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False), nullable=False),
        sa.Column("segment_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("waypoint_names", postgresql.ARRAY(sa.String(200)), nullable=True),
        sa.Column("distance_km", sa.Float, nullable=False),
        sa.Column("duration_minutes", sa.Float, nullable=False),
        sa.Column("base_duration_minutes", sa.Float, nullable=True),
        sa.Column("predicted_eta", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("risk_score", sa.Float, nullable=False),
        sa.Column("risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=False),
        sa.Column("reliability_score", sa.Float, nullable=False),
        sa.Column("estimated_cost", sa.Float, nullable=True),
        sa.Column("estimated_fuel_litres", sa.Float, nullable=True),
        sa.Column("is_vehicle_compatible", sa.Boolean, nullable=False),
        sa.Column("incompatibility_reason", sa.Text, nullable=True),
        sa.Column("blocked_segment_count", sa.SmallInteger, nullable=False),
        sa.Column("score_total", sa.Float, nullable=True),
        sa.Column("score_breakdown", postgresql.JSONB, nullable=True),
        sa.Column("weight_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("top_risk_factors", postgresql.JSONB, nullable=True),
        sa.Column("is_recommended", sa.Boolean, nullable=False),
        sa.Column("recommendation_reason", sa.Text, nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("routing_provider", sa.String(40), nullable=True),
        sa.Column("engine_version", sa.String(40), nullable=True),
        sa.Column("oldest_input_observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("distance_km >= 0", name="ck_route_candidates_candidate_distance_non_negative"),
        sa.CheckConstraint("risk_score >= 0 AND risk_score <= 100", name="ck_route_candidates_candidate_risk_range"),
        sa.CheckConstraint("reliability_score >= 0 AND reliability_score <= 100", name="ck_route_candidates_candidate_reliability_range"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_route_candidates_shipment_id_shipments", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["weight_profile_id"], ["routing_weight_profiles.id"], name="fk_route_candidates_weight_profile_id_routing_weight_profiles", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_route_candidates"),
        sa.UniqueConstraint("request_id", "label", name="uq_route_candidates_label"),
    )
    op.create_table(
        "vehicle_positions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("vehicle_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("road_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("speed_kmph", sa.Float, nullable=True),
        sa.Column("heading_deg", sa.Float, nullable=True),
        sa.Column("altitude_m", sa.Float, nullable=True),
        sa.Column("accuracy_m", sa.Float, nullable=True),
        sa.Column("odometer_km", sa.Float, nullable=True),
        sa.CheckConstraint("speed_kmph IS NULL OR speed_kmph >= 0", name="ck_vehicle_positions_speed_non_negative"),
        sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], name="fk_vehicle_positions_vehicle_id_vehicles", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_vehicle_positions_shipment_id_shipments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["road_segment_id"], ["road_segments.id"], name="fk_vehicle_positions_road_segment_id_road_segments", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_vehicle_positions"),
    )
    op.create_table(
        "routes",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", postgresql.ENUM(name="route_status", create_type=False), nullable=False),
        sa.Column("version", sa.SmallInteger, nullable=False),
        sa.Column("geom", Geometry(geometry_type="LINESTRING", srid=4326, spatial_index=False), nullable=False),
        sa.Column("segment_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("distance_km", sa.Float, nullable=False),
        sa.Column("duration_minutes", sa.Float, nullable=False),
        sa.Column("base_duration_minutes", sa.Float, nullable=True),
        sa.Column("risk_score", sa.Float, nullable=True),
        sa.Column("risk_level", postgresql.ENUM(name="risk_level", create_type=False), nullable=True),
        sa.Column("reliability_score", sa.Float, nullable=True),
        sa.Column("estimated_cost", sa.Float, nullable=True),
        sa.Column("activated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("superseded_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("selected_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_manual_override", sa.Boolean, nullable=False),
        sa.Column("override_reason", sa.Text, nullable=True),
        sa.Column("recommendation_reason", sa.Text, nullable=True),
        sa.Column("weight_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("distance_km >= 0", name="ck_routes_route_distance_non_negative"),
        sa.CheckConstraint("risk_score IS NULL OR (risk_score >= 0 AND risk_score <= 100)", name="ck_routes_route_risk_range"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_routes_shipment_id_shipments", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["route_candidates.id"], name="fk_routes_candidate_id_route_candidates", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["routes.id"], name="fk_routes_superseded_by_id_routes", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["selected_by_id"], ["users.id"], name="fk_routes_selected_by_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["weight_profile_id"], ["routing_weight_profiles.id"], name="fk_routes_weight_profile_id_routing_weight_profiles", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_routes"),
    )
    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("alert_type", postgresql.ENUM(name="alert_type", create_type=False), nullable=False),
        sa.Column("severity", postgresql.ENUM(name="alert_severity", create_type=False), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("recommended_action", sa.Text, nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=True),
        sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False), nullable=True),
        sa.Column("location_name", sa.String(200), nullable=True),
        sa.Column("district_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("road_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("route_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("context", postgresql.JSONB, nullable=True),
        sa.Column("triggered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("occurrence_count", sa.Integer, nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acknowledged_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("acknowledged_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acknowledgement_note", sa.Text, nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_simulated", sa.Boolean, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["district_id"], ["districts.id"], name="fk_alerts_district_id_districts", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["road_segment_id"], ["road_segments.id"], name="fk_alerts_road_segment_id_road_segments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_alerts_shipment_id_shipments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], name="fk_alerts_incident_id_incidents", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], name="fk_alerts_route_id_routes", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["acknowledged_by_id"], ["users.id"], name="fk_alerts_acknowledged_by_id_users", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], name="fk_alerts_created_by_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint("dedupe_key", name="uq_alerts_dedupe_key"),
    )
    op.create_table(
        "eta_predictions",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("route_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("predicted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("predicted_eta", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(40), nullable=True),
        sa.Column("base_duration_minutes", sa.Float, nullable=False),
        sa.Column("traffic_delay_minutes", sa.Float, nullable=False),
        sa.Column("weather_delay_minutes", sa.Float, nullable=False),
        sa.Column("incident_delay_minutes", sa.Float, nullable=False),
        sa.Column("road_condition_delay_minutes", sa.Float, nullable=False),
        sa.Column("rest_stop_minutes", sa.Float, nullable=False),
        sa.Column("total_duration_minutes", sa.Float, nullable=False),
        sa.Column("uncertainty_minutes", sa.Float, nullable=True),
        sa.Column("actual_arrival_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("error_minutes", sa.Float, nullable=True),
        sa.Column("absolute_error_minutes", sa.Float, nullable=True),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("model_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("is_simulated", sa.Boolean, nullable=False),
        sa.Column("simulation_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attributes", postgresql.JSONB, nullable=True),
        sa.CheckConstraint("total_duration_minutes >= 0", name="ck_eta_predictions_eta_duration_non_negative"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_eta_predictions_shipment_id_shipments", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], name="fk_eta_predictions_route_id_routes", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["model_version_id"], ["model_versions.id"], name="fk_eta_predictions_model_version_id_model_versions", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_eta_predictions"),
    )
    op.create_table(
        "route_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("provenance", postgresql.ENUM(name="data_provenance", create_type=False), nullable=False),
        sa.Column("source", sa.String(120), nullable=True),
        sa.Column("observed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("route_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shipment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", postgresql.ENUM(name="route_event_type", create_type=False), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("triggered_by_incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("triggered_by_segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("previous_risk_score", sa.Float, nullable=True),
        sa.Column("new_risk_score", sa.Float, nullable=True),
        sa.Column("previous_eta", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("new_eta", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=True),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], name="fk_route_events_route_id_routes", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shipment_id"], ["shipments.id"], name="fk_route_events_shipment_id_shipments", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["triggered_by_incident_id"], ["incidents.id"], name="fk_route_events_triggered_by_incident_id_incidents", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["triggered_by_segment_id"], ["road_segments.id"], name="fk_route_events_triggered_by_segment_id_road_segments", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], name="fk_route_events_actor_id_users", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_route_events"),
    )
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("alert_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel", postgresql.ENUM(name="notification_channel", create_type=False), nullable=False),
        sa.Column("status", postgresql.ENUM(name="notification_status", create_type=False), nullable=False),
        sa.Column("language", sa.String(10), nullable=False),
        sa.Column("rendered_title", sa.String(255), nullable=True),
        sa.Column("rendered_body", sa.Text, nullable=True),
        sa.Column("sent_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.SmallInteger, nullable=False),
        sa.Column("failure_reason", sa.Text, nullable=True),
        sa.Column("external_reference", sa.String(160), nullable=True),
        sa.ForeignKeyConstraint(["alert_id"], ["alerts.id"], name="fk_notifications_alert_id_alerts", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_notifications_user_id_users", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint("alert_id", "user_id", "channel", name="uq_notifications_alert_user_channel"),
    )


def _close_shipment_route_cycle() -> None:
    """Add the one foreign key that could not be created with its table.

    A shipment points at its currently active route and a route points back at
    its shipment, so whichever table is created first would reference a table
    that does not exist yet. The models mark this constraint ``use_alter=True``;
    here it becomes a plain ``ALTER TABLE`` once both tables are present.

    ``ON DELETE SET NULL``, not ``CASCADE``: deleting a route must not delete the
    shipment it was planned for. The shipment simply has no active route until
    one is recalculated.
    """

    op.create_foreign_key("fk_shipments_active_route_id_routes", "shipments", "routes", ["active_route_id"], ["id"], ondelete="SET NULL")


def _create_indexes() -> None:
    """Create every index, including the spatial ones.

    Indexes come last so that table creation is not slowed by maintaining them,
    and because a few are composite indexes over columns from more than one
    ``__table_args__`` entry.

    Three kinds are present. B-tree for the ordinary lookups and the
    ``created_at`` columns every table carries. GIST for the 22 geometry columns
    — without these, "which segments intersect this flood polygon?" degrades to a
    sequential scan over the whole road network. GIN for the two ``segment_ids``
    arrays, which is what makes "which routes use the segment that just closed?"
    an indexed lookup via ``= ANY(segment_ids)`` instead of a table scan.
    """

    op.create_index("ix_commodities_created_at", "commodities", ["created_at"])
    op.create_index("ix_commodities_name", "commodities", ["name"], unique=True)
    op.create_index("ix_hazard_zones_created_at", "hazard_zones", ["created_at"])
    op.create_index("ix_hazard_zones_provenance", "hazard_zones", ["provenance"])
    op.create_index("ix_hazard_zones_observed_at", "hazard_zones", ["observed_at"])
    op.create_index("ix_hazard_zones_hazard_type", "hazard_zones", ["hazard_type"])
    op.create_index("ix_hazard_zones_geom_gist", "hazard_zones", ["geom"], postgresql_using="gist")
    op.create_index("ix_hazard_zones_type_severity", "hazard_zones", ["hazard_type", "severity_index"])
    op.create_index("ix_roles_created_at", "roles", ["created_at"])
    op.create_index("ix_states_created_at", "states", ["created_at"])
    op.create_index("ix_states_provenance", "states", ["provenance"])
    op.create_index("ix_states_observed_at", "states", ["observed_at"])
    op.create_index("ix_weather_alerts_created_at", "weather_alerts", ["created_at"])
    op.create_index("ix_weather_alerts_provenance", "weather_alerts", ["provenance"])
    op.create_index("ix_weather_alerts_observed_at", "weather_alerts", ["observed_at"])
    op.create_index("ix_weather_alerts_severity", "weather_alerts", ["severity"])
    op.create_index("ix_weather_alerts_window", "weather_alerts", ["effective_from", "effective_to"])
    op.create_index("ix_weather_alerts_geom_gist", "weather_alerts", ["geom"], postgresql_using="gist")
    op.create_index("ix_districts_created_at", "districts", ["created_at"])
    op.create_index("ix_districts_provenance", "districts", ["provenance"])
    op.create_index("ix_districts_observed_at", "districts", ["observed_at"])
    op.create_index("ix_districts_state_id", "districts", ["state_id"])
    op.create_index("ix_districts_geom_gist", "districts", ["geom"], postgresql_using="gist")
    op.create_index("ix_roads_created_at", "roads", ["created_at"])
    op.create_index("ix_roads_provenance", "roads", ["provenance"])
    op.create_index("ix_roads_observed_at", "roads", ["observed_at"])
    op.create_index("ix_roads_name", "roads", ["name"])
    op.create_index("ix_roads_ref", "roads", ["ref"])
    op.create_index("ix_roads_state_id", "roads", ["state_id"])
    op.create_index("ix_road_segments_created_at", "road_segments", ["created_at"])
    op.create_index("ix_road_segments_provenance", "road_segments", ["provenance"])
    op.create_index("ix_road_segments_observed_at", "road_segments", ["observed_at"])
    op.create_index("ix_road_segments_road_id", "road_segments", ["road_id"])
    op.create_index("ix_road_segments_osm_way_id", "road_segments", ["osm_way_id"])
    op.create_index("ix_road_segments_district_id", "road_segments", ["district_id"])
    op.create_index("ix_road_segments_status", "road_segments", ["status"])
    op.create_index("ix_road_segments_official_closure", "road_segments", ["official_closure"])
    op.create_index("ix_road_segments_status_risk", "road_segments", ["status", "risk_score"])
    op.create_index("ix_users_created_at", "users", ["created_at"])
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_username", "users", ["username"], unique=True)
    op.create_index("ix_users_is_active", "users", ["is_active"])
    op.create_index("ix_users_state_id", "users", ["state_id"])
    op.create_index("ix_users_district_id", "users", ["district_id"])
    op.create_index("ix_weather_forecasts_created_at", "weather_forecasts", ["created_at"])
    op.create_index("ix_weather_forecasts_provenance", "weather_forecasts", ["provenance"])
    op.create_index("ix_weather_forecasts_observed_at", "weather_forecasts", ["observed_at"])
    op.create_index("ix_weather_forecasts_district_id", "weather_forecasts", ["district_id"])
    op.create_index("ix_weather_fc_district_valid", "weather_forecasts", ["district_id", "valid_from"])
    op.create_index("ix_weather_observations_created_at", "weather_observations", ["created_at"])
    op.create_index("ix_weather_observations_provenance", "weather_observations", ["provenance"])
    op.create_index("ix_weather_observations_observed_at", "weather_observations", ["observed_at"])
    op.create_index("ix_weather_observations_district_id", "weather_observations", ["district_id"])
    op.create_index("ix_weather_obs_district_observed", "weather_observations", ["district_id", "observed_at"])
    op.create_index("ix_audit_logs_occurred_at", "audit_logs", ["occurred_at"])
    op.create_index("ix_audit_logs_request_id", "audit_logs", ["request_id"])
    op.create_index("ix_audit_logs_actor_time", "audit_logs", ["actor_id", "occurred_at"])
    op.create_index("ix_audit_logs_action_time", "audit_logs", ["action", "occurred_at"])
    op.create_index("ix_audit_logs_entity", "audit_logs", ["entity_type", "entity_id"])
    op.create_index("ix_incidents_created_at", "incidents", ["created_at"])
    op.create_index("ix_incidents_provenance", "incidents", ["provenance"])
    op.create_index("ix_incidents_observed_at", "incidents", ["observed_at"])
    op.create_index("ix_incidents_incident_type", "incidents", ["incident_type"])
    op.create_index("ix_incidents_road_segment_id", "incidents", ["road_segment_id"])
    op.create_index("ix_incidents_district_id", "incidents", ["district_id"])
    op.create_index("ix_incidents_verification_status", "incidents", ["verification_status"])
    op.create_index("ix_incidents_started_at", "incidents", ["started_at"])
    op.create_index("ix_incidents_is_active", "incidents", ["is_active"])
    op.create_index("ix_incidents_is_simulated", "incidents", ["is_simulated"])
    op.create_index("ix_incidents_simulation_run_id", "incidents", ["simulation_run_id"])
    op.create_index("ix_incidents_active_type", "incidents", ["is_active", "incident_type"])
    op.create_index("ix_incidents_segment_active", "incidents", ["road_segment_id", "is_active"])
    op.create_index("ix_model_versions_created_at", "model_versions", ["created_at"])
    op.create_index("ix_model_versions_stage", "model_versions", ["stage"])
    op.create_index("ix_model_versions_task_stage", "model_versions", ["task", "stage"])
    op.create_index("ix_risk_factor_weights_created_at", "risk_factor_weights", ["created_at"])
    op.create_index("ix_risk_factor_weights_factor", "risk_factor_weights", ["factor"])
    op.create_index("ix_risk_factor_weights_is_active", "risk_factor_weights", ["is_active"])
    op.create_index("ix_routing_weight_profiles_created_at", "routing_weight_profiles", ["created_at"])
    op.create_index("ix_routing_weight_profiles_profile", "routing_weight_profiles", ["profile"])
    op.create_index("ix_routing_weight_profiles_is_active", "routing_weight_profiles", ["is_active"])
    op.create_index("ix_sync_queue_created_at", "sync_queue", ["created_at"])
    op.create_index("ix_sync_queue_status_created", "sync_queue", ["status", "client_created_at"])
    op.create_index("ix_sync_queue_user_status", "sync_queue", ["user_id", "status"])
    op.create_index("ix_vehicles_created_at", "vehicles", ["created_at"])
    op.create_index("ix_vehicles_provenance", "vehicles", ["provenance"])
    op.create_index("ix_vehicles_observed_at", "vehicles", ["observed_at"])
    op.create_index("ix_vehicles_registration_number", "vehicles", ["registration_number"], unique=True)
    op.create_index("ix_vehicles_vehicle_type", "vehicles", ["vehicle_type"])
    op.create_index("ix_vehicles_driver_id", "vehicles", ["driver_id"])
    op.create_index("ix_vehicles_type_status", "vehicles", ["vehicle_type", "operational_status"])
    op.create_index("ix_field_reports_created_at", "field_reports", ["created_at"])
    op.create_index("ix_field_reports_provenance", "field_reports", ["provenance"])
    op.create_index("ix_field_reports_observed_at", "field_reports", ["observed_at"])
    op.create_index("ix_field_reports_reporter_id", "field_reports", ["reporter_id"])
    op.create_index("ix_field_reports_incident_id", "field_reports", ["incident_id"])
    op.create_index("ix_field_reports_captured_at", "field_reports", ["captured_at"])
    op.create_index("ix_field_reports_verification_status", "field_reports", ["verification_status"])
    op.create_index("ix_field_reports_status_captured", "field_reports", ["sync_status", "captured_at"])
    op.create_index("ix_risk_scores_provenance", "risk_scores", ["provenance"])
    op.create_index("ix_risk_scores_observed_at", "risk_scores", ["observed_at"])
    op.create_index("ix_risk_scores_computed_at", "risk_scores", ["computed_at"])
    op.create_index("ix_risk_scores_is_simulated", "risk_scores", ["is_simulated"])
    op.create_index("ix_risk_scores_simulation_run_id", "risk_scores", ["simulation_run_id"])
    op.create_index("ix_risk_scores_segment_time", "risk_scores", ["road_segment_id", "computed_at"])
    op.create_index("ix_risk_scores_level_time", "risk_scores", ["risk_level", "computed_at"])
    op.create_index("ix_shipments_created_at", "shipments", ["created_at"])
    op.create_index("ix_shipments_provenance", "shipments", ["provenance"])
    op.create_index("ix_shipments_observed_at", "shipments", ["observed_at"])
    op.create_index("ix_shipments_reference_code", "shipments", ["reference_code"], unique=True)
    op.create_index("ix_shipments_commodity_id", "shipments", ["commodity_id"])
    op.create_index("ix_shipments_status", "shipments", ["status"])
    op.create_index("ix_shipments_origin_district_id", "shipments", ["origin_district_id"])
    op.create_index("ix_shipments_destination_district_id", "shipments", ["destination_district_id"])
    op.create_index("ix_shipments_vehicle_id", "shipments", ["vehicle_id"])
    op.create_index("ix_shipments_status_priority", "shipments", ["status", "priority"])
    op.create_index("ix_shipments_deadline", "shipments", ["deadline_at"])
    op.create_index("ix_incident_photos_created_at", "incident_photos", ["created_at"])
    op.create_index("ix_incident_photos_provenance", "incident_photos", ["provenance"])
    op.create_index("ix_incident_photos_observed_at", "incident_photos", ["observed_at"])
    op.create_index("ix_incident_photos_incident_id", "incident_photos", ["incident_id"])
    op.create_index("ix_incident_photos_field_report_id", "incident_photos", ["field_report_id"])
    op.create_index("ix_route_candidates_created_at", "route_candidates", ["created_at"])
    op.create_index("ix_route_candidates_provenance", "route_candidates", ["provenance"])
    op.create_index("ix_route_candidates_observed_at", "route_candidates", ["observed_at"])
    op.create_index("ix_route_candidates_request_id", "route_candidates", ["request_id"])
    op.create_index("ix_route_candidates_shipment_id", "route_candidates", ["shipment_id"])
    op.create_index("ix_route_candidates_simulation_run_id", "route_candidates", ["simulation_run_id"])
    op.create_index("ix_route_candidates_request_rank", "route_candidates", ["request_id", "rank"])
    op.create_index("ix_route_candidates_segments", "route_candidates", ["segment_ids"], postgresql_using="gin")
    op.create_index("ix_vehicle_positions_provenance", "vehicle_positions", ["provenance"])
    op.create_index("ix_vehicle_positions_observed_at", "vehicle_positions", ["observed_at"])
    op.create_index("ix_vehicle_positions_shipment_id", "vehicle_positions", ["shipment_id"])
    op.create_index("ix_vehicle_positions_vehicle_time", "vehicle_positions", ["vehicle_id", "recorded_at"])
    op.create_index("ix_routes_created_at", "routes", ["created_at"])
    op.create_index("ix_routes_provenance", "routes", ["provenance"])
    op.create_index("ix_routes_observed_at", "routes", ["observed_at"])
    op.create_index("ix_routes_shipment_id", "routes", ["shipment_id"])
    op.create_index("ix_routes_status", "routes", ["status"])
    op.create_index("ix_routes_shipment_status", "routes", ["shipment_id", "status"])
    op.create_index("ix_routes_segments", "routes", ["segment_ids"], postgresql_using="gin")
    op.create_index("ix_alerts_created_at", "alerts", ["created_at"])
    op.create_index("ix_alerts_provenance", "alerts", ["provenance"])
    op.create_index("ix_alerts_observed_at", "alerts", ["observed_at"])
    op.create_index("ix_alerts_alert_type", "alerts", ["alert_type"])
    op.create_index("ix_alerts_severity", "alerts", ["severity"])
    op.create_index("ix_alerts_district_id", "alerts", ["district_id"])
    op.create_index("ix_alerts_road_segment_id", "alerts", ["road_segment_id"])
    op.create_index("ix_alerts_shipment_id", "alerts", ["shipment_id"])
    op.create_index("ix_alerts_incident_id", "alerts", ["incident_id"])
    op.create_index("ix_alerts_is_active", "alerts", ["is_active"])
    op.create_index("ix_alerts_is_simulated", "alerts", ["is_simulated"])
    op.create_index("ix_alerts_simulation_run_id", "alerts", ["simulation_run_id"])
    op.create_index("ix_alerts_active_severity", "alerts", ["is_active", "severity"])
    op.create_index("ix_alerts_triggered_at", "alerts", ["triggered_at"])
    op.create_index("ix_eta_predictions_created_at", "eta_predictions", ["created_at"])
    op.create_index("ix_eta_predictions_provenance", "eta_predictions", ["provenance"])
    op.create_index("ix_eta_predictions_observed_at", "eta_predictions", ["observed_at"])
    op.create_index("ix_eta_predictions_route_id", "eta_predictions", ["route_id"])
    op.create_index("ix_eta_predictions_is_simulated", "eta_predictions", ["is_simulated"])
    op.create_index("ix_eta_predictions_simulation_run_id", "eta_predictions", ["simulation_run_id"])
    op.create_index("ix_eta_predictions_shipment_time", "eta_predictions", ["shipment_id", "predicted_at"])
    op.create_index("ix_route_events_provenance", "route_events", ["provenance"])
    op.create_index("ix_route_events_observed_at", "route_events", ["observed_at"])
    op.create_index("ix_route_events_shipment_id", "route_events", ["shipment_id"])
    op.create_index("ix_route_events_route_time", "route_events", ["route_id", "occurred_at"])
    op.create_index("ix_route_events_type_time", "route_events", ["event_type", "occurred_at"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])
    op.create_index("ix_notifications_user_status", "notifications", ["user_id", "status"])
    op.create_index("ix_notifications_status_queued", "notifications", ["status", "created_at"])
    op.create_index("ix_states_geom_gist", "states", ["geom"], postgresql_using="gist")
    op.create_index("ix_districts_centroid_gist", "districts", ["centroid"], postgresql_using="gist")
    op.create_index("ix_roads_geom_gist", "roads", ["geom"], postgresql_using="gist")
    op.create_index("ix_road_segments_geom_gist", "road_segments", ["geom"], postgresql_using="gist")
    op.create_index("ix_road_segments_start_point_gist", "road_segments", ["start_point"], postgresql_using="gist")
    op.create_index("ix_road_segments_end_point_gist", "road_segments", ["end_point"], postgresql_using="gist")
    op.create_index("ix_weather_forecasts_geom_gist", "weather_forecasts", ["geom"], postgresql_using="gist")
    op.create_index("ix_weather_observations_geom_gist", "weather_observations", ["geom"], postgresql_using="gist")
    op.create_index("ix_incidents_geom_gist", "incidents", ["geom"], postgresql_using="gist")
    op.create_index("ix_vehicles_last_position_gist", "vehicles", ["last_position"], postgresql_using="gist")
    op.create_index("ix_field_reports_geom_gist", "field_reports", ["geom"], postgresql_using="gist")
    op.create_index("ix_shipments_origin_geom_gist", "shipments", ["origin_geom"], postgresql_using="gist")
    op.create_index("ix_shipments_destination_geom_gist", "shipments", ["destination_geom"], postgresql_using="gist")
    op.create_index("ix_shipments_current_location_gist", "shipments", ["current_location"], postgresql_using="gist")
    op.create_index("ix_incident_photos_geom_gist", "incident_photos", ["geom"], postgresql_using="gist")
    op.create_index("ix_route_candidates_geom_gist", "route_candidates", ["geom"], postgresql_using="gist")
    op.create_index("ix_vehicle_positions_geom_gist", "vehicle_positions", ["geom"], postgresql_using="gist")
    op.create_index("ix_routes_geom_gist", "routes", ["geom"], postgresql_using="gist")
    op.create_index("ix_alerts_geom_gist", "alerts", ["geom"], postgresql_using="gist")


def _drop_indexes() -> None:
    """Drop the indexes created above.

    ``DROP TABLE`` would remove them anyway, but naming them here means the
    downgrade is explicit about what it destroys, and it keeps the reverse
    operation symmetric with the forward one.
    """

    op.drop_index("ix_commodities_created_at", table_name="commodities")
    op.drop_index("ix_commodities_name", table_name="commodities")
    op.drop_index("ix_hazard_zones_created_at", table_name="hazard_zones")
    op.drop_index("ix_hazard_zones_provenance", table_name="hazard_zones")
    op.drop_index("ix_hazard_zones_observed_at", table_name="hazard_zones")
    op.drop_index("ix_hazard_zones_hazard_type", table_name="hazard_zones")
    op.drop_index("ix_hazard_zones_geom_gist", table_name="hazard_zones")
    op.drop_index("ix_hazard_zones_type_severity", table_name="hazard_zones")
    op.drop_index("ix_roles_created_at", table_name="roles")
    op.drop_index("ix_states_created_at", table_name="states")
    op.drop_index("ix_states_provenance", table_name="states")
    op.drop_index("ix_states_observed_at", table_name="states")
    op.drop_index("ix_weather_alerts_created_at", table_name="weather_alerts")
    op.drop_index("ix_weather_alerts_provenance", table_name="weather_alerts")
    op.drop_index("ix_weather_alerts_observed_at", table_name="weather_alerts")
    op.drop_index("ix_weather_alerts_severity", table_name="weather_alerts")
    op.drop_index("ix_weather_alerts_window", table_name="weather_alerts")
    op.drop_index("ix_weather_alerts_geom_gist", table_name="weather_alerts")
    op.drop_index("ix_districts_created_at", table_name="districts")
    op.drop_index("ix_districts_provenance", table_name="districts")
    op.drop_index("ix_districts_observed_at", table_name="districts")
    op.drop_index("ix_districts_state_id", table_name="districts")
    op.drop_index("ix_districts_geom_gist", table_name="districts")
    op.drop_index("ix_roads_created_at", table_name="roads")
    op.drop_index("ix_roads_provenance", table_name="roads")
    op.drop_index("ix_roads_observed_at", table_name="roads")
    op.drop_index("ix_roads_name", table_name="roads")
    op.drop_index("ix_roads_ref", table_name="roads")
    op.drop_index("ix_roads_state_id", table_name="roads")
    op.drop_index("ix_road_segments_created_at", table_name="road_segments")
    op.drop_index("ix_road_segments_provenance", table_name="road_segments")
    op.drop_index("ix_road_segments_observed_at", table_name="road_segments")
    op.drop_index("ix_road_segments_road_id", table_name="road_segments")
    op.drop_index("ix_road_segments_osm_way_id", table_name="road_segments")
    op.drop_index("ix_road_segments_district_id", table_name="road_segments")
    op.drop_index("ix_road_segments_status", table_name="road_segments")
    op.drop_index("ix_road_segments_official_closure", table_name="road_segments")
    op.drop_index("ix_road_segments_status_risk", table_name="road_segments")
    op.drop_index("ix_users_created_at", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_index("ix_users_username", table_name="users")
    op.drop_index("ix_users_is_active", table_name="users")
    op.drop_index("ix_users_state_id", table_name="users")
    op.drop_index("ix_users_district_id", table_name="users")
    op.drop_index("ix_weather_forecasts_created_at", table_name="weather_forecasts")
    op.drop_index("ix_weather_forecasts_provenance", table_name="weather_forecasts")
    op.drop_index("ix_weather_forecasts_observed_at", table_name="weather_forecasts")
    op.drop_index("ix_weather_forecasts_district_id", table_name="weather_forecasts")
    op.drop_index("ix_weather_fc_district_valid", table_name="weather_forecasts")
    op.drop_index("ix_weather_observations_created_at", table_name="weather_observations")
    op.drop_index("ix_weather_observations_provenance", table_name="weather_observations")
    op.drop_index("ix_weather_observations_observed_at", table_name="weather_observations")
    op.drop_index("ix_weather_observations_district_id", table_name="weather_observations")
    op.drop_index("ix_weather_obs_district_observed", table_name="weather_observations")
    op.drop_index("ix_audit_logs_occurred_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_request_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_actor_time", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_time", table_name="audit_logs")
    op.drop_index("ix_audit_logs_entity", table_name="audit_logs")
    op.drop_index("ix_incidents_created_at", table_name="incidents")
    op.drop_index("ix_incidents_provenance", table_name="incidents")
    op.drop_index("ix_incidents_observed_at", table_name="incidents")
    op.drop_index("ix_incidents_incident_type", table_name="incidents")
    op.drop_index("ix_incidents_road_segment_id", table_name="incidents")
    op.drop_index("ix_incidents_district_id", table_name="incidents")
    op.drop_index("ix_incidents_verification_status", table_name="incidents")
    op.drop_index("ix_incidents_started_at", table_name="incidents")
    op.drop_index("ix_incidents_is_active", table_name="incidents")
    op.drop_index("ix_incidents_is_simulated", table_name="incidents")
    op.drop_index("ix_incidents_simulation_run_id", table_name="incidents")
    op.drop_index("ix_incidents_active_type", table_name="incidents")
    op.drop_index("ix_incidents_segment_active", table_name="incidents")
    op.drop_index("ix_model_versions_created_at", table_name="model_versions")
    op.drop_index("ix_model_versions_stage", table_name="model_versions")
    op.drop_index("ix_model_versions_task_stage", table_name="model_versions")
    op.drop_index("ix_risk_factor_weights_created_at", table_name="risk_factor_weights")
    op.drop_index("ix_risk_factor_weights_factor", table_name="risk_factor_weights")
    op.drop_index("ix_risk_factor_weights_is_active", table_name="risk_factor_weights")
    op.drop_index("ix_routing_weight_profiles_created_at", table_name="routing_weight_profiles")
    op.drop_index("ix_routing_weight_profiles_profile", table_name="routing_weight_profiles")
    op.drop_index("ix_routing_weight_profiles_is_active", table_name="routing_weight_profiles")
    op.drop_index("ix_sync_queue_created_at", table_name="sync_queue")
    op.drop_index("ix_sync_queue_status_created", table_name="sync_queue")
    op.drop_index("ix_sync_queue_user_status", table_name="sync_queue")
    op.drop_index("ix_vehicles_created_at", table_name="vehicles")
    op.drop_index("ix_vehicles_provenance", table_name="vehicles")
    op.drop_index("ix_vehicles_observed_at", table_name="vehicles")
    op.drop_index("ix_vehicles_registration_number", table_name="vehicles")
    op.drop_index("ix_vehicles_vehicle_type", table_name="vehicles")
    op.drop_index("ix_vehicles_driver_id", table_name="vehicles")
    op.drop_index("ix_vehicles_type_status", table_name="vehicles")
    op.drop_index("ix_field_reports_created_at", table_name="field_reports")
    op.drop_index("ix_field_reports_provenance", table_name="field_reports")
    op.drop_index("ix_field_reports_observed_at", table_name="field_reports")
    op.drop_index("ix_field_reports_reporter_id", table_name="field_reports")
    op.drop_index("ix_field_reports_incident_id", table_name="field_reports")
    op.drop_index("ix_field_reports_captured_at", table_name="field_reports")
    op.drop_index("ix_field_reports_verification_status", table_name="field_reports")
    op.drop_index("ix_field_reports_status_captured", table_name="field_reports")
    op.drop_index("ix_risk_scores_provenance", table_name="risk_scores")
    op.drop_index("ix_risk_scores_observed_at", table_name="risk_scores")
    op.drop_index("ix_risk_scores_computed_at", table_name="risk_scores")
    op.drop_index("ix_risk_scores_is_simulated", table_name="risk_scores")
    op.drop_index("ix_risk_scores_simulation_run_id", table_name="risk_scores")
    op.drop_index("ix_risk_scores_segment_time", table_name="risk_scores")
    op.drop_index("ix_risk_scores_level_time", table_name="risk_scores")
    op.drop_index("ix_shipments_created_at", table_name="shipments")
    op.drop_index("ix_shipments_provenance", table_name="shipments")
    op.drop_index("ix_shipments_observed_at", table_name="shipments")
    op.drop_index("ix_shipments_reference_code", table_name="shipments")
    op.drop_index("ix_shipments_commodity_id", table_name="shipments")
    op.drop_index("ix_shipments_status", table_name="shipments")
    op.drop_index("ix_shipments_origin_district_id", table_name="shipments")
    op.drop_index("ix_shipments_destination_district_id", table_name="shipments")
    op.drop_index("ix_shipments_vehicle_id", table_name="shipments")
    op.drop_index("ix_shipments_status_priority", table_name="shipments")
    op.drop_index("ix_shipments_deadline", table_name="shipments")
    op.drop_index("ix_incident_photos_created_at", table_name="incident_photos")
    op.drop_index("ix_incident_photos_provenance", table_name="incident_photos")
    op.drop_index("ix_incident_photos_observed_at", table_name="incident_photos")
    op.drop_index("ix_incident_photos_incident_id", table_name="incident_photos")
    op.drop_index("ix_incident_photos_field_report_id", table_name="incident_photos")
    op.drop_index("ix_route_candidates_created_at", table_name="route_candidates")
    op.drop_index("ix_route_candidates_provenance", table_name="route_candidates")
    op.drop_index("ix_route_candidates_observed_at", table_name="route_candidates")
    op.drop_index("ix_route_candidates_request_id", table_name="route_candidates")
    op.drop_index("ix_route_candidates_shipment_id", table_name="route_candidates")
    op.drop_index("ix_route_candidates_simulation_run_id", table_name="route_candidates")
    op.drop_index("ix_route_candidates_request_rank", table_name="route_candidates")
    op.drop_index("ix_route_candidates_segments", table_name="route_candidates")
    op.drop_index("ix_vehicle_positions_provenance", table_name="vehicle_positions")
    op.drop_index("ix_vehicle_positions_observed_at", table_name="vehicle_positions")
    op.drop_index("ix_vehicle_positions_shipment_id", table_name="vehicle_positions")
    op.drop_index("ix_vehicle_positions_vehicle_time", table_name="vehicle_positions")
    op.drop_index("ix_routes_created_at", table_name="routes")
    op.drop_index("ix_routes_provenance", table_name="routes")
    op.drop_index("ix_routes_observed_at", table_name="routes")
    op.drop_index("ix_routes_shipment_id", table_name="routes")
    op.drop_index("ix_routes_status", table_name="routes")
    op.drop_index("ix_routes_shipment_status", table_name="routes")
    op.drop_index("ix_routes_segments", table_name="routes")
    op.drop_index("ix_alerts_created_at", table_name="alerts")
    op.drop_index("ix_alerts_provenance", table_name="alerts")
    op.drop_index("ix_alerts_observed_at", table_name="alerts")
    op.drop_index("ix_alerts_alert_type", table_name="alerts")
    op.drop_index("ix_alerts_severity", table_name="alerts")
    op.drop_index("ix_alerts_district_id", table_name="alerts")
    op.drop_index("ix_alerts_road_segment_id", table_name="alerts")
    op.drop_index("ix_alerts_shipment_id", table_name="alerts")
    op.drop_index("ix_alerts_incident_id", table_name="alerts")
    op.drop_index("ix_alerts_is_active", table_name="alerts")
    op.drop_index("ix_alerts_is_simulated", table_name="alerts")
    op.drop_index("ix_alerts_simulation_run_id", table_name="alerts")
    op.drop_index("ix_alerts_active_severity", table_name="alerts")
    op.drop_index("ix_alerts_triggered_at", table_name="alerts")
    op.drop_index("ix_eta_predictions_created_at", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_provenance", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_observed_at", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_route_id", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_is_simulated", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_simulation_run_id", table_name="eta_predictions")
    op.drop_index("ix_eta_predictions_shipment_time", table_name="eta_predictions")
    op.drop_index("ix_route_events_provenance", table_name="route_events")
    op.drop_index("ix_route_events_observed_at", table_name="route_events")
    op.drop_index("ix_route_events_shipment_id", table_name="route_events")
    op.drop_index("ix_route_events_route_time", table_name="route_events")
    op.drop_index("ix_route_events_type_time", table_name="route_events")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_user_status", table_name="notifications")
    op.drop_index("ix_notifications_status_queued", table_name="notifications")
    op.drop_index("ix_states_geom_gist", table_name="states")
    op.drop_index("ix_districts_centroid_gist", table_name="districts")
    op.drop_index("ix_roads_geom_gist", table_name="roads")
    op.drop_index("ix_road_segments_geom_gist", table_name="road_segments")
    op.drop_index("ix_road_segments_start_point_gist", table_name="road_segments")
    op.drop_index("ix_road_segments_end_point_gist", table_name="road_segments")
    op.drop_index("ix_weather_forecasts_geom_gist", table_name="weather_forecasts")
    op.drop_index("ix_weather_observations_geom_gist", table_name="weather_observations")
    op.drop_index("ix_incidents_geom_gist", table_name="incidents")
    op.drop_index("ix_vehicles_last_position_gist", table_name="vehicles")
    op.drop_index("ix_field_reports_geom_gist", table_name="field_reports")
    op.drop_index("ix_shipments_origin_geom_gist", table_name="shipments")
    op.drop_index("ix_shipments_destination_geom_gist", table_name="shipments")
    op.drop_index("ix_shipments_current_location_gist", table_name="shipments")
    op.drop_index("ix_incident_photos_geom_gist", table_name="incident_photos")
    op.drop_index("ix_route_candidates_geom_gist", table_name="route_candidates")
    op.drop_index("ix_vehicle_positions_geom_gist", table_name="vehicle_positions")
    op.drop_index("ix_routes_geom_gist", table_name="routes")
    op.drop_index("ix_alerts_geom_gist", table_name="alerts")

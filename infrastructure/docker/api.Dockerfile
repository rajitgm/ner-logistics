# API image. Build from the repository root:
#   docker build -f infrastructure/docker/api.Dockerfile -t ner-api .
#
# Two stages so the runtime image carries wheels but not the compilers used to
# build them. python:3.12-slim rather than alpine: asyncpg and psycopg publish
# manylinux wheels, and musl would force a source build of both.

# --------------------------------------------------------------------- builder
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential and libpq-dev cover the case where a pinned dependency has no
# wheel for this platform; they stay in this stage and are never shipped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
# Requirements copied alone so the layer is cached until a pin actually changes.
COPY services/api/requirements.txt ./
RUN pip wheel --wheel-dir /wheels/dist -r requirements.txt

# --------------------------------------------------------------------- runtime
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# curl for the compose healthcheck's fallback and for probing OSRM during
# development; libpq5 is psycopg's runtime library. No compilers here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl libpq5 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels/dist /wheels/dist
COPY services/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels/dist -r /tmp/requirements.txt \
 && rm -rf /wheels /tmp/requirements.txt

# Runs as a non-root user: the API accepts file uploads, and a container that
# writes attacker-supplied bytes should not do so as uid 0. /data is the mount
# point for uploads and OSM extracts.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
 && mkdir -p /data/uploads /data/osm \
 && chown -R appuser:appuser /data

WORKDIR /app
COPY --chown=appuser:appuser services/api /app

USER appuser
EXPOSE 8000

# Overridden by docker-compose, which adds --reload for development. This form
# is the one a deployment would use.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

# ------------------------------------------------------------------------- dev
# Adds pytest, ruff and mypy. Built explicitly, so the default image stays lean:
#   docker build -f infrastructure/docker/api.Dockerfile --target dev -t ner-api:dev .
FROM runtime AS dev

USER root
COPY services/api/requirements-dev.txt /tmp/requirements-dev.txt
# Not --no-index: the dev pins were not wheeled in the builder stage, since a
# deployment never installs them.
RUN pip install -r /tmp/requirements-dev.txt && rm /tmp/requirements-dev.txt
USER appuser

CMD ["pytest", "-q"]

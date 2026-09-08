"""Application configuration.

Every tunable is read from the environment (or a local ``.env``) — there are no
secrets in source control. ``.env.example`` at the repository root documents each
variable and is kept in sync with this module by
``scripts/verify_phase1.py::check_env_parity``.

Configuration is validated at import time so a misconfigured deployment fails
fast and loudly rather than silently running with an insecure default.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Any, List, Literal, Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Sentinel used by ``.env.example``. Refused outside development.
DEV_SECRET_PLACEHOLDER = "change-me-in-production"


class Settings(BaseSettings):
    """Typed, validated runtime settings."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ----------------------------------------------------------------- application
    PROJECT_NAME: str = "NER Logistics Resilience Platform"
    ENV: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # -------------------------------------------------------------------- security
    SECRET_KEY: str = DEV_SECRET_PLACEHOLDER
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "ner-logistics"
    JWT_AUDIENCE: str = "ner-logistics-api"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, ge=1, le=1440)
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, ge=1, le=90)
    BCRYPT_ROUNDS: int = Field(default=12, ge=10, le=16)

    #: Comma-separated in the environment, list in code.
    CORS_ORIGINS: List[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    CORS_ALLOW_CREDENTIALS: bool = True

    RATE_LIMIT_ENABLED: bool = True
    #: Requests per window for general endpoints.
    RATE_LIMIT_DEFAULT: int = Field(default=120, ge=1)
    #: Much tighter budget for credential endpoints, to blunt brute force.
    RATE_LIMIT_AUTH: int = Field(default=10, ge=1)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    #: Consecutive failed logins before an account is temporarily locked.
    LOGIN_MAX_FAILURES: int = Field(default=5, ge=1)
    LOGIN_LOCKOUT_MINUTES: int = Field(default=15, ge=1)

    # ---------------------------------------------------------------- file uploads
    UPLOAD_DIR: str = "/data/uploads"
    MAX_UPLOAD_MB: int = Field(default=10, ge=1, le=100)
    ALLOWED_UPLOAD_MIME: List[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/webp"]
    )

    # -------------------------------------------------------------------- database
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "ner"
    POSTGRES_PASSWORD: str = "ner"
    POSTGRES_DB: str = "ner_logistics"
    DB_POOL_SIZE: int = Field(default=10, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=20, ge=0)
    DB_ECHO: bool = False

    # ----------------------------------------------------------------------- redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: Optional[str] = None

    # ------------------------------------------------------------------ geospatial
    #: Storage SRID. WGS84 everywhere in the database and over the API.
    DEFAULT_SRID: int = 4326
    #: Metric SRID for distance/area/buffer work. UTM zone 46N covers most of the
    #: North Eastern Region (approx. 90E-96E); zone 45N is used for far-western
    #: Assam when higher precision is needed.
    METRIC_SRID: int = 32646

    # --------------------------------------------------------------------- routing
    ROUTING_PROVIDER: Literal["osrm", "graphhopper", "valhalla", "stub"] = "osrm"
    OSRM_BASE_URL: str = "http://osrm:5000"
    ROUTING_TIMEOUT_SECONDS: float = Field(default=10.0, gt=0)
    #: Number of distinct candidate routes to score for a shipment.
    ROUTE_CANDIDATE_COUNT: int = Field(default=3, ge=1, le=10)

    # ------------------------------------------------------- external data adapters
    #: Each provider defaults to the honest option: a synthetic implementation.
    #: Switching to a live source is a configuration change, not a code change.
    WEATHER_PROVIDER: Literal["synthetic", "imd", "openmeteo"] = "synthetic"
    OSM_PROVIDER: Literal["local_extract", "overpass"] = "local_extract"
    HAZARD_PROVIDER: Literal["synthetic", "bhuvan"] = "synthetic"
    INCIDENT_PROVIDER: Literal["internal", "external_feed"] = "internal"

    IMD_BASE_URL: Optional[str] = None
    IMD_API_KEY: Optional[str] = None
    OPENMETEO_BASE_URL: str = "https://api.open-meteo.com/v1"
    BHUVAN_BASE_URL: Optional[str] = None
    BHUVAN_API_KEY: Optional[str] = None
    OVERPASS_BASE_URL: str = "https://overpass-api.de/api/interpreter"
    OSM_EXTRACT_PATH: str = "/data/osm/northeast-india-latest.osm.pbf"

    # ------------------------------------------------------- LLM copilot (Phase 12)
    #: The core product must work with the copilot switched off. When
    #: ``LLM_ENABLED`` is false the assistant endpoints return 503 rather than
    #: degrading routing or risk output.
    LLM_ENABLED: bool = False
    LLM_PROVIDER: Literal["ollama", "openai_compatible", "stub"] = "ollama"
    #: From a container on Docker Desktop, the Windows host is host.docker.internal.
    #: Running the API natively, use http://localhost:11434 instead.
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"
    #: Exact local tag, e.g. `qwen3:30b-a3b`. Verify with `ollama list`.
    LLM_MODEL: str = "qwen3:30b-a3b"
    LLM_TIMEOUT_SECONDS: float = Field(default=120.0, gt=0)
    LLM_MAX_TOKENS: int = Field(default=1024, ge=64)
    LLM_TEMPERATURE: float = Field(default=0.1, ge=0.0, le=2.0)
    #: Hard ceiling on tool-call round trips per user question.
    LLM_MAX_TOOL_CALLS: int = Field(default=5, ge=1, le=20)

    # --------------------------------------------------------- realtime & simulator
    WS_HEARTBEAT_SECONDS: int = Field(default=25, ge=5)
    GPS_SIMULATOR_ENABLED: bool = False
    GPS_SIMULATOR_INTERVAL_SECONDS: float = Field(default=5.0, gt=0)

    # ------------------------------------------------------------------- bootstrap
    #: Used once by ``scripts/seed.py`` to create the first administrator. Leave
    #: the password unset to have the seeder generate one and print it.
    BOOTSTRAP_ADMIN_EMAIL: str = "admin@ner-logistics.local"
    BOOTSTRAP_ADMIN_PASSWORD: Optional[str] = None
    SEED_SYNTHETIC_DATA: bool = True

    # ------------------------------------------------------------------- validation
    @field_validator("CORS_ORIGINS", "ALLOWED_UPLOAD_MIME", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Accept both a JSON array and a comma-separated string from the env."""

        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                return value  # let pydantic parse the JSON form
            return [item.strip() for item in stripped.split(",") if item.strip()]
        return value

    @field_validator("LOG_LEVEL")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return upper

    @model_validator(mode="after")
    def _enforce_deployment_safety(self) -> "Settings":
        """Refuse insecure combinations outside development.

        Catching these at boot is the difference between a failed deploy and a
        publicly readable command centre.
        """

        if self.ENV in {"production", "staging"}:
            if self.SECRET_KEY == DEV_SECRET_PLACEHOLDER or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "SECRET_KEY must be set to a unique value of at least 32 "
                    f"characters when ENV={self.ENV}. Generate one with: "
                    "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
                )
            if self.DEBUG:
                raise ValueError(f"DEBUG must be false when ENV={self.ENV}")
            if "*" in self.CORS_ORIGINS:
                raise ValueError(
                    f"CORS_ORIGINS may not be a wildcard when ENV={self.ENV}"
                )
        return self

    # -------------------------------------------------------------- derived values
    @property
    def sqlalchemy_dsn(self) -> str:
        """Synchronous DSN, used by Alembic and management scripts."""

        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def sqlalchemy_async_dsn(self) -> str:
        """Asynchronous DSN, used by the FastAPI request path."""

        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def redis_dsn(self) -> str:
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"


@lru_cache
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so the environment is parsed and validated exactly once. Tests clear
    the cache via ``get_settings.cache_clear()`` when overriding values.
    """

    return Settings()


def generate_secret_key() -> str:
    """Helper for operators bootstrapping a deployment."""

    return secrets.token_urlsafe(48)


"""Validated settings for platform-owned infrastructure."""

import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_DATABASE_URI = (
    "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform"
)
LOCAL_CORS_ORIGINS = (
    "http://127.0.0.1:3000",
    "http://localhost:3000",
)
# Only meaningful for a source checkout. A non-editable install resolves this
# inside site-packages, which is why production must set both directories.
DEVELOPMENT_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEVELOPMENT_DATA_DIR = DEVELOPMENT_PROJECT_ROOT / "var" / "data"
DEVELOPMENT_RUN_DIR = DEVELOPMENT_PROJECT_ROOT / "var" / "runs"
_SETTINGS_CONFIG = SettingsConfigDict(
    env_file=".env",
    env_file_encoding="utf-8",
    extra="ignore",
    case_sensitive=False,
)


Environment = Literal["development", "test", "production"]


def _blank_path_to_none(value: object) -> object:
    """Treat ``AGRIHUB_*_DIR=`` from a copied ``.env.example`` as unset."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _resolve_agrihub_directories(settings: "DataPaths | Settings") -> None:
    """Fill development defaults, or reject production without explicit paths."""
    missing = [
        name
        for name in ("AGRIHUB_DATA_DIR", "AGRIHUB_RUN_DIR")
        if getattr(settings, name) is None
    ]
    if missing and settings.ENVIRONMENT == "production":
        raise ValueError(
            f"{' and '.join(missing)} must be set when ENVIRONMENT=production"
        )
    if settings.AGRIHUB_DATA_DIR is None:
        settings.AGRIHUB_DATA_DIR = DEVELOPMENT_DATA_DIR
    if settings.AGRIHUB_RUN_DIR is None:
        settings.AGRIHUB_RUN_DIR = DEVELOPMENT_RUN_DIR


class DataPaths(BaseSettings):
    """Where AgriHub reads species bundles and writes run-scoped stores.

    Development and test fall back to ``var/data`` and ``var/runs`` in the
    source checkout. Production refuses to start unless both are set.
    """

    ENVIRONMENT: Environment = "development"
    AGRIHUB_DATA_DIR: Path | None = None
    AGRIHUB_RUN_DIR: Path | None = None

    model_config = _SETTINGS_CONFIG

    _blank_directories = field_validator(
        "AGRIHUB_DATA_DIR", "AGRIHUB_RUN_DIR", mode="before"
    )(_blank_path_to_none)

    @model_validator(mode="after")
    def require_production_directories(self) -> "DataPaths":
        """Resolve the directories for the current environment."""
        _resolve_agrihub_directories(self)
        return self

    @property
    def data_dir(self) -> Path:
        """Return the directory holding one subdirectory per species bundle."""
        return Path(str(self.AGRIHUB_DATA_DIR))

    @property
    def run_dir(self) -> Path:
        """Return the directory holding one subdirectory per run."""
        return Path(str(self.AGRIHUB_RUN_DIR))


class Settings(BaseSettings):
    """Load database configuration from the process environment."""

    ENVIRONMENT: Environment = "development"
    AGRIHUB_DATA_DIR: Path | None = None
    AGRIHUB_RUN_DIR: Path | None = None
    DATABASE_URI: str | None = Field(default=None, repr=False)
    API_HOST: str = "127.0.0.1"
    API_PORT: int = Field(default=8000, ge=1, le=65535)
    API_ALLOWED_ORIGINS: list[str] = Field(
        default_factory=lambda: list(LOCAL_CORS_ORIGINS)
    )
    API_MAX_REQUEST_BODY_BYTES: int = Field(default=10_485_760, ge=1)
    API_MAX_CONCURRENT_RUNS: int = Field(default=4, ge=1)
    API_STREAM_SUBSCRIBER_QUEUE_SIZE: int = Field(default=16, ge=1)
    AUTH_MODE: Literal["api_key", "disabled"] | None = None
    API_KEY_PEPPER: str | None = Field(default=None, repr=False)
    API_KEY_PREFIX: str = "aghub"
    API_KEY_LOOKUP_PREFIX_LENGTH: int = Field(default=8, ge=4, le=32)
    API_KEY_SECRET_BYTES: int = Field(default=32, ge=32)
    API_KEY_LAST_USED_MIN_INTERVAL_SECONDS: float = Field(default=60, ge=0)
    DEVELOPMENT_USER_ID: uuid.UUID = uuid.UUID(
        "00000000-0000-4000-8000-000000000001"
    )
    DEVELOPMENT_USER_EMAIL: str = "developer@agrihub.local"
    DEVELOPMENT_AGENT_ID: uuid.UUID = uuid.UUID(
        "00000000-0000-4000-8000-000000000002"
    )
    DEVELOPMENT_GRAPH_ID: str = "agrihub"
    STUDY_AGENT_ID: uuid.UUID = uuid.UUID(
        "00000000-0000-4000-8000-000000000010"
    )
    STUDY_GRAPH_ID: str = "agrihub_study"
    GRAPH_FIXTURE: bool = False

    model_config = _SETTINGS_CONFIG

    _blank_directories = field_validator(
        "AGRIHUB_DATA_DIR", "AGRIHUB_RUN_DIR", mode="before"
    )(_blank_path_to_none)

    @field_validator("DATABASE_URI", mode="before")
    @classmethod
    def validate_database_uri(cls, value: object) -> str | None:
        """Accept only PostgreSQL connection URLs."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            raise ValueError("DATABASE_URI must be a PostgreSQL connection URL")

        uri = value.strip()
        parsed = urlsplit(uri)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("DATABASE_URI must use postgres:// or postgresql://")
        if not parsed.hostname or parsed.path in {"", "/"}:
            raise ValueError("DATABASE_URI must include a host and database name")
        return uri

    @model_validator(mode="after")
    def require_production_database_uri(self) -> "Settings":
        """Use a local default except when production requires an explicit URL."""
        if self.DATABASE_URI is None:
            if self.ENVIRONMENT == "production":
                raise ValueError(
                    "DATABASE_URI is required when ENVIRONMENT=production"
                )
            self.DATABASE_URI = DEVELOPMENT_DATABASE_URI
        if self.ENVIRONMENT == "production":
            database_password = urlsplit(self.DATABASE_URI).password
            if (
                database_password is None
                or len(database_password) < 16
                or database_password == "agent_platform"
            ):
                raise ValueError(
                    "production DATABASE_URI must include a non-example password "
                    "of at least 16 characters"
                )
        if self.AUTH_MODE is None:
            # Tests opt into disabled auth by leaving the mode unset. Local
            # development and production both require an issued API key.
            self.AUTH_MODE = "disabled" if self.ENVIRONMENT == "test" else "api_key"
        if self.AUTH_MODE == "disabled" and self.ENVIRONMENT == "production":
            raise ValueError(
                "AUTH_MODE=disabled is not allowed when ENVIRONMENT=production"
            )
        if self.AUTH_MODE == "api_key" and (
            self.API_KEY_PEPPER is None or len(self.API_KEY_PEPPER) < 16
        ):
            raise ValueError(
                "API_KEY_PEPPER is required when AUTH_MODE=api_key "
                "and must be at least 16 characters"
            )
        if not self.API_KEY_PREFIX.isalpha() or not self.API_KEY_PREFIX.islower():
            raise ValueError("API_KEY_PREFIX must be lowercase letters")
        if not 2 <= len(self.API_KEY_PREFIX) <= 32:
            raise ValueError("API_KEY_PREFIX must be 2-32 lowercase letters")
        _resolve_agrihub_directories(self)
        return self


def get_settings() -> Settings:
    """Return settings loaded from the current environment."""
    return Settings()


def get_data_paths() -> DataPaths:
    """Return the AgriHub data and run directories without the API secrets."""
    return DataPaths()

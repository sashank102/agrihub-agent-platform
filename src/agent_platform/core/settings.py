"""Validated settings for platform-owned infrastructure."""

import uuid
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


class Settings(BaseSettings):
    """Load database configuration from the process environment."""

    ENVIRONMENT: Literal["development", "test", "production"] = "development"
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
    GRAPH_FIXTURE: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

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
        if self.AUTH_MODE is None:
            self.AUTH_MODE = (
                "api_key" if self.ENVIRONMENT == "production" else "disabled"
            )
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
        return self


def get_settings() -> Settings:
    """Return settings loaded from the current environment."""
    return Settings()

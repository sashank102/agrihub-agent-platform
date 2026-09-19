"""Validated settings for platform-owned infrastructure."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_DATABASE_URI = (
    "postgresql://agent_platform:agent_platform@localhost:5432/agent_platform"
)


class Settings(BaseSettings):
    """Load database configuration from the process environment."""

    ENVIRONMENT: Literal["development", "test", "production"] = "development"
    DATABASE_URI: str | None = Field(default=None, repr=False)

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
        return self


def get_settings() -> Settings:
    """Return settings loaded from the current environment."""
    return Settings()

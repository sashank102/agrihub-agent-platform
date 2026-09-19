import pytest
from pydantic import ValidationError

from agent_platform.core.settings import DEVELOPMENT_DATABASE_URI, Settings


def test_development_settings_have_safe_local_database_default():
    settings = Settings(
        ENVIRONMENT="development",
        DATABASE_URI=None,
        _env_file=None,
    )

    assert settings.DATABASE_URI == DEVELOPMENT_DATABASE_URI


def test_production_settings_require_explicit_database_uri():
    with pytest.raises(ValidationError, match="DATABASE_URI is required"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI=None,
            _env_file=None,
        )


def test_database_uri_must_point_to_postgresql():
    with pytest.raises(ValidationError, match="must use postgres"):
        Settings(
            DATABASE_URI="sqlite:///local.db",
            _env_file=None,
        )

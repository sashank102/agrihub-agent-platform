import pytest
from pydantic import ValidationError

from agent_platform.core.settings import (
    DEVELOPMENT_DATABASE_URI,
    LOCAL_CORS_ORIGINS,
    Settings,
)


def test_development_settings_have_safe_local_database_default():
    settings = Settings(
        ENVIRONMENT="development",
        AUTH_MODE="disabled",
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


def test_local_cors_defaults_include_both_loopback_origins():
    settings = Settings(
        ENVIRONMENT="development",
        AUTH_MODE="disabled",
        DATABASE_URI=None,
        _env_file=None,
    )

    assert list(LOCAL_CORS_ORIGINS) == [
        "http://127.0.0.1:3000",
        "http://localhost:3000",
    ]
    assert settings.API_ALLOWED_ORIGINS == list(LOCAL_CORS_ORIGINS)


def test_production_requires_api_key_mode_and_pepper():
    with pytest.raises(ValidationError, match="AUTH_MODE=disabled is not allowed"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI="postgresql://agent_platform:production-password@db.internal:5432/agent_platform",
            AUTH_MODE="disabled",
            _env_file=None,
        )
    with pytest.raises(ValidationError, match="API_KEY_PEPPER is required"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI="postgresql://agent_platform:production-password@db.internal:5432/agent_platform",
            _env_file=None,
        )


def test_production_rejects_weak_database_password():
    with pytest.raises(ValidationError, match="non-example password"):
        Settings(
            ENVIRONMENT="production",
            DATABASE_URI="postgresql://agent_platform:agent_platform@db.internal:5432/agent_platform",
            API_KEY_PEPPER="production-api-key-pepper",
            _env_file=None,
        )


def test_development_defaults_to_api_key_and_requires_a_pepper():
    with pytest.raises(ValidationError, match="API_KEY_PEPPER is required"):
        Settings(ENVIRONMENT="development", DATABASE_URI=None, _env_file=None)
    settings = Settings(
        ENVIRONMENT="development",
        DATABASE_URI=None,
        API_KEY_PEPPER="local-development-pepper",
        _env_file=None,
    )
    assert settings.AUTH_MODE == "api_key"


def test_pepper_is_excluded_from_settings_repr():
    settings = Settings(
        ENVIRONMENT="development",
        AUTH_MODE="api_key",
        API_KEY_PEPPER="pepper-value-not-logged",
        DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
        _env_file=None,
    )
    assert "pepper-value-not-logged" not in repr(settings)


def test_request_body_ceiling_defaults_to_ten_megabytes():
    settings = Settings(
        ENVIRONMENT="test",
        DATABASE_URI="postgresql://agent_platform:agent_platform@localhost:5432/agent_platform",
        _env_file=None,
    )
    assert settings.API_MAX_REQUEST_BODY_BYTES == 10_485_760


def test_database_uri_must_point_to_postgresql():
    with pytest.raises(ValidationError, match="must use postgres"):
        Settings(
            DATABASE_URI="sqlite:///local.db",
            _env_file=None,
        )

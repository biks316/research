import pytest
from pydantic import ValidationError

from app.config import Settings, format_settings_error, load_settings

REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/db",
    "REDIS_URL": "redis://localhost:6379/0",
    "SQS_PLANNING_QUEUE_URL": "http://q/planning",
    "SQS_GENERATION_QUEUE_URL": "http://q/generation",
    "S3_EXPORTS_BUCKET": "bucket",
    "JWT_ISSUER": "iss",
    "JWT_AUDIENCE": "aud",
    "JWT_SECRET": "s3cret",
}


def make(**overrides):
    values = {**REQUIRED, **overrides}
    return Settings(_env_file=None, **{k: v for k, v in values.items() if v is not None})


def test_defaults_match_env_example():
    s = make()
    assert s.API_PREFIX == "/api/v1"
    assert s.IDEMPOTENCY_TTL_SECONDS == 86400
    assert s.SQS_WAIT_TIME_SECONDS == 20
    assert s.LLM_DEFAULT_MODEL == "claude-opus-5-5"
    assert s.JWT_SECRET.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(s)


def test_missing_required_setting_reports_name(monkeypatch):
    for key in REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None)
    message = format_settings_error(exc.value)
    assert "DATABASE_URL: missing required setting" in message
    assert "S3_EXPORTS_BUCKET: missing required setting" in message


def test_load_settings_fails_fast(monkeypatch, capsys):
    monkeypatch.setattr(Settings, "model_config", {**Settings.model_config, "env_file": None})
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        load_settings()
    assert "DATABASE_URL" in capsys.readouterr().err


def test_hmac_algorithm_requires_secret(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError, match="JWT_SECRET is required"):
        make(JWT_SECRET=None)


def test_asymmetric_algorithm_requires_jwks(monkeypatch):
    monkeypatch.delenv("JWT_JWKS_URL", raising=False)
    with pytest.raises(ValidationError, match="JWT_JWKS_URL is required"):
        make(JWT_ALGORITHM="RS256")
    assert make(JWT_ALGORITHM="RS256", JWT_JWKS_URL="https://x/jwks.json").JWT_ALGORITHM == "RS256"


def test_prefix_is_normalized():
    assert make(API_PREFIX="api/v1/").API_PREFIX == "/api/v1"

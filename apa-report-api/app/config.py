"""Application settings, loaded from environment variables and `.env`."""

from __future__ import annotations

import sys
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

HMAC_ALGORITHMS = {"HS256", "HS384", "HS512"}
ASYMMETRIC_ALGORITHMS = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_ENV: Literal["development", "test", "staging", "production"] = "development"
    LOG_LEVEL: str = "INFO"
    API_PREFIX: str = "/api/v1"
    CORS_ALLOW_ORIGINS: str = ""
    PROBLEM_TYPE_BASE_URL: str = "https://api.example.com/problems"

    # Database
    DATABASE_URL: str
    DB_POOL_SIZE: int = Field(default=10, ge=1)
    DB_MAX_OVERFLOW: int = Field(default=10, ge=0)
    DB_ECHO: bool = False

    # Redis
    REDIS_URL: str

    # AWS
    AWS_REGION: str = "us-east-1"
    AWS_ENDPOINT_URL: str | None = None
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: SecretStr | None = None

    # SQS
    SQS_PLANNING_QUEUE_URL: str
    SQS_GENERATION_QUEUE_URL: str
    SQS_WAIT_TIME_SECONDS: int = Field(default=20, ge=0, le=20)
    SQS_VISIBILITY_TIMEOUT_SECONDS: int = Field(default=300, ge=30, le=43200)
    SQS_HEARTBEAT_SECONDS: int = Field(default=60, ge=1)
    SQS_MAX_RECEIVE_COUNT: int = Field(default=5, ge=1)

    # S3
    S3_EXPORTS_BUCKET: str
    S3_PRESIGN_TTL_SECONDS: int = Field(default=900, ge=1)

    # Auth
    JWT_ALGORITHM: str = "HS256"
    JWT_SECRET: SecretStr | None = None
    JWT_JWKS_URL: str | None = None
    JWT_ISSUER: str
    JWT_AUDIENCE: str
    JWT_LEEWAY_SECONDS: int = Field(default=30, ge=0)

    # Request handling
    IDEMPOTENCY_TTL_SECONDS: int = Field(default=86400, ge=1)
    MAX_ACTIVE_REPORTS_PER_USER: int = Field(default=3, ge=1)
    RATE_LIMIT_REQUESTS: int = Field(default=10, ge=1)
    RATE_LIMIT_WINDOW_SECONDS: int = Field(default=60, ge=1)
    ACTIVE_LIMIT_RETRY_AFTER_SECONDS: int = Field(default=60, ge=1)
    OUTBOX_RELAY_INTERVAL_SECONDS: float = Field(default=5, gt=0)
    OUTBOX_RELAY_BATCH_SIZE: int = Field(default=50, ge=1)
    SSE_KEEPALIVE_SECONDS: float = Field(default=15, gt=0)

    # Workers
    WORKER_CONCURRENCY: int = Field(default=4, ge=1)

    # OpenAlex
    OPENALEX_BASE_URL: str = "https://api.openalex.org"
    OPENALEX_API_KEY: SecretStr | None = None
    OPENALEX_MAILTO: str | None = None
    OPENALEX_TIMEOUT_SECONDS: float = Field(default=30, gt=0)
    OPENALEX_SEMANTIC_RPS: float = Field(default=1, gt=0)
    OPENALEX_CACHE_TTL_SECONDS: int = Field(default=86400, ge=0)
    OPENALEX_PER_PAGE: int = Field(default=25, ge=1, le=200)

    # LLM
    LLM_PROVIDER: Literal["anthropic", "fake"] = "anthropic"
    ANTHROPIC_API_KEY: SecretStr | None = None
    LLM_DEFAULT_MODEL: str = "claude-opus-5-5"
    LLM_SCREENING_MODEL: str = "claude-haiku-4-5"
    LLM_WRITING_MODEL: str = "claude-opus-5-5"
    LLM_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    LLM_TIMEOUT_SECONDS: float = Field(default=300, gt=0)
    LLM_MAX_TOKENS: int = Field(default=16000, ge=256)
    LLM_TEMPERATURE: float = Field(default=0.2, ge=0, le=1)
    LLM_MAX_CONCURRENCY: int = Field(default=4, ge=1)
    LLM_MAX_RETRIES: int = Field(default=4, ge=0)

    # Pipeline
    MIN_PAPERS_PER_SUBQUESTION: int = Field(default=5, ge=1)
    MAX_RETRIEVAL_ROUNDS: int = Field(default=3, ge=1)
    MAX_CANDIDATES_PER_SUBQUESTION: int = Field(default=40, ge=1)
    RETRIEVAL_EXPANSION_SEEDS: int = Field(default=5, ge=0)
    SCREENING_BATCH_SIZE: int = Field(default=10, ge=1)
    MIN_INCLUDED_PAPERS: int = Field(default=3, ge=1)

    @field_validator("AWS_ENDPOINT_URL", "JWT_JWKS_URL", "OPENALEX_MAILTO", "AWS_ACCESS_KEY_ID", mode="before")
    @classmethod
    def _empty_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("JWT_SECRET", "OPENALEX_API_KEY", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", mode="before")
    @classmethod
    def _empty_secret_to_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("API_PREFIX")
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        value = "/" + value.strip("/")
        return "" if value == "/" else value

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        alg = self.JWT_ALGORITHM.upper()
        if alg in HMAC_ALGORITHMS:
            if self.JWT_SECRET is None:
                raise ValueError(f"JWT_SECRET is required when JWT_ALGORITHM is {alg}")
        elif alg in ASYMMETRIC_ALGORITHMS:
            if not self.JWT_JWKS_URL:
                raise ValueError(f"JWT_JWKS_URL is required when JWT_ALGORITHM is {alg}")
        else:
            raise ValueError(f"Unsupported JWT_ALGORITHM {self.JWT_ALGORITHM!r}")
        self.JWT_ALGORITHM = alg
        if self.LLM_PROVIDER == "anthropic" and self.ANTHROPIC_API_KEY is None and self.APP_ENV == "production":
            raise ValueError("ANTHROPIC_API_KEY is required when LLM_PROVIDER=anthropic in production")
        if self.lease_seconds >= self.SQS_VISIBILITY_TIMEOUT_SECONDS:
            raise ValueError(
                "SQS_VISIBILITY_TIMEOUT_SECONDS must be greater than 2 * SQS_HEARTBEAT_SECONDS + 10 "
                "(a dead worker's lease must expire before SQS redelivers its message)"
            )
        return self

    @property
    def lease_seconds(self) -> int:
        """How long a worker's claim on a report lasts without a heartbeat."""
        return 2 * self.SQS_HEARTBEAT_SECONDS + 10

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ALLOW_ORIGINS.split(",") if o.strip()]


def format_settings_error(exc: ValidationError) -> str:
    lines = ["Invalid configuration - fix these settings (see .env.example):"]
    for err in exc.errors():
        name = ".".join(str(p) for p in err["loc"]) or "settings"
        reason = "missing required setting" if err["type"] == "missing" else err["msg"]
        lines.append(f"  - {name}: {reason}")
    return "\n".join(lines)


def load_settings() -> Settings:
    """Build settings or exit with a readable message listing every bad key."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        print(format_settings_error(exc), file=sys.stderr)
        raise SystemExit(2) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()

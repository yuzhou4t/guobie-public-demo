from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GUOBIE_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "国别智枢 API"
    environment: str = "development"
    public_api_read_only: bool = False
    public_api_key: SecretStr | None = None
    cors_allowed_origins: str = ""
    public_demo_enabled: bool = False
    public_demo_secret_key: SecretStr | None = None
    public_demo_allowed_origins: str = ""
    public_demo_cron_secret: SecretStr | None = None

    database_url: str = Field(
        default="postgresql+psycopg://guobie:guobie@localhost:5432/guobie",
        validation_alias=AliasChoices("GUOBIE_DATABASE_URL", "DATABASE_URL"),
    )
    redis_url: str = "redis://localhost:6379/0"

    s3_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "guobie"
    s3_secret_key: str = "change-me"
    s3_bucket: str = "guobie-raw"

    source_probe_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    source_probe_max_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    source_probe_max_redirects: int = Field(default=4, ge=0, le=10)
    source_probe_doh_url: str | None = None
    comtrade_subscription_key: SecretStr | None = None

    agent_runtime: str = "evidence_only"
    agent_model: str = ""
    agent_openai_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GUOBIE_AGENT_OPENAI_API_KEY", "OPENAI_API_KEY"),
    )
    agent_coze_token: SecretStr | None = None
    agent_coze_bot_id: str = ""
    agent_timeout_seconds: float = Field(default=45.0, gt=0, le=120)
    agent_max_steps: int = Field(default=6, ge=1, le=8)
    agent_codex_binary: str = "codex"
    live_search_provider: str = "disabled"
    live_search_model: str = ""
    live_search_timeout_seconds: float = Field(default=45.0, gt=0, le=120)

    field_material_storage_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / "var" / "uploads" / "field-materials"
    )
    field_material_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_driver(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return f"postgresql+psycopg://{value.removeprefix('postgres://')}"
        if value.startswith("postgresql://"):
            return f"postgresql+psycopg://{value.removeprefix('postgresql://')}"
        return value

    @field_validator(
        "public_api_key",
        "comtrade_subscription_key",
        "agent_openai_api_key",
        "agent_coze_token",
        mode="before",
    )
    @classmethod
    def normalize_empty_secret(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("agent_runtime")
    @classmethod
    def validate_agent_runtime(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"evidence_only", "codex_local", "coze_test", "openai_responses", "user_api"}
        if normalized not in allowed:
            raise ValueError(f"agent_runtime must be one of {sorted(allowed)}")
        return normalized

    @field_validator("agent_codex_binary")
    @classmethod
    def validate_agent_codex_binary(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("agent_codex_binary must be a non-empty executable path")
        return normalized

    @field_validator("live_search_provider")
    @classmethod
    def validate_live_search_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        allowed = {"disabled", "codex_local", "openai_responses", "coze_test"}
        if normalized not in allowed:
            raise ValueError(f"live_search_provider must be one of {sorted(allowed)}")
        return normalized

    @property
    def parsed_cors_allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def celery_broker_url(self) -> str:
        return self.redis_url

    @property
    def celery_result_backend(self) -> str:
        return self.redis_url


@lru_cache
def get_settings() -> Settings:
    return Settings()

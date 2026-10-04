"""Typed settings loaded from environment variables (and .env for local runs)."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Shared secret for HS256 JWTs. Same value is Cube's CUBEJS_API_SECRET.
    # No default: a default would be public, and anyone who knows it can mint admin tokens.
    jwt_secret: str = ""
    jwt_ttl_seconds: int = 900
    chainlit_auth_secret: str = ""
    # Allows the placeholder secrets from .env.example. Only safe while every port is bound to 127.0.0.1.
    dev_mode: bool = False

    # Postgres: owner DSN (seed/migrations/checkpointer) and the low-privilege reader used by run_sql.
    pg_admin_dsn: str = "postgresql://app_owner:app_owner_pw@localhost:5432/retail"
    pg_reader_dsn: str = "postgresql://mcp_reader:mcp_reader_pw@localhost:5432/retail"
    pg_audit_dsn: str = "postgresql://audit_writer:audit_writer_pw@localhost:5432/retail"
    pg_checkpoint_dsn: str = "postgresql://app_owner:app_owner_pw@localhost:5432/agent_state"

    cube_url: str = "http://localhost:4000/cubejs-api/v1"
    mcp_url: str = "http://localhost:8001/mcp"

    ollama_url: str = "http://localhost:11434"
    model_name: str = "gemma4:e2b"
    model_temperature: float = 0.0
    model_reasoning: bool = True  # Gemma 4 "thinking" mode: slower, but noticeably better tool choice
    model_num_ctx: int = 16384
    llm_input_guard: bool = False  # extra LLM classifier pass on user input (adds one model call per turn)

    policies_path: Path = ROOT / "config" / "policies.yaml"

    # Agent behavior
    max_agent_steps: int = 8
    max_sql_repairs: int = 2
    require_sql_approval: bool = True

    otel_endpoint: str | None = None  # e.g. http://localhost:6006/v1/traces (Phoenix)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def require_secret(name: str, value: str) -> str:
    """Refuse missing, short or placeholder secrets unless DEV_MODE=true. Called where a secret is
    used (not at settings load), so jobs that don't need it, like the seed, still run."""
    weak = not value or len(value) < 32 or value.startswith("change-me")
    if weak and not get_settings().dev_mode:
        raise RuntimeError(
            f"{name.upper()} is missing or a placeholder. Generate one with "
            "`python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`, "
            "or set DEV_MODE=true for a local-only demo."
        )
    return value

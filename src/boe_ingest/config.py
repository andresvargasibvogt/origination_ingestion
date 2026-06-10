"""Configuration via env vars, validated by Pydantic Settings.

All knobs that vary across environments (workspace name, throttle pace,
user-agent) live here. The Pydantic Settings model gives type validation,
defaults, and a single source of truth — no scattered `os.getenv()` calls.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable constants (not env-driven). BOE-source-specific only — shared infra
# constants (ONELAKE_ACCOUNT_URL) live in origination_common.config.
BOE_BASE_URL: str = "https://www.boe.es"
SUMARIO_API_PATH: str = "/datosabiertos/api/boe/sumario/{date}"

_DEFAULT_RELEVANCE_PATH = Path(__file__).parent / "relevance.yaml"


class Settings(BaseSettings):
    """Env-driven settings. All fields read from the process environment.

    Usage:
        settings = Settings()
        print(settings.fabric_workspace_name)

    Required env vars are only required when their value is actually used
    (e.g. FABRIC_WORKSPACE_NAME is required for OneLake writes but not
    for `--out-dir` local runs).
    """

    model_config = SettingsConfigDict(
        env_prefix="",            # no global prefix; we use explicit names below
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Fabric target — used by the promoter when copying clean blobs to OneLake.
    fabric_workspace_name: str | None = Field(default=None, alias="FABRIC_WORKSPACE_NAME")
    fabric_lakehouse_name: str = Field(default="lh_esp_origination", alias="FABRIC_LAKEHOUSE_NAME")

    # Staging Azure Storage Account (ADR-008). Ingest writes here; promoter
    # reads from here and copies clean blobs to OneLake.
    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    # User-assigned managed identity client ID for production. When set,
    # we use ManagedIdentityCredential(client_id=...) explicitly. When not
    # set, we fall back to DefaultAzureCredential (typical for local dev).
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")

    # BOE source posture (deep-dive §8 conventions).
    # Override via BOE_USER_AGENT env var if a more identifying string is wanted.
    user_agent: str = Field(default="boe-ingest/1.0", alias="BOE_USER_AGENT")
    sumario_throttle_secs: float = Field(default=1.0, alias="BOE_SUMARIO_THROTTLE_SECS", ge=0.0)
    pdf_throttle_secs: float = Field(default=2.0, alias="BOE_PDF_THROTTLE_SECS", ge=0.0)
    pdf_concurrency: int = Field(default=4, alias="BOE_PDF_CONCURRENCY", ge=1, le=32)
    http_timeout_secs: float = Field(default=30.0, alias="BOE_HTTP_TIMEOUT_SECS", gt=0.0)

    relevance_config_path: Path = Field(
        default=_DEFAULT_RELEVANCE_PATH,
        alias="BOE_RELEVANCE_CONFIG",
    )


def load_settings() -> Settings:
    """Construct a Settings instance from the current process environment."""
    return Settings()  # type: ignore[call-arg]

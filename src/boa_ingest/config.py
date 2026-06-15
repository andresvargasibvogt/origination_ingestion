"""Configuration via env vars, validated by Pydantic Settings.

Mirrors the other source configs for BOA. The staging / Fabric /
managed-identity fields are deliberately the same env-var names across every
source so one ACA Job env block applies uniformly.

BOA-specific:
  - JSON endpoint discovered via the SPA's XHR (ADR-004 Step 2 / Option B
    succeeded — no headless browser needed). The SPA appends `SECC-C=BOA` to
    the URL which makes the gateway return the SPA shell HTML; calling
    without that param returns clean JSON.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable constants (not env-driven).
BOA_BASE_URL: str = "https://www.boa.aragon.es"

# The sumario JSON endpoint. Returns ~600 KB of JSON per publication day with
# the full item list (Seccion / Subseccion / Emisor / Titulo / UrlPdf / etc.).
# Non-publication days (Sundays, holidays) return ~8 KB of SPA shell HTML
# instead of JSON — detected by the orchestrator and treated as empty.
SUMARIO_URL_TEMPLATE: str = (
    "/cgi-bin/EBOA/BRSCGI"
    "?CMD=VERLST"
    "&BASE=BOLE"
    "&DOCS=1-250"
    "&SEC=OPENDATABOAJSONAPP"
    "&OUTPUTMODE=JSON"
    "&SORT=-PUBL"
    "&SEPARADOR="
    "&PUBL-C={date}"
)

# The server serves the body as ISO-8859-1 (Latin-1) regardless of the
# `Content-Type: text/html; charset=ISO-8859-1` header (which mislabels JSON
# as HTML). Decode bytes with this encoding before json.loads.
BOA_RESPONSE_ENCODING: str = "iso-8859-1"

_DEFAULT_RELEVANCE_PATH = Path(__file__).parent / "relevance.yaml"


class Settings(BaseSettings):
    """Env-driven settings for the BOA loader.

    Shared env names with the BOE loader: FABRIC_*, STG_*, AZURE_CLIENT_ID.
    BOA-specific knobs have a BOA_ prefix so the two never collide.
    """

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Fabric target — used by the promoter when copying clean blobs to OneLake.
    # (Identical to BOE; promoter is shared.)
    fabric_workspace_name: str | None = Field(default=None, alias="FABRIC_WORKSPACE_NAME")
    fabric_lakehouse_name: str = Field(default="lh_esp_origination", alias="FABRIC_LAKEHOUSE_NAME")

    # Staging Azure Storage Account (ADR-008). Same account as BOE.
    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    # Managed identity for production. Same UAMI as BOE (`id-origination`).
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")

    # BOA-specific posture. User-Agent: the BOA deep-dive notes that the
    # endpoint resets connections without a Mozilla-style UA, so the default
    # below is intentionally browser-like.
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; iBVogt-DataPlatform; +mailto:Andres.Vargas@ibvogt.com)",
        alias="BOA_USER_AGENT",
    )
    sumario_throttle_secs: float = Field(default=1.0, alias="BOA_SUMARIO_THROTTLE_SECS", ge=0.0)
    pdf_throttle_secs: float = Field(default=2.0, alias="BOA_PDF_THROTTLE_SECS", ge=0.0)
    pdf_concurrency: int = Field(default=2, alias="BOA_PDF_CONCURRENCY", ge=1, le=32)
    http_timeout_secs: float = Field(default=30.0, alias="BOA_HTTP_TIMEOUT_SECS", gt=0.0)

    relevance_config_path: Path = Field(
        default=_DEFAULT_RELEVANCE_PATH,
        alias="BOA_RELEVANCE_CONFIG",
    )


def load_settings() -> Settings:
    """Construct a Settings instance from the current process environment."""
    return Settings()  # type: ignore[call-arg]

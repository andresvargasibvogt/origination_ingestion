"""Configuration for the e-distribución (Grupo Endesa) poller.

Mirrors the REE poller: a monthly CSV published on an uncertain day, so the
Job polls daily and the orchestrator is a no-op until a genuinely new month
appears. Shares the infra env-var names (FABRIC_*, STG_*, AZURE_CLIENT_ID)
with the other sources so one ACA Job env block applies uniformly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable constants (not env-driven).
ENDESA_BASE_URL: str = "https://www.edistribucion.com"

# The public "capacidad de generación" landing page. Plain server-rendered HTML
# (no SPA) — it lists each month's downloads as static /content/dam/ hrefs, so a
# single HTTP GET is enough to discover the latest file.
LANDING_PAGE_PATH: str = "/es/red-electrica/nodos-capacidad-red/capacidad-generacion.html"

# The page publishes TWO parallel monthly CSV series with similar filenames:
#   - "Capacidad de generación en e-distribución" (the large network dataset) ← we want this
#   - "Capacidad de generación en EASA"            (a separate, tiny dataset)  ← excluded
# They are distinguished by the visible link text, not reliably by filename (the
# internal R-code, e.g. R1299 vs R1026, is stable today but is a document id we
# don't want to hard-depend on). We select by this normalized text fragment.
SERIES_TEXT_MARKER: str = "e-distribuci"   # appears in the wanted series' link text
SERIES_EXCLUDE_MARKER: str = "easa"        # marks the other series (defensive)


class Settings(BaseSettings):
    """Env-driven settings for the e-distribución poller."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Fabric target — used for the OneLake dedup existence check and (in
    # direct/local modes) for writing. Same names as the other sources.
    fabric_workspace_name: str | None = Field(default=None, alias="FABRIC_WORKSPACE_NAME")
    fabric_lakehouse_name: str = Field(default="lh_esp_origination", alias="FABRIC_LAKEHOUSE_NAME")

    # Staging Azure Storage Account (ADR-008). Same account as BOE/BOA/REE.
    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    # Managed identity for production. Same UAMI as the other sources (`id-origination`).
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")

    # Posture. Mozilla-style, good-faith bot identification, no PII (robots.txt
    # is `User-agent: * -> Allow: /`, so this UA may fetch the downloads).
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; iBVogt-DataPlatform)",
        alias="ENDESA_USER_AGENT",
    )
    http_timeout_secs: float = Field(default=60.0, alias="ENDESA_HTTP_TIMEOUT_SECS", gt=0.0)


def load_settings() -> Settings:
    """Construct a Settings instance from the current process environment."""
    return Settings()  # type: ignore[call-arg]

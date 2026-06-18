"""Configuration for the REE poller.

Shares the infra env-var names (FABRIC_*, STG_*, AZURE_CLIENT_ID) with the
other sources so one ACA Job env block applies uniformly.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable constants (not env-driven).
REE_BASE_URL: str = "https://www.ree.es"

# The public "Conoce la capacidad de acceso" landing page. It lists the latest
# CSV/PDF/XLSX as static hrefs (server-rendered HTML, no SPA) — we scrape the
# href rather than guessing the filename's date component.
LANDING_PAGE_PATH: str = "/es/clientes/generador/acceso-conexion/conoce-la-capacidad-de-acceso"

# The target file is the generation capacity table at transport-grid nodes,
# published monthly. Filenames embed the publication date: e.g.
# /sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv
# We want the CSV variant (the page also offers PDF + XLSX of the same data).
TARGET_FILENAME_SUFFIX: str = "_GRT_generacion.csv"


class Settings(BaseSettings):
    """Env-driven settings for the REE poller."""

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

    # Staging Azure Storage Account (ADR-008). Same account as BOE/BOA.
    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    # Managed identity for production. Same UAMI as BOE/BOA (`id-origination`).
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")

    # REE posture. ree.es resets the connection without a browser-like UA.
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; iBVogt-DataPlatform)",
        alias="REE_USER_AGENT",
    )
    http_timeout_secs: float = Field(default=60.0, alias="REE_HTTP_TIMEOUT_SECS", gt=0.0)


def load_settings() -> Settings:
    """Construct a Settings instance from the current process environment."""
    return Settings()  # type: ignore[call-arg]

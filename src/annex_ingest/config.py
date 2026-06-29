"""Configuration for the annex acquisition tier.

Shares the infra env-var names (FABRIC_*, STG_*, AZURE_CLIENT_ID) with the
other sources so one ACA Job env block applies uniformly. Annexes ride the
existing `boe` source segment (they land under bronze/boe/annexes/), so no new
manifest.Source value is needed.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable constants (not env-driven).
BOE_BASE_URL: str = "https://www.boe.es"
# BOE serves each announcement as XML; the announcement text (with the annex
# links) lives in the XML body. We reuse the per-item `url_xml` recorded in the
# BOE manifest, or build this for ad-hoc --announcement runs.
BOE_XML_PATH: str = "/diario_boe/xml.php?id={identifier}"

ALMACEN_BASE_URL: str = "https://almacen.redsara.es"
# Confirmed public, anonymous JSON API (the SPA's backend). The list endpoint
# returns {"status":"success","data":{"id","expirationDate","files":[...]}}.
ALMACEN_LIST_PATH: str = "/api/v1/sending/public/{uuid}"
ALMACEN_ATTACHMENT_PATH: str = "/api/v1/sending/public/{uuid}/attachment/{file_id}"


class Settings(BaseSettings):
    """Env-driven settings for the annex tier."""

    model_config = SettingsConfigDict(
        env_prefix="",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Fabric target — used to read the day's promoted BOE manifest, for the
    # OneLake dedup existence check, and for writing the JSONL state manifest.
    fabric_workspace_name: str | None = Field(default=None, alias="FABRIC_WORKSPACE_NAME")
    fabric_lakehouse_name: str = Field(default="lh_esp_origination", alias="FABRIC_LAKEHOUSE_NAME")

    # Staging Azure Storage Account (ADR-008). Same account as the gazette sources.
    stg_account_name: str | None = Field(default=None, alias="STG_ACCOUNT_NAME")
    stg_container_untrusted: str = Field(default="untrusted", alias="STG_CONTAINER_UNTRUSTED")
    stg_container_quarantine: str = Field(default="quarantine", alias="STG_CONTAINER_QUARANTINE")

    # Managed identity for production. Same UAMI as the other sources.
    azure_client_id: str | None = Field(default=None, alias="AZURE_CLIENT_ID")

    # Posture. almacen.redsara.es exposes a documented public API; the UA is a
    # good-faith, no-PII identifier (see [[deploy-image-and-jobs-reality]] sibling sources).
    user_agent: str = Field(
        default="Mozilla/5.0 (compatible; iBVogt-DataPlatform)",
        alias="ALMACEN_USER_AGENT",
    )
    # Large files (up to ~880 MB) → generous timeout and modest concurrency.
    http_timeout_secs: float = Field(default=300.0, alias="ALMACEN_HTTP_TIMEOUT_SECS", gt=0.0)
    throttle_secs: float = Field(default=1.0, alias="ALMACEN_THROTTLE_SECS", ge=0.0)
    concurrency: int = Field(default=2, alias="ALMACEN_CONCURRENCY", ge=1, le=8)
    chunk_bytes: int = Field(default=8 * 1024 * 1024, alias="ALMACEN_CHUNK_BYTES", ge=64 * 1024)
    # 0 = unlimited (user decision: download every linked file). A positive value
    # caps per-file size (files above it are recorded `skipped_too_large`) — used
    # in dry-runs / cost control, off by default.
    max_file_bytes: int = Field(default=0, alias="ALMACEN_MAX_FILE_BYTES", ge=0)
    # Default backfill window (≈ the portal link-expiry horizon).
    backfill_lookback_days: int = Field(default=90, alias="ANNEX_BACKFILL_LOOKBACK_DAYS", ge=1)


def load_settings() -> Settings:
    """Construct a Settings instance from the current process environment."""
    return Settings()  # type: ignore[call-arg]

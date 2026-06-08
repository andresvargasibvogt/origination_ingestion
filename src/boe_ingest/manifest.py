"""Manifest models (Pydantic v2).

The manifest is the stable contract between this loader and any downstream
consumer (ADR-005). Schema version bumps are deliberate and reviewed.

Pydantic gives us:
  - Runtime validation on construction
  - Auto JSON Schema export via Manifest.model_json_schema()
  - Round-trip serialisation with model_dump_json()
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION: Literal["1.0"] = "1.0"
SOURCE: Literal["boe"] = "boe"
ATTRIBUTION: str = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"


class _Frozen(BaseModel):
    """Base config for frozen, strict models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ItemEntry(_Frozen):
    """One row in the manifest's `items` list — represents one landed PDF."""

    identifier: str = Field(description="BOE-A-YYYY-N disposition identifier")
    section: str = Field(description="BOE section codigo (e.g. 'III', 'V')")
    departamento_codigo: str = Field(description="Departamento codigo (e.g. '9575' for MITECO)")
    departamento: str = Field(description="Departamento full name as the sumario reports it")
    published_at: str = Field(description="ISO date (YYYY-MM-DD)")
    url_pdf: str | None = None
    url_xml: str | None = None
    url_html: str | None = None
    eli: str | None = None
    pdf_path: str = Field(description="Lakehouse-relative path under Files/")
    sha256: str = Field(min_length=64, max_length=64)
    size_bytes: int = Field(ge=0)


class FailedItem(_Frozen):
    identifier: str
    reason: str


class RunInfo(BaseModel):
    """Run telemetry — non-frozen so we can mutate counters during execution."""

    model_config = ConfigDict(extra="forbid")

    started_at: str
    ended_at: str
    date: str = Field(description="Target ingestion date (ISO YYYY-MM-DD)")
    sumario_items_total: int = Field(ge=0)
    items_filtered_in: int = Field(ge=0)
    items_written: int = Field(ge=0)
    items_failed: list[FailedItem] = Field(default_factory=list)
    items_robots_blocked: int = Field(default=0, ge=0)
    attribution: str = ATTRIBUTION


class Manifest(BaseModel):
    """The full _manifest.json payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    source: Literal["boe"] = SOURCE
    run: RunInfo
    items: list[ItemEntry]

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_iso() -> str:
    """Wall-clock UTC ISO-8601 with seconds precision (e.g. '2026-06-02T07:01:42Z')."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )

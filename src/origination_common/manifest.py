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

# Source identifiers used in both the manifest payload and the OneLake bronze
# path (bronze/{source}/raw/...). Widen this literal when a new source comes
# online; the promoter discovers the source from each blob's path at runtime.
Source = Literal["boe", "boa", "ree"]
SOURCE_BOE: Source = "boe"
SOURCE_BOA: Source = "boa"
SOURCE_REE: Source = "ree"

# Backward-compat alias. Prefer the explicit SOURCE_* constants in new code.
SOURCE: Source = SOURCE_BOE

# Per-source attribution strings (PSI / CC BY 4.0 obligation).
ATTRIBUTION_BOE: str = "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"
ATTRIBUTION_BOA: str = "Fuente de los datos: Gobierno de Aragón — Boletín Oficial de Aragón"
ATTRIBUTION_REE: str = "Fuente de los datos: Red Eléctrica de España (REE)"

# Backward-compat alias for BOE callers.
ATTRIBUTION: str = ATTRIBUTION_BOE

_ATTRIBUTION_BY_SOURCE: dict[str, str] = {
    SOURCE_BOE: ATTRIBUTION_BOE,
    SOURCE_BOA: ATTRIBUTION_BOA,
    SOURCE_REE: ATTRIBUTION_REE,
}


def attribution_for(source: Source) -> str:
    """Return the attribution string for a given source."""
    try:
        return _ATTRIBUTION_BY_SOURCE[source]
    except KeyError:
        raise ValueError(f"Unknown source: {source!r}") from None


class _Frozen(BaseModel):
    """Base config for frozen, strict models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ItemEntry(_Frozen):
    """One row in the manifest's `items` list — represents one landed PDF."""

    identifier: str = Field(description="Disposition identifier (BOE-A-YYYY-N for BOE; MLKOB for BOA)")
    section: str = Field(description="Section código (e.g. 'III', 'V')")
    subsection: str | None = Field(
        default=None,
        description="Subsection inside a section (BOA only — 'a', 'b', 'c'). None for BOE.",
    )
    departamento_codigo: str = Field(description="Departamento código (BOE) or empty (BOA, departamento name is canonical)")
    departamento: str = Field(description="Departamento full name as the source reports it")
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
    attribution: str = ATTRIBUTION_BOE


class Manifest(BaseModel):
    """The full _manifest.json payload."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    source: Source = SOURCE_BOE
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

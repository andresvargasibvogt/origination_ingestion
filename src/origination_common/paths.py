"""Lakehouse-relative path generation.

All paths produced here are RELATIVE to the lakehouse's `Files/` root. The
writer (`onelake.py` or `LocalWriter`) prepends the workspace / lakehouse
prefix when actually writing. This keeps the manifest portable across
workspaces — the manifest's `pdf_path` field is the value these functions
return.

Source-parametric and granularity-parametric:
  - `source` selects the bronze/{source}/raw root (default BOE).
  - `granularity` selects the partition depth:
      "day"   → year=YYYY/month=MM/day=DD   (daily sources: BOE, BOA)
      "month" → year=YYYY/month=MM           (monthly sources: REE)
    A monthly source publishes one file per month, so a day= partition would
    just scatter single files into otherwise-empty day folders — month-level
    is the right granularity for it.

See ADR-005 for the medallion layout contract.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from .manifest import SOURCE_BOE, Source

Granularity = Literal["day", "month"]


def bronze_root(source: Source = SOURCE_BOE) -> str:
    return f"bronze/{source}/raw"


def _partition_suffix(target_date: date, granularity: Granularity) -> str:
    suffix = f"year={target_date.year:04d}/month={target_date.month:02d}"
    if granularity == "day":
        suffix += f"/day={target_date.day:02d}"
    return suffix


def partition_dir(
    target_date: date,
    source: Source = SOURCE_BOE,
    granularity: Granularity = "day",
) -> str:
    """Hive-style partition path under the source's raw/ root."""
    return f"{bronze_root(source)}/{_partition_suffix(target_date, granularity)}"


def manifests_partition_dir(
    target_date: date,
    source: Source = SOURCE_BOE,
    granularity: Granularity = "day",
) -> str:
    """Hive-style partition path under _manifests/."""
    return f"{bronze_root(source)}/_manifests/{_partition_suffix(target_date, granularity)}"


def pdf_path(target_date: date, identifier: str, source: Source = SOURCE_BOE) -> str:
    return f"{partition_dir(target_date, source)}/{identifier}.pdf"


def manifest_path(
    target_date: date,
    source: Source = SOURCE_BOE,
    granularity: Granularity = "day",
) -> str:
    return f"{manifests_partition_dir(target_date, source, granularity)}/_manifest.json"

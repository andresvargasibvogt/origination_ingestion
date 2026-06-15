"""Partition-path granularity tests.

Locks in the contract that daily sources (BOE/BOA) get day= partitions and
the monthly source (REE) gets month-level partitions with no day= folder.
A regression here would scatter REE's monthly file into empty day folders or
break the promoter's path round-trip.
"""

from __future__ import annotations

from datetime import date

from origination_common.manifest import SOURCE_BOA, SOURCE_BOE, SOURCE_REE
from origination_common.paths import manifest_path, partition_dir, pdf_path

D = date(2026, 6, 4)


def test_daily_sources_keep_day_partition() -> None:
    assert partition_dir(D, SOURCE_BOE) == "bronze/boe/raw/year=2026/month=06/day=04"
    assert partition_dir(D, SOURCE_BOA) == "bronze/boa/raw/year=2026/month=06/day=04"
    # Default granularity is "day" — BOE/BOA callers pass no granularity.
    assert pdf_path(D, "BOE-A-2026-1", SOURCE_BOE) == (
        "bronze/boe/raw/year=2026/month=06/day=04/BOE-A-2026-1.pdf"
    )
    assert manifest_path(D, SOURCE_BOE).endswith("/day=04/_manifest.json")


def test_ree_uses_month_partition_no_day() -> None:
    p = partition_dir(D, SOURCE_REE, granularity="month")
    assert p == "bronze/ree/raw/year=2026/month=06"
    assert "day=" not in p
    m = manifest_path(D, SOURCE_REE, granularity="month")
    assert m == "bronze/ree/raw/_manifests/year=2026/month=06/_manifest.json"
    assert "day=" not in m


def test_explicit_day_granularity_matches_default() -> None:
    assert partition_dir(D, SOURCE_BOE, granularity="day") == partition_dir(D, SOURCE_BOE)

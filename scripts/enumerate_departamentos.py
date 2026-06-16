"""Read-only derivation helper for the historical backfill filter.

Enumerates, across a span of years, the distinct departamento / ministry
entities that appear in the sections we care about — so the backfill filter is
built from OBSERVED entities, not guesses. Sumario-only (no PDF downloads).

Department/ministry names are piecewise-constant (they change only on government
reorganizations), so monthly sampling catches every name that ever existed.

Usage:
    uv run python scripts/enumerate_departamentos.py boe 2019 2025
    uv run python scripts/enumerate_departamentos.py boa 2019 2025

Output: a table of (entity → sections seen → first-seen → last-seen → #samples),
which a human then classifies into the renewable lineage and writes into the
relevant relevance.backfill.yaml.
"""

from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field

import httpx


@dataclass
class Seen:
    sections: set[str] = field(default_factory=set)
    first: str = ""
    last: str = ""
    samples: int = 0

    def add(self, section: str, date_iso: str) -> None:
        self.sections.add(section)
        self.first = date_iso if not self.first else min(self.first, date_iso)
        self.last = max(self.last, date_iso)
        self.samples += 1


def _sample_dates(start_year: int, end_year: int) -> list[str]:
    """One probe date per month (the 15th) across the span, descending."""
    dates: list[str] = []
    for y in range(end_year, start_year - 1, -1):
        for m in range(12, 0, -1):
            dates.append(f"{y}{m:02d}15")
    return dates


async def _enumerate_boe(years: tuple[int, int]) -> dict[str, Seen]:
    from boe_ingest.config import BOE_BASE_URL
    from boe_ingest.sumario import EmptyDay, fetch_sumario, walk_items

    catalog: dict[str, Seen] = defaultdict(Seen)
    async with httpx.AsyncClient(
        base_url=BOE_BASE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; iBVogt-DataPlatform)"},
        timeout=30.0, http2=True, follow_redirects=True,
    ) as client:
        for ymd in _sample_dates(*years):
            date_iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            try:
                sumario = await fetch_sumario(client, ymd)
            except (EmptyDay, Exception):
                continue
            for _item, dept, seccion in walk_items(sumario):
                codigo = str(dept.get("codigo", "")).strip()
                nombre = str(dept.get("nombre", "")).strip()
                # Only the sections the filter cares about (III + V.*)
                if not (seccion == "3" or seccion.startswith("5")):
                    continue
                key = f"[{codigo}] {nombre}"
                catalog[key].add(seccion, date_iso)
            await asyncio.sleep(0.3)
    return catalog


async def _enumerate_boa(years: tuple[int, int]) -> dict[str, Seen]:
    import json

    from boa_ingest.config import BOA_BASE_URL, BOA_RESPONSE_ENCODING, SUMARIO_URL_TEMPLATE

    catalog: dict[str, Seen] = defaultdict(Seen)
    async with httpx.AsyncClient(
        base_url=BOA_BASE_URL,
        headers={"User-Agent": "Mozilla/5.0 (compatible; iBVogt-DataPlatform)"},
        timeout=30.0, http2=True, follow_redirects=True,
    ) as client:
        for ymd in _sample_dates(*years):
            date_iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"
            try:
                resp = await client.get(
                    SUMARIO_URL_TEMPLATE.format(date=ymd),
                    headers={"Accept": "application/json,text/plain,*/*"},
                )
                body = resp.content
                if not body.lstrip().startswith(b"["):
                    continue
                items = json.loads(body.decode(BOA_RESPONSE_ENCODING))
            except Exception:
                continue
            for it in items:
                seccion = str(it.get("Seccion", ""))
                sub = str(it.get("Subseccion", ""))
                emisor = str(it.get("Emisor", "")).strip()
                # Section V only (where anuncios live)
                if not seccion.startswith("V"):
                    continue
                key = f"{emisor}"
                catalog[key].add(f"{seccion} / {sub}".strip(" /"), date_iso)
            await asyncio.sleep(0.3)
    return catalog


def _print(catalog: dict[str, Seen]) -> None:
    rows = sorted(catalog.items(), key=lambda kv: (kv[1].first, kv[0]))
    print(f"\n{'entity':70}  {'first':10}  {'last':10}  {'n':>3}  sections")
    print("-" * 120)
    for key, s in rows:
        print(f"{key[:70]:70}  {s.first:10}  {s.last:10}  {s.samples:>3}  {sorted(s.sections)}")
    print(f"\n{len(rows)} distinct entities observed (monthly sampling).")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    source, sy, ey = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    if source == "boe":
        catalog = asyncio.run(_enumerate_boe((sy, ey)))
    elif source == "boa":
        catalog = asyncio.run(_enumerate_boa((sy, ey)))
    else:
        print("source must be boe|boa"); return 2
    _print(catalog)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

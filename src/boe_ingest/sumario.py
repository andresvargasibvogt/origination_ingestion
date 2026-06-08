"""Fetch + walk the BOE Datos Abiertos daily sumario.

The sumario API returns a tree of sección → departamento → (epígrafe →)? item.
`walk_items()` flattens it into (item, departamento, seccion_codigo) tuples
so the caller can apply the relevance filter at the item level.

Per the deep-dive §8: each item carries its resolved url_pdf / url_xml /
url_html — we don't construct URLs ourselves.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import structlog

from .config import SUMARIO_API_PATH

log = structlog.get_logger()


class EmptyDay(Exception):
    """Raised when BOE returns 404 for a date (Sundays, holidays)."""


class SumarioError(Exception):
    """Raised on an operational error inside an HTTP-200 sumario response."""


async def fetch_sumario(client: httpx.AsyncClient, date_yyyymmdd: str) -> dict[str, Any]:
    """Fetch the daily sumario for `date_yyyymmdd` (e.g. '20260527').

    Validates both the HTTP status AND the inner `status.code` per the BOE
    deep-dive §8 — a 200 can carry a body-level error.
    """
    url = SUMARIO_API_PATH.format(date=date_yyyymmdd)
    log.info("sumario_fetch_start", url=url)

    resp = await client.get(url, headers={"Accept": "application/json"})
    if resp.status_code == 404:
        raise EmptyDay(f"sumario 404 for {date_yyyymmdd}")
    resp.raise_for_status()

    data = resp.json()
    _validate_inner_status(data)
    log.info("sumario_fetch_ok", date=date_yyyymmdd, bytes=len(resp.content))
    return data


def _validate_inner_status(data: dict[str, Any]) -> None:
    """The sumario can return HTTP 200 with a body-level error.

    Body shape (JSON): {"data": {"sumario": {"metadatos": ..., "diario": ...}, ...},
                        "status": {"code": "200", "text": "..."}}
    Some responses nest status under 'data.sumario' instead.
    """
    # Top-level status
    status = data.get("status")
    if isinstance(status, dict) and str(status.get("code")) != "200":
        raise SumarioError(f"sumario operational error: {status}")
    # Some payloads carry the status nested under data.sumario
    nested = data.get("data", {}).get("sumario", {}).get("status")
    if isinstance(nested, dict) and str(nested.get("code")) != "200":
        raise SumarioError(f"sumario operational error: {nested}")


def walk_items(sumario: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any], str]]:
    """Yield `(item, departamento, seccion_codigo)` for every item in the sumario.

    Items can appear either directly under `departamento.item[*]` OR nested
    under `departamento.epigrafe[*].item[*]` — we handle both.
    """
    sumario_root = sumario.get("data", {}).get("sumario", {})
    diario_list = _as_list(sumario_root.get("diario"))

    for diario in diario_list:
        secciones = _as_list(diario.get("seccion"))
        for seccion in secciones:
            seccion_codigo = str(seccion.get("codigo", ""))
            departamentos = _as_list(seccion.get("departamento"))
            for dept in departamentos:
                for item in _items_in_departamento(dept):
                    yield item, dept, seccion_codigo


def _items_in_departamento(departamento: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(_as_list(departamento.get("item")))
    for epigrafe in _as_list(departamento.get("epigrafe")):
        items.extend(_as_list(epigrafe.get("item")))
    return items


def _as_list(maybe_list: Any) -> list[Any]:
    """Sumario fields are sometimes a single dict, sometimes a list."""
    if maybe_list is None:
        return []
    if isinstance(maybe_list, list):
        return maybe_list
    return [maybe_list]


def total_items(sumario: dict[str, Any]) -> int:
    return sum(1 for _ in walk_items(sumario))

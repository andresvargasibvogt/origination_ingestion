"""Fetch the daily BOA sumario JSON.

Discovered endpoint (ADR-004 Step 2 / Option B succeeded):

    GET /cgi-bin/EBOA/BRSCGI
        ?CMD=VERLST&BASE=BOLE&DOCS=1-250
        &SEC=OPENDATABOAJSONAPP&OUTPUTMODE=JSON&SORT=-PUBL
        &SEPARADOR=&PUBL-C=YYYYMMDD

No auth, no cookies, no XHR header dance. The SPA itself appends `SECC-C=BOA`
which triggers a server-side redirect to the SPA shell — *omitting* that
parameter is the trick that lets us call the endpoint as a normal HTTP client.

Two complications worth flagging:

  1. The server returns `Content-Type: text/html; charset=ISO-8859-1` even
     when the body is JSON. We sniff the body to tell them apart.
  2. The body is genuinely ISO-8859-1 (Latin-1) — not UTF-8 mislabelled.
     Spanish characters like 'ó' arrive as the single byte 0xF3, not as the
     UTF-8 2-byte sequence 0xC3 0xB3. Decode before json.loads.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from .config import BOA_RESPONSE_ENCODING, SUMARIO_URL_TEMPLATE

log = structlog.get_logger()


class EmptyDay(Exception):
    """Raised when BOA returns the SPA shell instead of JSON (no publication
    that day — Sundays, holidays, future dates).
    """


class SumarioError(Exception):
    """Raised on a malformed sumario response we don't know how to recover from."""


async def fetch_sumario(client: httpx.AsyncClient, date_yyyymmdd: str) -> list[dict[str, Any]]:
    """Fetch BOA's daily JSON sumario as a flat list of item dicts.

    Each item has the keys documented in `boa-dataset-deep-dive.html` §sumario:
        NOrden, DOCN, FechaPublicacion, Numeroboletin,
        Seccion, Subseccion, Fechadisposicion, Rango,
        Emisor, Titulo, Texto, CodigoMateria, UrlPdf, UrlBCOM

    Raises:
        EmptyDay: BOA didn't publish on `date_yyyymmdd`.
        SumarioError: Response is unparseable JSON despite looking JSON-ish.
        httpx.HTTPStatusError: Underlying HTTP error from BOA's gateway.
    """
    url = SUMARIO_URL_TEMPLATE.format(date=date_yyyymmdd)
    log.info("boa_sumario_fetch_start", url=url)

    resp = await client.get(url, headers={"Accept": "application/json,text/plain,*/*"})
    resp.raise_for_status()

    body = resp.content
    # Empty-day detection: BOA returns the SPA shell HTML (≈8 KB) for dates
    # with no publication. The JSON response always starts with '['.
    if not body.lstrip().startswith(b"["):
        log.info("boa_sumario_empty_day", date=date_yyyymmdd, bytes=len(body))
        raise EmptyDay(f"BOA returned non-JSON for {date_yyyymmdd} (likely no publication)")

    try:
        items = json.loads(body.decode(BOA_RESPONSE_ENCODING))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SumarioError(f"Failed to parse BOA sumario for {date_yyyymmdd}: {exc}") from exc

    if not isinstance(items, list):
        raise SumarioError(
            f"BOA sumario for {date_yyyymmdd} did not return a JSON list; got {type(items).__name__}"
        )

    log.info("boa_sumario_fetch_ok", date=date_yyyymmdd, bytes=len(body), items=len(items))
    return items


def extract_pdf_url(item: dict[str, Any]) -> str | None:
    """Extract the PDF URL from a BOA item's `UrlPdf` field.

    BOA's payload format wraps each URL in backticks AND uses an acute accent
    (´, U+00B4) as a separator between alternates. A real example:

        `https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI?CMD=VEROBJ&MLKOB=12345´`https://...

    Both `` ` `` and `´` are URL terminators in this format — we strip both
    from each end of the candidate URL.
    """
    raw = item.get("UrlPdf")
    if not isinstance(raw, str) or not raw:
        return None
    # Strip leading wrappers.
    s = raw.lstrip("`").lstrip("´")
    # Find the earliest terminator in `s` and cut.
    cut = len(s)
    for terminator in ("`", "´"):
        idx = s.find(terminator)
        if idx >= 0 and idx < cut:
            cut = idx
    s = s[:cut].strip()
    return s or None


def extract_mlkob(pdf_url: str) -> str | None:
    """Extract the MLKOB identifier from a `BRSCGI?CMD=VEROBJ&MLKOB=...` URL.

    Defensive against trailing acute accents / backticks that may leak in
    if `extract_pdf_url` cleaning is bypassed.
    """
    if not pdf_url:
        return None
    marker = "MLKOB="
    i = pdf_url.find(marker)
    if i < 0:
        return None
    rest = pdf_url[i + len(marker):]
    # Stop at next & or wrapper char or whitespace.
    for terminator in ("&", "`", "´", " ", "\t"):
        j = rest.find(terminator)
        if j >= 0:
            rest = rest[:j]
    return rest.strip() or None

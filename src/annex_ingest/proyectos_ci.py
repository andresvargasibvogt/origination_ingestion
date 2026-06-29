"""Resolve annexes hosted behind the Delegaciones del Gobierno 'proyectos-ci'
portals (mpt / mptfp / mptmd.gob.es) to their underlying almacen.redsara.es link.

Many BOE announcements don't link almacen directly — they link the *generic
regional* proyectos-ci portal. The actual dossier still lives on almacen; the
per-project portal page carries the almacen link. So this is a DISCOVERY hop,
not a new downloader: announcement → regional listing → match the project (by
expediente code, name fallback) → project page → almacen link → (reuse the
almacen client).

Best-effort by design (per-region variance): every portal page also embeds the
full site nav, so we only consider anchors under the portal's own region path;
matching prefers the expediente code and falls back to project-name token
overlap; if the landing page isn't the project list we follow one hop. When we
can't resolve, we say so (`unresolved`) or detect office-only (`office_only`)
rather than guessing.
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from typing import Literal
from urllib.parse import urljoin, urlparse

import httpx
import structlog
from pydantic import BaseModel

log = structlog.get_logger()

PORTAL_RE = re.compile(r"https?://(?:www\.)?(?:mpt|mptfp|mptmd)\.gob\.es/[^\s\"'<]+", re.I)
_ALMACEN_RE = re.compile(
    r"almacen\.redsara\.es/sending/public/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)
_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_EXP_RE = re.compile(r"\b(?:PE[oó]l[- ]?)?[A-Z]{2,5}-?\d{2,4}(?:-[A-Z]{2,4})?\b")
_STOPWORDS = set(
    ["ANUNCIO", "AREA", "FUNCIONAL", "DEPENDENCIA", "INDUSTRIA", "ENERGIA", "SUBDELEGACION", "DELEGACION", "GOBIERNO", "SOLICITUD", "INFORMACION", "PUBLICA", "AUTORIZACION", "ADMINISTRATIVA", "CONSTRUCCION", "PREVIA", "PROYECTO", "PROVINCIA", "TERMINO", "MUNICIPAL", "POTENCIA", "INSTALADA", "MODULO", "GENERACION", "PARQUE", "EOLICO", "FOTOVOLTAICA", "SOLAR", "HIBRIDACION", "EXISTENTE", "INFRAESTRUCTURA", "EVACUACION", "DECLARACION", "IMPACTO", "AMBIENTAL", "UTILIDAD", "PUBLICA", "SOMETE", "SOLICITA", "MEDIANTE", "DENOMINADO", "DENOMINADA", "PROYECTOS"]
)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper().replace("_", "-")


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[A-Z0-9]{4,}", _norm(s)) if w not in _STOPWORDS}


def extract_portal_url(xml_text: str) -> str | None:
    m = PORTAL_RE.search(xml_text)
    return m.group(0) if m else None


def extract_expedientes(title: str, body: str) -> set[str]:
    """Candidate expediente codes (e.g. FV-168-ALM, PFOT-761, ALM-180)."""
    cands = {_norm(x) for x in _EXP_RE.findall(_norm(title + " " + body))}
    # keep code-like tokens with a digit and enough length; drop year/NIF noise
    return {c for c in cands if any(ch.isdigit() for ch in c) and len(c) >= 5 and not c.startswith(("B-20", "A-20"))}


def name_tokens(title: str) -> set[str]:
    return _tokens(title)


class ResolveResult(BaseModel):
    status: Literal["resolved", "office_only", "unresolved", "no_portal"]
    almacen_uuids: tuple[str, ...] = ()
    project_url: str | None = None
    matched_by: str | None = None


def _anchors(page: str, base_url: str) -> list[tuple[str, str]]:
    """All anchors as (absolute_url, text)."""
    return [
        (urljoin(base_url, href), _html.unescape(_TAG_RE.sub(" ", inner)).strip())
        for href, inner in _ANCHOR_RE.findall(page)
    ]


def _region_anchors(anchors: list[tuple[str, str]], base_url: str) -> list[tuple[str, str]]:
    """Anchors under the portal's own region path (content area, not the global nav)."""
    base_path = urlparse(base_url).path.rstrip("/")
    out = []
    for u, t in anchors:
        p = urlparse(u).path.rstrip("/")
        if p.startswith(base_path) and len(p) > len(base_path) and len(t) >= 20:
            out.append((u, t))
    return out


def _by_expediente(anchors: list[tuple[str, str]], expedientes: set[str]) -> tuple[str, str] | None:
    """Match by expediente code anywhere in the href/text. Precise → safe over ALL
    anchors (global-nav links never contain a code like PFOT-761). Longest code first."""
    for e in sorted(expedientes, key=len, reverse=True):
        for u, t in anchors:
            if e in _norm(u + " " + t):
                return u, f"expediente:{e}"
    return None


def _by_name(region_anchors: list[tuple[str, str]], names: set[str]) -> tuple[str, str] | None:
    """Name-token fallback, restricted to content-area anchors with a clear winner."""
    best: tuple[int, str] | None = None
    second = 0
    for u, t in region_anchors:
        score = len(_tokens(t) & names)
        if best is None or score > best[0]:
            second = best[0] if best else 0
            best = (score, u)
        elif score > second:
            second = score
    if best and best[0] >= 3 and best[0] > second:
        return best[1], f"name:{best[0]}"
    return None


def _list_subpage(anchors: list[tuple[str, str]], base_url: str) -> str | None:
    """A same-region link to the project list (one hop from a landing page)."""
    for u, t in _region_anchors(anchors, base_url):
        if re.search(r"proyecto|informaci", t.lower()):
            return u
    return None


def _inline_uuids(page: str, expedientes: set[str]) -> tuple[str, ...]:
    """Some portals (e.g. Valencia) list the almacen link INLINE next to the
    expediente, with no per-project page. Find our expediente in the page text
    and return the almacen UUID(s) in its block (a forward window)."""
    alm = [(m.start(), m.group(1)) for m in _ALMACEN_RE.finditer(page)]
    if not alm:
        return ()
    for e in sorted(expedientes, key=len, reverse=True):
        em = re.search(re.escape(e), page, re.I)  # e is normalized; page may differ in case
        if not em:
            continue
        pos = em.start()
        window = [u for d, u in alm if pos - 200 <= d <= pos + 3000]
        if window:
            return tuple(dict.fromkeys(window))
        nearest = min(alm, key=lambda a: abs(a[0] - pos))
        return (nearest[1],)
    return ()


async def resolve(
    client: httpx.AsyncClient, portal_url: str, expedientes: set[str], names: set[str],
    *, cache: dict[str, str | None],
) -> ResolveResult:
    """Resolve a proyectos-ci portal link to its almacen sending UUID(s)."""
    async def _get(url: str) -> str | None:
        if url not in cache:
            try:
                r = await client.get(url)
                cache[url] = r.text if r.status_code == 200 else None
            except httpx.HTTPError:
                cache[url] = None
        return cache[url]

    seen: set[str] = set()
    queue = [portal_url]
    while queue:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        page = await _get(url)
        if not page:
            continue
        # Pattern B (inline, e.g. Valencia): almacen link sits by the expediente on this page.
        inline = _inline_uuids(page, expedientes)
        if inline:
            log.info("annex_portal_resolved", project_url=url, matched_by="inline", sendings=len(inline))
            return ResolveResult(status="resolved", almacen_uuids=inline, project_url=url, matched_by="inline")
        # Pattern A (Aragón): match a per-project anchor, then follow to its page.
        anchors = _anchors(page, url)
        match = _by_expediente(anchors, expedientes) or _by_name(_region_anchors(anchors, url), names)
        if match:
            project_url, matched_by = match
            ppage = await _get(project_url)
            if not ppage:
                return ResolveResult(status="unresolved", project_url=project_url, matched_by=matched_by)
            uuids = tuple(dict.fromkeys(_ALMACEN_RE.findall(ppage)))
            if uuids:
                log.info("annex_portal_resolved", project_url=project_url, matched_by=matched_by, sendings=len(uuids))
                return ResolveResult(status="resolved", almacen_uuids=uuids, project_url=project_url, matched_by=matched_by)
            if "cita previa" in ppage.lower():
                return ResolveResult(status="office_only", project_url=project_url, matched_by=matched_by)
            return ResolveResult(status="unresolved", project_url=project_url, matched_by=matched_by)
        # No match on this page → try one hop to a project-list sub-page.
        if len(seen) == 1:
            sub = _list_subpage(anchors, url)
            if sub:
                queue.append(sub)
    return ResolveResult(status="unresolved")

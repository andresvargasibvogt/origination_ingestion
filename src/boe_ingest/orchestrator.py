"""End-to-end orchestration of one daily BOE run.

Wires sumario fetch + relevance filter + PDF fetch + robots guard + writer.
The sumario JSON is fetched in-memory for filtering only — it is NOT
persisted (we keep just the matching PDFs). In staging mode the manifest is
built but not written here; the promoter owns the OneLake manifest. Pure
async; the CLI in __main__.py drives it.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
import structlog

from origination_common import paths
from origination_common.fetcher import PDFFetcher, PDFFetchError
from origination_common.manifest import (
    FailedItem,
    ItemEntry,
    Manifest,
    RunInfo,
    now_iso,
    sha256_hex,
)
from origination_common.onelake import Writer
from origination_common.onelake import emit_manifest as emit_manifest_helper
from origination_common.robots import RobotsGuard

from .config import BOE_BASE_URL, Settings
from .relevance import RelevanceConfig, passes_filter
from .sumario import EmptyDay, fetch_sumario, total_items, walk_items

log = structlog.get_logger()


def _yyyymmdd(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}{d.day:02d}"


def _extract_url(value: Any) -> str | None:
    """BOE sumario wraps url_pdf as {"szBytes": ..., "texto": "<url>"}.

    url_xml / url_html are plain strings. This normaliser handles both.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        texto = value.get("texto")
        return texto if isinstance(texto, str) else None
    return None


async def ingest_one_day(
    target_date: date,
    writer: Writer,
    relevance: RelevanceConfig,
    settings: Settings,
    emit_manifest: bool = True,
) -> Manifest:
    """Run a single day's ingestion. Always returns a Manifest, even for empty days.

    When `emit_manifest=False` (staging mode), the Manifest is built and
    returned but NOT written via the writer — the promoter will build the
    OneLake manifest after scan results are in.

    Scope: only items matching the relevance filter (section + departamento)
    are persisted. The unfiltered BOE sumario is fetched in-memory for
    walking + filtering but is NOT written to staging or OneLake — it
    contains 240+ items per day outside our renewable-energy scope (judicial,
    civil-service, commercial anuncios) that we don't need and shouldn't
    store. The manifest captures everything about each filtered item.
    """
    started = now_iso()
    log.info("run_start", date=target_date.isoformat())

    robots = RobotsGuard(base_url=BOE_BASE_URL, user_agent=settings.user_agent)
    robots.load()

    async with httpx.AsyncClient(
        base_url=BOE_BASE_URL,
        headers={"User-Agent": settings.user_agent},
        timeout=settings.http_timeout_secs,
        http2=True,
        follow_redirects=True,
    ) as client:
        # 1. Sumario — fetch in-memory only; never written to storage.
        try:
            sumario = await fetch_sumario(client, _yyyymmdd(target_date))
        except EmptyDay:
            log.info("empty_day", date=target_date.isoformat())
            manifest = _empty_manifest(target_date, started)
            _emit(manifest, paths.manifest_path(target_date), settings, emit_manifest, writer)
            return manifest

        sumario_count = total_items(sumario)
        log.info("sumario_items_total", count=sumario_count)

        # 2. Relevance filter — section + departamento (human's filter)
        selected: list[tuple[dict[str, Any], dict[str, Any], str]] = []
        for item, dept, seccion in walk_items(sumario):
            if passes_filter(item, dept, seccion, relevance):
                selected.append((item, dept, seccion))
        log.info("items_filtered_in", count=len(selected))

        # 3. Per-item PDF fetch
        fetcher = PDFFetcher(
            client=client,
            concurrency=settings.pdf_concurrency,
            throttle_secs=settings.pdf_throttle_secs,
        )

        items_written: list[ItemEntry] = []
        items_failed: list[FailedItem] = []
        items_robots_blocked = 0

        async def _process(
            item: dict[str, Any], dept: dict[str, Any], seccion: str
        ) -> ItemEntry | None:
            nonlocal items_robots_blocked
            identifier = str(item.get("identificador", "")).strip()
            url_pdf = _extract_url(item.get("url_pdf"))
            url_xml = _extract_url(item.get("url_xml"))
            url_html = _extract_url(item.get("url_html"))
            if not identifier or not url_pdf:
                items_failed.append(
                    FailedItem(
                        identifier=identifier or "?",
                        reason="missing identifier or url_pdf",
                    )
                )
                return None
            if not robots.can_fetch(url_pdf):
                items_robots_blocked += 1
                log.info("robots_blocked", identifier=identifier, url=url_pdf)
                return None
            try:
                pdf_bytes = await fetcher.fetch(url_pdf)
            except PDFFetchError as exc:
                items_failed.append(FailedItem(identifier=identifier, reason=str(exc)))
                return None
            pdf_rel_path = paths.pdf_path(target_date, identifier)
            sha = sha256_hex(pdf_bytes)
            departamento_codigo = str(dept.get("codigo", ""))
            departamento_name = str(dept.get("nombre", ""))
            # Per-blob metadata so the promoter can build the OneLake manifest
            # without re-fetching anything from BOE.
            blob_metadata: dict[str, str] = {
                "identifier": identifier,
                "section": seccion,
                "departamento_codigo": departamento_codigo,
                "departamento": departamento_name[:8000],  # blob metadata value cap
                "published_at": target_date.isoformat(),
                "sha256": sha,
                "size_bytes": str(len(pdf_bytes)),
            }
            if url_pdf:
                blob_metadata["url_pdf"] = url_pdf
            if url_xml:
                blob_metadata["url_xml"] = url_xml
            if url_html:
                blob_metadata["url_html"] = url_html
            writer.write_bytes(pdf_rel_path, pdf_bytes, metadata=blob_metadata)
            return ItemEntry(
                identifier=identifier,
                section=seccion,
                departamento_codigo=departamento_codigo,
                departamento=departamento_name,
                published_at=target_date.isoformat(),
                url_pdf=url_pdf,
                url_xml=url_xml,
                url_html=url_html,
                eli=_extract_url(item.get("url_epub")) or _extract_url(item.get("eli")),
                pdf_path=pdf_rel_path,
                sha256=sha,
                size_bytes=len(pdf_bytes),
            )

        tasks = [asyncio.create_task(_process(it, dp, sc)) for it, dp, sc in selected]
        for coro in asyncio.as_completed(tasks):
            result = await coro
            if result is not None:
                items_written.append(result)

    # 4. Manifest
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=target_date.isoformat(),
        sumario_items_total=sumario_count,
        items_filtered_in=len(selected),
        items_written=len(items_written),
        items_failed=items_failed,
        items_robots_blocked=items_robots_blocked,
    )
    manifest = Manifest(run=run, items=items_written)
    _emit(manifest, paths.manifest_path(target_date), settings, emit_manifest, writer)
    log.info(
        "run_done",
        date=target_date.isoformat(),
        written=run.items_written,
        failed=len(run.items_failed),
        robots_blocked=run.items_robots_blocked,
        manifest_emitted=emit_manifest,
    )
    return manifest


def _emit(manifest: Manifest, manifest_rel_path: str, settings: Settings, emit_manifest: bool, writer: Writer) -> None:
    """Write the manifest via the writer (local/direct mode) or, for an empty
    day in staging mode, straight to OneLake so the processed-but-empty day is
    visible (the promoter only writes manifests for days that have items)."""
    emit_manifest_helper(
        writer=writer,
        manifest_json=manifest.to_json(),
        has_items=bool(manifest.items),
        lakehouse_path=manifest_rel_path,
        emit_via_writer=emit_manifest,
        workspace_name=settings.fabric_workspace_name,
        lakehouse_name=settings.fabric_lakehouse_name,
        azure_client_id=settings.azure_client_id,
    )


def _empty_manifest(target_date: date, started: str) -> Manifest:
    """Manifest for Sundays / holidays where the sumario 404s."""
    run = RunInfo(
        started_at=started,
        ended_at=now_iso(),
        date=target_date.isoformat(),
        sumario_items_total=0,
        items_filtered_in=0,
        items_written=0,
    )
    return Manifest(run=run, items=[])

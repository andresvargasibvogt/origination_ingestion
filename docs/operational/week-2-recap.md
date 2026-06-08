# Week 2 — BOA Ingest + Unified Image

**Period:** 2026-06-08 (single-day spike to land BOA and consolidate infra)
**Scope:** Renewable-energy-relevant slice of the Boletín Oficial de Aragón
landing as raw PDFs + per-day manifest in the same Fabric lakehouse as BOE,
on a single shared container image.

This document captures what shipped on top of [week 1](./week-1-recap.md).

---

## 1. Headline

A **second daily ingestion source (BOA)** is now flowing autonomously into
the team's Fabric lakehouse, on the **same unified container image** as BOE.
Verified end-to-end on 2026-06-08:

```text
{"sumarios_copied": 0, "promoted": 6, "quarantined": 0, "pending": 0, "skipped": 0}
  → 4 BOE PDFs in bronze/boe/raw/year=2026/month=06/day=08/
  → 2 BOA PDFs in bronze/boa/raw/year=2026/month=06/day=08/
  → manifest_upserted source=boe items_total=4
  → manifest_upserted source=boa items_total=2
```

The big-ticket changes that paid off:

- **BOA discovered via direct HTTP** instead of Playwright/Crawlee. ADR-004
  Step 2 (the undocumented-endpoint spike) succeeded after we noticed the SPA
  itself appends a `SECC-C=BOA` query parameter that triggers a server-side
  redirect to the SPA shell. Omitting that one parameter returns clean JSON
  with all of the day's items. No headless browser needed.
- **Unified container image** (`origination-ingest:latest`) replaces the
  per-source container model. Same Python 3.12-slim base as BOE (~150 MB,
  no Chromium). Each ACA Job runs the same image with a different `command`
  override (`boe-ingest`, `boa-ingest`, `boe-promoter`). Adding source N+1
  = new Python module + new entry point + new ACA Job. No new image.

---

## 2. What changed since week 1

### 2.1 Manifest / paths / promoter refactor (single commit, BOE-preserving)

Backbone refactor to support multiple sources from one promoter image. All
three changes preserve BOE behavior via sensible defaults.

| File | Change |
|---|---|
| `src/boe_ingest/manifest.py` | `Source = Literal["boe", "boa"]` (was `Literal["boe"]`). Added `SOURCE_BOE`/`SOURCE_BOA` constants, `ATTRIBUTION_BOA`, `attribution_for(source)` helper. Added optional `subsection: str | None` to `ItemEntry` (None for BOE, "b" for BOA's V.b). |
| `src/boe_ingest/paths.py` | Path helpers now accept `source: Source = SOURCE_BOE` kwarg. `bronze_root(source)` replaces the constant from config.py. |
| `src/boe_ingest/promoter.py` | Groups promoted blobs by `(source, date_iso)` tuple. Source is extracted from each blob's `bronze/{source}/raw/...` path and validated via `pydantic.TypeAdapter[Source]`. `_upsert_manifest()` takes source as a parameter; uses `attribution_for(source)` so the manifest carries the right PSI/CC BY 4.0 attribution per source. **One promoter run now handles both sources from a single image.** |

### 2.2 New `boa_ingest` package

```text
src/boa_ingest/
    __init__.py
    __main__.py        — argparse CLI mirroring boe_ingest/__main__.py
    config.py          — Pydantic Settings; shares STG_/FABRIC_/AZURE_ env
                         names with BOE so one env block works for both Jobs
    sumario.py         — fetch_sumario() + extract_pdf_url() + extract_mlkob()
                         Handles BOA's ISO-8859-1 body + backtick/acute-accent
                         URL wrapping
    relevance.py       — section + subsection + departamento_name matcher
    relevance.yaml     — single rule (see §3.2 for filter)
    orchestrator.py    — one-day pipeline: fetch sumario JSON → walk items
                         → filter → fetch each PDF (BRSCGI?CMD=VEROBJ&MLKOB=…)
                         → write to staging with per-blob metadata. Does NOT
                         write the manifest in staging mode (promoter owns it).
```

**Reused unchanged from `boe_ingest` via direct imports:** `BlobWriter`,
`OneLakeWriter`, `LocalWriter`, `Writer` Protocol, `select_credential`,
`PDFFetcher`, `RobotsGuard`, `ItemEntry`/`Manifest`/`RunInfo`/`FailedItem`
schemas (post-refactor), `sha256_hex`, `now_iso`. No code duplication.

### 2.3 Tests

| File | What |
|---|---|
| `tests/test_boa_relevance.py` | Calibration test mirroring the BOE pattern |
| `tests/fixtures/boa_calibration_set.yaml` | 3 positives + 11 negatives, hand-curated from real BOA dates 2026-06-01..2026-06-08 against the live filter |

Total test count: 8 (4 BOE + 4 BOA), 0 misclassifications.

### 2.4 Unified container image

`containers/origination-ingest.Dockerfile` replaces `containers/boe.Dockerfile`.

- Base: `python:3.12-slim`, same as the previous BOE-only image
- Multi-stage build with `uv sync --frozen --no-dev --no-editable` (the `--no-editable` fix from week 1)
- Copies both `src/boe_ingest/` and `src/boa_ingest/`
- No fixed ENTRYPOINT — each ACA Job sets its own `command` to one of the console scripts (`boe-ingest`, `boa-ingest`, `boe-promoter`) installed by `uv` into `/opt/venv/bin/` (on PATH)
- Runtime image size unchanged from week 1 (~150 MB)

ACR tags after Step 5: `acrorigination.azurecr.io/origination-ingest:latest` + `:boa-daily-ingest` (forensic tag for this change). The old `acrorigination.azurecr.io/boe-ingest:*` repo will be deleted after a one-week soak.

### 2.5 `pyproject.toml`

- Renamed: `boe-ingest` → `origination-ingest` v0.2.0
- Added `boa-ingest = "boa_ingest.__main__:main"` to `[project.scripts]`
- Added `src/boa_ingest` to `[tool.hatch.build.targets.wheel].packages`
- No dependency changes — BOA pulls nothing extra (httpx, pyyaml, pydantic, structlog, azure-* were already pulled by BOE)

---

## 3. Current operational state — combined view

### 3.1 ACA Jobs (post-week-2)

| Job | Cron | Command | Image |
|---|---|---|---|
| `caj-boe-daily` | `0 7 * * 1-6` (07:00 UTC Mon–Sat) | `boe-ingest --date=today` | `origination-ingest:latest` |
| `caj-boa-daily` | `0 8 * * 1-6` (08:00 UTC Mon–Sat) | `boa-ingest --date=today` | `origination-ingest:latest` |
| `caj-promoter` | `15,45 7,8 * * 1-6` (07:15, 07:45, 08:15, 08:45 UTC Mon–Sat) | `boe-promoter` | `origination-ingest:latest` |
| `caj-boe-backfill` | None (manual) | `boe-ingest --from=… --to=…` | `origination-ingest:latest` |

The promoter cron expansion (`15,45 7,8 * * 1-6` vs `15,45 7 * * 1-6`) adds two extra runs at 08:15 and 08:45 UTC to catch BOA's batch. 4 promoter runs per business day = 24 per week, no change to the "no polling-style cron" principle from week 1.

### 3.2 Filter (BOA only — BOE filter unchanged from week 1)

`src/boa_ingest/relevance.yaml`:

```yaml
rules:
  - section: "V"
    subsection: "b"
    departamento_name: "DEPARTAMENTO DE ECONOMÍA, COMPETITIVIDAD Y EMPLEO"
```

Matched against the BOA JSON's `Seccion`, `Subseccion`, and `Emisor` fields after extracting the leading code (`V. Anuncios` → `V`; `b) Otros anuncios` → `b`). Normalisation is case- and whitespace-insensitive so a BOA reskin that tweaks heading capitalisation doesn't break us.

### 3.3 OneLake layout (combined)

```text
lh_esp_origination.Lakehouse/Files/
  bronze/boe/raw/year=YYYY/month=MM/day=DD/
      BOE-A-YYYY-N.pdf          (one per matching disposition)
      _manifests/year=YYYY/month=MM/day=DD/_manifest.json
  bronze/boa/raw/year=YYYY/month=MM/day=DD/
      {MLKOB}.pdf               (one per matching anuncio)
      _manifests/year=YYYY/month=MM/day=DD/_manifest.json
```

Each manifest contains `source: "boe"` or `source: "boa"`, the same `RunInfo`
shape, and the same `ItemEntry` schema (BOA items carry the optional
`subsection` field; BOE items leave it null).

---

## 4. Discovery story — how BOA stopped needing a browser

This is the part of week 2 worth remembering for future sources.

ADR-004's sequenced decision tree:

```text
Step 1: Email publisher                       → skipped (slow)
Step 2: Spike for RSS / sitemap / XHR         → APPEARED TO FAIL initially:
                                                  - SPARQL stale since 2025-01-23
                                                  - CKAN points back to SPA
                                                  - /feed, /rss, /sitemap.xml 404
                                                  - BRSCGI CMD verbs (VERSUM, VERSEC, ...)
                                                    all return "CMD no reconocido"
                                                  - BRSCGI?CMD=VERLST&...&SECC-C=BOA
                                                    returns the SPA shell HTML
Step 3: Crawlee/Playwright (the planned fallback)
```

What broke this loose was capturing the SPA's actual XHR via Playwright with the network listener, then noticing that the **second** request returned 8 KB HTML while the **first** request had returned ~600 KB JSON. The two URLs differed in exactly one parameter: the failing one had `SECC-C=BOA` appended, and the JSON-returning one didn't. The SPA's own URL builder adds `SECC-C=BOA` for the "by-date" route; calling without it gets us JSON directly.

The endpoint:

```text
https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI
    ?CMD=VERLST
    &BASE=BOLE
    &DOCS=1-250
    &SEC=OPENDATABOAJSONAPP
    &OUTPUTMODE=JSON
    &SORT=-PUBL
    &SEPARADOR=
    &PUBL-C=YYYYMMDD
```

Quirks worth noting (captured in `src/boa_ingest/sumario.py`):

- Response `Content-Type: text/html; charset=ISO-8859-1` is **mislabelled** — the body is JSON. We sniff by checking whether the body starts with `[`.
- Body is genuinely **ISO-8859-1** (not UTF-8 mislabelled). Spanish `ó` arrives as byte `0xF3`, not the UTF-8 2-byte `0xC3 0xB3`. We decode with `latin-1` before `json.loads`.
- `UrlPdf` field wraps URLs in backticks and uses `´` (U+00B4) as a separator between alternates. Real value:
  ```text
  `https://www.boa.aragon.es/cgi-bin/EBOA/BRSCGI?CMD=VEROBJ&MLKOB=1451564830505´`https://...
  ```
  Strip both wrappers; the first URL is the PDF.
- Non-publication days (Sundays, holidays) return the SPA shell (~8 KB HTML). The orchestrator treats this as an empty day and emits an empty manifest, just like BOE.

ADR-004 is now closed: **Option B succeeded, BOA stays slim, no headless browser anywhere**.

---

## 5. Open items / next steps

### 5.1 Immediate

- **Tomorrow morning (2026-06-09 ~08:20 UTC) spot-check.** Verify the autonomous 07:00 BOE Job + 08:00 BOA Job + 07:15/07:45/08:15/08:45 promoter ticks left both sources in OneLake without manual intervention.

### 5.2 Cleanup tasks (low priority)

- **Delete legacy ACR repo `boe-ingest`** after a one-week soak (when we're confident the unified image is stable).
- **Remove the redundant `Storage Blob Data Contributor`** RBAC assignment on the staging account — `Storage Blob Data Owner` is a superset.

### 5.3 Notable gotchas added this week

- **BOA's BRSCGI returns the SPA shell when `SECC-C=BOA` is in the URL.** Drop that parameter to get JSON. Documented in `src/boa_ingest/sumario.py` header.
- **BOA's JSON content-type lies.** `text/html; charset=ISO-8859-1` for a body that is JSON in genuine Latin-1. Sniff with `body.lstrip().startswith(b"[")` and decode `latin-1` explicitly.
- **BOA's URL fields use backticks and `´` as wrappers/separators.** Strip both before using.

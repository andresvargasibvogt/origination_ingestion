# Week 3 — REE Capacity Poller + `origination_common` Extraction

**Period:** 2026-06-10
**Scope:** Third ingestion source (REE monthly capacity CSV) + the shared-library
refactor the plan deferred until source #3.

Builds on [week 1](./week-1-recap.md) (BOE) and [week 2](./week-2-recap.md) (BOA).

---

## 1. Headline

A **third source (REE)** is live, and the codebase was refactored so no source
package imports another. REE is a different shape from the gazettes: a single
CSV published **monthly on an uncertain day**, **checked daily** but **collected
once** — the dedup (OneLake existence check) is what guarantees one collection
per month, independent of poll frequency. REE lands in **month-level folders**
(`year=/month=/`, no `day=`) since a monthly file in daily folders would just
scatter single files into empty day partitions.

Verified end-to-end in cloud on 2026-06-10:

- `caj-ree-monthly` discovered + downloaded `2026_06_04_GRT_generacion.csv` (300 KB) → staging
- Defender scanned it → `caj-promoter` promoted it to OneLake `bronze/ree/raw/year=2026/month=06/` (month-level, no `day=`) + wrote the manifest
- Re-trigger logged `ree_version_already_present` (dedup no-op) — no re-download, no re-scan
- BOE + BOA re-verified on the same refactored image: 4 BOE + 1 BOA items still land correctly

---

## 2. The `origination_common` extraction

Through weeks 1–2, `boe_ingest/` was doing double duty — the BOE source package
**and** the de-facto shared library (`boa_ingest` did `from boe_ingest.manifest
import ...`, which reads wrong). The plan flagged this: *"defer the common
package until source #3 lands."* REE is #3, so we extracted it.

```text
src/
  origination_common/     ← shared, no source owns it
    config.py             (ONELAKE_ACCOUNT_URL + CommonSettings infra fields)
    manifest.py           (Pydantic schemas, Source literal, attribution)
    onelake.py            (OneLakeWriter [+ exists()], LocalWriter, Writer, creds)
    blob.py               (BlobWriter — staging)
    fetcher.py            (async HTTP fetcher)
    robots.py             (RobotsGuard — base_url now required)
    paths.py              (bronze/{source}/raw/... helpers)
    promoter.py           (staging → OneLake, handles any source from the path)
  boe_ingest/             ← BOE-only: config, sumario, relevance, orchestrator, __main__
  boa_ingest/             ← BOA-only: config, sumario, relevance, orchestrator, __main__
  ree_ingest/             ← REE-only: config, discover, orchestrator, __main__
```

Moves were `git mv` (history preserved). Every source now imports
`from origination_common import ...`; none imports another source.

**Bug found + fixed during the move:** the promoter's
`_blob_metadata_to_item_entry` never read the `subsection` field, so BOA's
subsection (`"b"`) was silently dropped from the OneLake manifest (the staging
blob had it). Now read correctly; also tolerant of absent
section/departamento_codigo (REE leaves them empty).

**Entry-point rename:** the promoter console script is now `promoter`
(`origination_common.promoter:main`). `boe-promoter` kept as a transitional
alias so the running Job survived the cutover; `caj-promoter` is now switched to
`promoter`.

---

## 3. REE source — what's different

| | BOE / BOA | REE |
|---|---|---|
| Cadence | Daily | **Monthly, uncertain day** |
| Trigger model | Ingest "today" | **Poll for the latest published version** |
| Output | Many PDFs, filtered | **One CSV, whole file** |
| Filter | section / departamento | **None — grab the whole file** |
| Discovery | sumario JSON | **Static href on the landing page** |
| Dedup | path = today (idempotent) | **OneLake existence check on the pub-date path** |

### Discovery

No SPA, no browser. The landing page
`/es/clientes/generador/acceso-conexion/conoce-la-capacidad-de-acceso` lists the
file as a static href:

```text
/sites/default/files/12_CLIENTES/Documentos/2026_06_04_GRT_generacion.csv
```

`ree_ingest.discover.find_latest_csv()` extracts every `*_GRT_generacion.csv`
href, parses the `YYYY_MM_DD` publication date from each filename, and returns
the most recent. The page also offers PDF + XLSX of the same data; we take the
**CSV** only (per scope). The CSV is UTF-8 (BOM), CRLF, semicolon-delimited — we
land raw bytes; no parsing at bronze.

### Poll + dedup

`caj-ree-monthly` runs **daily at 09:00 UTC** (`0 9 * * *`). Each run:
1. fetches the landing page, finds the latest CSV + its publication date
2. checks if `bronze/ree/raw/year=/month=/{file}.csv` already exists in OneLake (month-level path — no `day=`)
3. if present → logs `ree_version_already_present`, exits (no download, no scan)
4. if new → downloads → writes to staging → promoter promotes after Defender clears it

REE publishes ~once a month on an uncertain day (expected first week). We
**check daily** so a release is caught within a day whenever it drops, and the
**dedup makes it a no-op on the other ~30 days** — exactly one collection per
month. (Cadence history: started daily; briefly tried weekly to reduce noise but
that risked ~6-day latency if the file dropped just after the weekly run, so
reverted to daily-check + dedup, which is both prompt and idempotent.) The file
lands in a **single month folder** `bronze/ree/raw/year=2026/month=06/` — daily
partitions made no sense for a monthly artifact. `--force` bypasses the dedup;
`--out-dir` runs locally without Azure.

---

## 4. Current operational state — all three sources

| Job | Cron (UTC) | Command | Cadence |
|---|---|---|---|
| `caj-boe-daily` | `0 7 * * 1-6` | `boe-ingest --date=today` | daily Mon–Sat |
| `caj-boa-daily` | `0 8 * * 1-6` | `boa-ingest --date=today` | daily Mon–Sat |
| `caj-ree-monthly` | `0 9 * * *` | `ree-ingest` | daily check, one collection/month (dedup) |
| `caj-promoter` | `15,45 7,8,9 * * 1-6` | `promoter` | 6 runs/day, drains all sources |
| `caj-boe-backfill` | manual | `boe-ingest --from=… --to=…` | on demand |

All five Jobs run the single unified image `origination-ingest:latest`. The
promoter cron gained hour `9` to cover REE's 09:00 upload window (07:15, 07:45,
08:15, 08:45, 09:15, 09:45).

OneLake bronze layout now spans three sources:

```text
lh_esp_origination.Lakehouse/Files/bronze/
  boe/raw/year=/month=/day=/BOE-*.pdf               + _manifests/year=/month=/day=/_manifest.json   (daily)
  boa/raw/year=/month=/day=/{MLKOB}.pdf             + _manifests/year=/month=/day=/_manifest.json   (daily)
  ree/raw/year=/month=/{date}_GRT_generacion.csv    + _manifests/year=/month=/_manifest.json         (monthly — no day=)
```

REE manifests carry `source: "ree"`, empty gazette fields, and
`departamento: "Red Eléctrica de España"`.

---

## 5. Adding source #4 from here

The pattern is now fully generalized. Source N+1 =

1. `src/{name}_ingest/` with `config`, a discovery module, `orchestrator`, `__main__` — importing everything shared from `origination_common`
2. one line in `manifest.py` widening the `Source` literal + an `ATTRIBUTION_{NAME}`
3. `{name}-ingest` entry point in `pyproject.toml` + the package in the wheel list
4. a new `caj-{name}-…` ACA Job
5. a promoter cron tick covering the new upload window if it's outside 07–09 UTC

Partition granularity is a per-source choice via the `granularity` kwarg on the
`paths.py` helpers: daily sources use the default (`"day"` → `…/day=DD/`),
monthly/low-frequency sources pass `"month"` (`…/month=MM/`, no day folder). The
promoter parses both shapes — no promoter change needed for either.

For an unknown-release-day source like REE, "check daily + dedup against OneLake"
gives prompt pickup with exactly one collection — don't reach for a low-frequency
cron (it trades latency for nothing the dedup doesn't already provide).

No new container, no new image, no promoter code change.

---

## 6. Cleanup done / outstanding

- **Done:** legacy `boe-ingest` ACR repo deleted (week 2); `containers/boe.Dockerfile` removed (superseded by `origination-ingest.Dockerfile`); promoter switched to canonical `promoter` command.
- **Outstanding (low priority):** drop the redundant `Storage Blob Data Contributor` RBAC on the staging account (Owner is a superset).
- **Watch (next ~3 weeks):** confirm July's REE release is auto-detected. Expected first week of July; the daily `caj-ree-monthly` check should land it within ~1 day of publication into a new `month=07` folder and log the new `published_at`.

# How the ingestion pipeline works

A detailed walkthrough of how the scrapers and the promoter move data from
public Spanish sources into the Fabric lakehouse. This is the "how it actually
runs" reference — for *why* each choice was made see the ADRs in
[`docs/decisions/`](../decisions/); for *what shipped when* see the weekly
recaps in [`docs/operational/`](../operational/).

---

## 1. The big picture

Every source follows the same two-stage path, with a malware-scanning DMZ in
the middle. **No scraper writes to OneLake directly** — that is the whole point
of the design (ADR-008, "Pattern F").

```text
                      STAGE 1 — INGEST (per source)                STAGE 2 — PROMOTE (shared)
                 ┌──────────────────────────────────┐        ┌──────────────────────────────────┐
 public source ─►│  caj-{source}-daily  (ACA Job)    │        │  caj-promoter  (ACA Job)          │
 (boe.es,        │   discover → filter → fetch files │        │   for each staged blob:           │
  boa.aragon.es, │   ↓                                │        │     read Defender scan tag        │
  ree.es)        │   write each file to STAGING       │        │     clean   → copy to OneLake     │
                 │   with per-item blob metadata      │        │     bad     → move to quarantine  │
                 └──────────────┬─────────────────────┘        │     pending → skip, retry later   │
                                │                              │   then upsert one manifest/partition
                                ▼                              └───────────────┬──────────────────┘
                 ┌──────────────────────────────────┐                          │
                 │  storiginationdmz (storage acct)  │                          ▼
                 │   container: untrusted/           │        ┌──────────────────────────────────┐
                 │   ┌ Defender for Storage scans ──┐│        │  OneLake — lh_esp_origination     │
                 │   │ every upload, writes a blob   ││        │  Files/bronze/{source}/raw/...    │
                 │   │ index tag with the verdict    ││        │  + _manifests/.../_manifest.json  │
                 │   └───────────────────────────────┘│        └──────────────────────────────────┘
                 │   container: quarantine/          │
                 └──────────────────────────────────┘
```

Why two stages instead of writing straight to OneLake? Because the files come
from the public internet. Defender for Storage scans every blob *after upload*
and writes the verdict as a blob index tag — but scanning is asynchronous
(seconds to minutes). So the scraper drops files into a quarantine-by-default
staging account and exits; a separate scheduled job ("the promoter") later reads
each verdict and copies only clean files into the lakehouse. The lakehouse never
holds an unscanned byte.

---

## 2. Code layout

```text
src/
  origination_common/   ← shared, source-agnostic (no source owns it)
    config.py           CommonSettings (Fabric/staging/MI env) + ONELAKE_ACCOUNT_URL
    manifest.py         Pydantic schemas, Source literal, per-source attribution
    onelake.py          OneLakeWriter / LocalWriter / Writer protocol / credential selection
    blob.py             BlobWriter — writes to the staging account with metadata
    fetcher.py          PDFFetcher — async HTTP fetch with retries + throttle (any content type)
    robots.py           RobotsGuard — honours robots.txt
    paths.py            bronze/{source}/raw/... path helpers (day vs month granularity)
    promoter.py         the promoter job (Stage 2)
  boe_ingest/           BOE-specific: config, sumario, relevance(.yaml), orchestrator, __main__
  boa_ingest/           BOA-specific: config, sumario, relevance(.yaml), orchestrator, __main__
  ree_ingest/           REE-specific: config, discover, orchestrator, __main__
```

All three source packages import their plumbing from `origination_common`; none
imports another source. One container image (`origination-ingest:latest`) holds
all of it. Each ACA Job runs the same image with a different `command`
(`boe-ingest`, `boa-ingest`, `ree-ingest`, `promoter`).

---

## 3. Stage 1 — the scrapers (ingest)

Each scraper is a small async program that does four things: **discover** what is
available, **filter** to what is in scope, **fetch** the matching files, and
**write** them to staging with metadata. Discovery + filter differ per source;
fetch + write are shared.

### 3.1 What differs per source

| | **BOE** (`boe_ingest`) | **BOA** (`boa_ingest`) | **REE** (`ree_ingest`) |
|---|---|---|---|
| Cadence | Daily | Daily | Monthly (uncertain day) |
| Discovery | Open-data **JSON API** `…/datosabiertos/api/boe/sumario/{YYYYMMDD}` | **JSON endpoint** the SPA uses: `BRSCGI?…SEC=OPENDATABOAJSONAPP&…&PUBL-C=YYYYMMDD` (omit `SECC-C=BOA` or it returns the SPA shell) | **Landing-page HTML**; regex-extract the `*_GRT_generacion.csv` href |
| What we want | Renewable dispositions | Renewable anuncios | The whole capacity CSV |
| Filter | section + departamento código (`relevance.yaml`) | section + subsection + departamento name (`relevance.yaml`) | none — one file |
| Files fetched | many PDFs | many PDFs (`BRSCGI?CMD=VEROBJ&MLKOB=…`) | one CSV |
| Dedup | path = the day (idempotent overwrite) | path = the day | **OneLake existence check** (poll daily, collect once) |
| OneLake partition | `…/year=/month=/day=/` | `…/year=/month=/day=/` | `…/year=/month=/` (no `day=`) |

### 3.2 The shared ingest flow

Using BOE's `orchestrator.ingest_one_day()` as the model (BOA mirrors it; REE is
the single-file variant `ingest_latest()`):

1. **Load robots.txt** for the source host into a `RobotsGuard`. Every URL we are
   about to fetch is checked against it; blocked URLs are skipped and counted
   (`robots_blocked`), never fetched.
2. **Fetch the index** (sumario JSON / landing page) over an `httpx.AsyncClient`
   with the source's identifying `User-Agent` and HTTP/2.
3. **Walk + filter** the index items. The filter is *structural* (section /
   subsection / departamento), loaded from the source's editable
   `relevance.yaml` — not keyword matching. `items_filtered_in` is logged.
4. **Fetch each surviving file** concurrently through `PDFFetcher`, which wraps
   `httpx` with a concurrency semaphore, a politeness throttle, and `tenacity`
   retries (3 attempts, exponential backoff on HTTP/transport errors).
5. **Write each file to staging** via the `BlobWriter` with a dict of per-item
   **blob metadata** (see §3.3). The orchestrator computes the lakehouse-relative
   path (`paths.pdf_path`, or the month path for REE) and hands `(path, bytes,
   metadata)` to the writer.
6. The unfiltered source index is **not** persisted — only the matching files.
7. **Manifest:** in staging mode the orchestrator builds the manifest object but
   does **not** write it (`emit_manifest=False`) — the promoter owns the OneLake
   manifest, because only the promoter knows which files actually passed the
   scan. (In `--out-dir` local mode the manifest is written locally; see §8.)

### 3.3 Per-item blob metadata — the hand-off to the promoter

When the scraper writes a file to staging it attaches metadata to the blob. This
is how the promoter later rebuilds the manifest **without re-parsing anything**.
The keys the promoter reads:

```text
identifier            BOE-A-2026-12345 / MLKOB / 2026_06_04_GRT_generacion
section               "III", "V", or "" (REE)
subsection            "b" (BOA) or absent
departamento_codigo   "9575" (BOE) or "" (BOA/REE)
departamento          full issuer name
published_at          ISO date YYYY-MM-DD
sha256                hash of the bytes
size_bytes            length
url_pdf / url_xml      source URLs (optional)
```

Azure blob metadata values must be **ASCII**, so `BlobWriter` ASCII-folds them
(`"Transición"` → `"Transicion"`). The full Unicode original survives in the
manifest, which the promoter writes to OneLake as UTF-8. Defender's own
scan-result tags are *blob index tags* — a separate mechanism from this
metadata; the two don't collide.

### 3.4 The REE poller specifically

REE is monthly but published on an unknown day, so `ree_ingest` runs **daily**
and dedups:

1. fetch the landing page; `discover.find_latest_csv()` regex-extracts every
   `*_GRT_generacion.csv` href, parses the `YYYY_MM_DD` publication date from
   each filename, and returns the most recent.
2. build the target month path `bronze/ree/raw/year=YYYY/month=MM/{file}.csv`.
3. **dedup:** `OneLakeWriter.exists(target)` — if that month's file is already in
   OneLake, log `ree_version_already_present` and exit (no download, no scan).
4. otherwise download the CSV → write to staging → the promoter takes over.

So ~30 days a month the REE job is a clean no-op; on the day a new file appears
it lands within a day. Exactly one collection per month, regardless of poll
frequency. (`--force` bypasses the dedup.)

---

## 4. The staging DMZ + Defender

The scrapers write into `storiginationdmz`, container `untrusted/`, at the same
lakehouse-relative path the file will eventually have in OneLake (e.g.
`bronze/boe/raw/year=2026/month=06/day=11/BOE-A-2026-12345.pdf`). Writing to the
*same* path is deliberate: the promoter's promotion step is then a
path-preserving copy.

**Defender for Storage** (Standard plan, on-upload malware scanning enabled on
the account) reacts to each `BlobCreated` event via an auto-provisioned Event
Grid system topic, scans the bytes, and **writes the result as a blob index
tag**:

```text
key:   "Malware Scanning scan result"     (singular — Microsoft's exact key)
value: "No threats found"  |  "Malicious"
```

This is asynchronous — typically 5–15 minutes after upload, with a documented
outer bound of 30 min to 3 hours for unusually large/complex blobs. Until the
tag appears the blob is "pending" and the promoter leaves it alone.

---

## 5. Stage 2 — the promoter (in detail)

The promoter (`origination_common/promoter.py`, entry point `promoter`, Job
`caj-promoter`, cron `15,45 7,8,9 * * 1-6`) is the **one shared, source-agnostic
component** that reads scanned blobs back out of staging and lands the clean
ones in OneLake. It owns the OneLake manifest. It holds no per-source knowledge —
it learns the source from each blob's path.

### 5.1 Startup (`main()`)

1. configure structured (JSON) logging.
2. `settings = load_common_settings()` — reads `STG_ACCOUNT_NAME`,
   `FABRIC_WORKSPACE_NAME`, `FABRIC_LAKEHOUSE_NAME`, `AZURE_CLIENT_ID`,
   `STG_CONTAINER_UNTRUSTED`, `STG_CONTAINER_QUARANTINE` from the environment.
3. hard-fail (exit 1) if `STG_ACCOUNT_NAME` or `FABRIC_WORKSPACE_NAME` is missing.
4. `cred = select_credential(azure_client_id)` — in Azure (`IDENTITY_ENDPOINT`
   set) this is `ManagedIdentityCredential(client_id=…)`, i.e. the
   `id-origination` UAMI; locally it's `DefaultAzureCredential`.
5. open `BlobServiceClient` for the staging account and get container clients for
   `untrusted/` and `quarantine/`.
6. construct an `OneLakeWriter` for the target workspace + lakehouse.
7. log `promoter_run_start`.

### 5.2 The blob loop

It lists **every** blob in `untrusted/` (`list_blobs(include=["metadata"])`) and
for each one:

**a. Parse the path.** `_parse_date_path()` matches

```text
bronze/(source)/raw/year=(YYYY)/month=(MM)[/day=(DD)]/(rest)
```

with the `day=` segment **optional**. Daily sources (BOE, BOA) have it; the
monthly source (REE) does not. Returns `(source, year, month, day|None,
filename)`, or `None` if the path doesn't match → log `path_unparseable`,
count `skipped`, move on.

**b. Validate the source.** The parsed `source` segment is validated against the
`Source` literal (`"boe" | "boa" | "ree"`) via a Pydantic `TypeAdapter`. An
unknown source → log `unknown_source`, count `skipped`, move on. (This is the
single source of truth for "is this a source we recognise?" — it widens
automatically when the `Source` literal grows.)

**c. Handle the file by type:**

- **`sumario.json`** (legacy — current scrapers no longer write it): copied to
  OneLake as-is and deleted, no scan check (it was our own JSON, not fetched user
  content). Counts `sumarios_copied`. Present only to drain any old blobs.
- **everything else** (the real content — PDFs, CSV): goes through the scan gate.

**d. Read the scan verdict.** `_read_scan_verdict()` calls `get_blob_tags()` and
returns the value of the `"Malware Scanning scan result"` tag, or `None` if the
tag isn't there yet.

```text
verdict is None            → log scan_pending, count pending, SKIP (retry next run)
verdict == "Malicious"     → copy blob to quarantine/, delete from untrusted/,
                             log scan_malicious (warning), count quarantined. NOT promoted.
verdict == "No threats found" → PROMOTE (below)
anything else              → log scan_unknown_verdict, count skipped, leave it.
```

**e. Promote a clean blob.**

1. read the blob's metadata (`get_blob_properties().metadata`).
2. download the bytes.
3. `onelake.write_bytes(path, data)` — writes to OneLake at the **same**
   lakehouse-relative path; logs `onelake_write_ok`.
4. `_blob_metadata_to_item_entry()` turns the metadata into a manifest
   `ItemEntry` (tolerant of missing optional fields; bad metadata → logged
   `manifest_metadata_invalid` and the item is skipped from the manifest, but the
   bytes are still promoted).
5. the entry is grouped under the key `(source, year, month, day|None)`.
6. **delete the staging blob** — log `blob_promoted`.

The order is **copy-to-OneLake → delete-from-staging** ("copy then delete"). If
the job dies in between, the next run re-finds the blob, re-copies identical
bytes (idempotent overwrite), and retries the delete. Nothing is lost; at worst a
blob is copied twice.

### 5.3 Manifest upsert (after the loop)

Promoted items are grouped by partition key `(source, year, month, day|None)`.
For each touched partition the promoter writes **one** `_manifest.json`:

- **day present** (BOE/BOA): manifest at `…/_manifests/year=/month=/day=/`,
  `RunInfo.date` = that ISO date, granularity `"day"`.
- **day absent** (REE): manifest at `…/_manifests/year=/month=/`, `RunInfo.date`
  = the item's true `published_at`, granularity `"month"`.

The write is an **upsert**, not an overwrite (`_upsert_manifest`):

1. `_load_existing_manifest()` reads the current manifest if present (`None` on
   the first write of the partition).
2. existing items are keyed by `identifier` into a dict; new items are merged in
   (last write wins per identifier).
3. a fresh `RunInfo` is produced (new manifest) or the existing one is copied
   with updated `ended_at` / `items_written` / `items_failed`.
4. the merged manifest is written back; log `manifest_upserted` with `source`,
   `date`, `granularity`, `items_added`, `items_total`.

Because the merge is keyed by `identifier`, promoting the same partition across
several runs (BOE files scanned at different times, or a crash-retry) just
converges — already-present items are no-ops, newly-clean items are added.

### 5.4 End of run

Logs `promoter_run_done` with the tallies: `sumarios_copied`, `promoted`,
`quarantined`, `pending`, `skipped`. A healthy steady-state run after everything
has drained reads `promoted: 0, pending: 0` — a pure no-op.

### 5.5 Why a single promoter for all sources works

- It routes purely on the path (`bronze/{source}/raw/…`), so adding a source
  needs **zero** promoter changes — only that the `Source` literal includes the
  new name.
- It handles both daily and monthly partitions from the same regex.
- It is scheduled often enough (6×/day: 07:15, 07:45, 08:15, 08:45, 09:15, 09:45
  UTC) to catch each source's upload window after Defender finishes scanning:
  BOE uploads ~07:00, BOA ~08:00, REE ~09:00.

---

## 6. The manifest — the downstream contract

Each partition has a `_manifest.json` (Pydantic-validated, `schema_version`
`"1.0"`). It is the stable contract for downstream extraction/linking tools: they
read the manifest to discover what landed, rather than listing files.

```jsonc
{
  "schema_version": "1.0",
  "source": "boe",                       // "boe" | "boa" | "ree"
  "run": {
    "started_at": "...", "ended_at": "...",
    "date": "2026-06-11",
    "sumario_items_total": 0,            // promoter doesn't know the source total; informational
    "items_filtered_in": 4,
    "items_written": 4,
    "items_failed": [],
    "items_robots_blocked": 0,
    "attribution": "Fuente de los datos: ..."   // per-source PSI / CC BY 4.0 line
  },
  "items": [
    {
      "identifier": "BOE-A-2026-12345",
      "section": "III", "subsection": null,
      "departamento_codigo": "9575",
      "departamento": "Ministerio para la Transición Ecológica ...",
      "published_at": "2026-06-11",
      "url_pdf": "https://...", "url_xml": null, "url_html": null, "eli": null,
      "pdf_path": "bronze/boe/raw/year=2026/month=06/day=11/BOE-A-2026-12345.pdf",
      "sha256": "…64 hex…", "size_bytes": 198517
    }
  ]
}
```

`pdf_path` is the lakehouse-relative path to the landed file regardless of type
(it holds the `.csv` path for REE). Gazette-specific fields are empty for REE.

---

## 7. Identity & auth (no secrets anywhere)

One user-assigned managed identity, `id-origination`, is used by every Job:

- **AcrPull** on the resource group → pull the container image.
- **Storage Blob Data Owner** on `storiginationdmz` → scrapers write blobs +
  metadata; the promoter reads blobs, reads the scan index tags, copies, and
  deletes. (Owner rather than Contributor because this tenant's Contributor role
  lacks the `blobs/tags/read` action that reading Defender verdicts needs.)
- **Workspace Contributor** on the Fabric workspace (granted via the
  `Central Members (DEV)` security group) → the promoter + the REE dedup
  read/write OneLake.

`select_credential()` picks `ManagedIdentityCredential` in Azure (detected by the
`IDENTITY_ENDPOINT` env var ACA injects) and `DefaultAzureCredential` locally so
a developer's `az login` works for dry runs.

---

## 8. Run modes (the writer abstraction)

The same orchestrator code runs three ways, decided by env/flags in each
`__main__`:

| Mode | Trigger | Writer | Manifest |
|---|---|---|---|
| **Staging** (production) | `STG_ACCOUNT_NAME` set | `BlobWriter` → `untrusted/` | written by the **promoter** |
| **Direct-to-OneLake** | no `STG_…`, `FABRIC_WORKSPACE_NAME` set | `OneLakeWriter` | written by the scraper |
| **Local** | `--out-dir ./out` | `LocalWriter` (+ `.meta.json` sidecars) | written locally |

All three implement the same `Writer` protocol (`write_bytes` / `write_text`), so
the orchestrator is identical in each. Local mode needs no Azure credentials and
is how the scrapers are dry-run and calibration-tested.

---

## 9. Scheduling

| Job | Cron (UTC) | Command | Notes |
|---|---|---|---|
| `caj-boe-daily` | `0 7 * * 1-6` | `boe-ingest --date=today` | Mon–Sat (BOE doesn't publish Sun) |
| `caj-boa-daily` | `0 8 * * 1-6` | `boa-ingest --date=today` | Mon–Sat |
| `caj-ree-monthly` | `0 9 * * *` | `ree-ingest` | daily check, one collection/month via dedup |
| `caj-promoter` | `15,45 7,8,9 * * 1-6` | `promoter` | 6×/day, drains all sources |
| `caj-boe-backfill` | manual | `boe-ingest --from=… --to=…` | on-demand history |

ACA Job args with spaces don't round-trip, so dates use the `=` form
(`--date=today`).

---

## 10. Failure modes & how the design absorbs them

| Situation | What happens |
|---|---|
| Defender hasn't scanned yet | blob stays in `untrusted/`; promoter logs `scan_pending`, picks it up a later run |
| Malicious file | moved to `quarantine/`, never reaches OneLake, `scan_malicious` warning |
| Promoter crashes mid-promote | copy-then-delete → next run re-copies identical bytes, retries delete |
| Source publishes nothing (Sunday/holiday) | scraper logs `empty_day`, writes nothing (or an empty manifest in non-staging mode) |
| Same file re-ingested | identical path + bytes → idempotent overwrite; manifest merge by `identifier` is a no-op |
| REE polled but no new release | `ree_version_already_present`, exits without downloading |
| Bad/missing blob metadata | bytes still promoted; item skipped from manifest with `manifest_metadata_invalid` |
| Unknown source segment in a path | `unknown_source`, skipped (never promoted to a bogus location) |

---

## 11. End-to-end example (a normal BOE weekday)

```text
07:00  caj-boe-daily fires.
       fetch sumario JSON for today → 301 items.
       filter (section III + dept 9575/9593) → 4 match.
       fetch 4 PDFs; write each to
         storiginationdmz/untrusted/bronze/boe/raw/year=2026/month=06/day=11/BOE-A-2026-*.pdf
         with per-item metadata. Job exits (manifest NOT written).

07:00– Defender sees 4 BlobCreated events, scans each, writes
07:10    "Malware Scanning scan result" = "No threats found" index tags.

07:15  caj-promoter fires.
       lists untrusted/ → 4 BOE blobs.
       each: parse path (source=boe, day=11), read tag = clean,
             copy bytes to OneLake bronze/boe/raw/.../day=11/, delete from staging,
             log blob_promoted.
       group the 4 items under (boe,2026,06,11) → upsert
         OneLake bronze/boe/raw/_manifests/year=2026/month=06/day=11/_manifest.json
       log promoter_run_done: promoted=4, pending=0, quarantined=0.

07:45  caj-promoter fires again → untrusted/ empty → promoted=0 (no-op).
08:xx  same dance for BOA; 09:xx for REE (only when a new monthly file exists).
```

Downstream tools then read `_manifest.json` from each partition to know exactly
what landed, and `read_bytes` each file by its `pdf_path`.

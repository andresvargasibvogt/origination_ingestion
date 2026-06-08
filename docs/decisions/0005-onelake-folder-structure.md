# ADR-005 — OneLake folder structure and ingestion contract

- **Status:** Proposed
- **Date:** 2026-06-01 (revised 2026-06-02 to match verified lakehouse + bronze-only scope)
- **Owners:** Data platform team
- **Related:** [ADR-001 compute](0001-compute-platform.md), [ADR-002 network](0002-network-posture.md), [ADR-003 supply-chain](0003-supply-chain-controls.md), [ADR-004 BOA discovery](0004-boa-discovery-method.md)

## Scope

**This ADR covers raw ingestion landing only.** Specifically: where the scraper writes scraped bytes, in what folder layout, and what stable contract downstream tools can rely on to find that data.

Silver / gold tiers (extraction to typed events, business rollups, dashboards), the BFF + React consumption layer, and any AI-on-data work are **out of scope** for this ADR and will be covered separately when that work begins.

## Context

The team already runs a Fabric workspace (`Central Data & Integration`, with dev / stg / prod variants) that hosts a domain "origination" lakehouse — `lh_esp_origination` — for Spain-sourced data. The lakehouse already has a partial layout (`Files/bronze/{boe,boa,endesa,ree}/raw/`) seeded by manual file drops. This ADR formalises the layout for **automated ingestion** to write into.

The corpus boundary is the renewable-energy slice of Spanish official gazettes (BOE first, BOA later) — the conceptual scope is set by the BOE and BOA deep-dives kept at the repo root. Two parallel loaders (Endesa, REE) exist for the grid-data sources and are owned by other workstreams; this ADR uses the same folder convention but does not constrain what those loaders write.

## Decision

Land all scraped raw content into `lh_esp_origination` under:

```text
Files/bronze/{source}/raw/year=YYYY/month=MM/day=DD/
```

The lakehouse is shared with parallel domain loaders (Endesa, REE) which use the same convention. This ADR's contract applies only to the loaders covered by this repo — currently BOE; BOA will follow.

## Per-workspace layout

Applied identically in each existing workspace (`Central Data & Integration (DEV|STG|PROD)`). No new workspaces or lakehouses are created.

```text
Central Data & Integration ({DEV|STG|PROD}).Workspace/
  lh_esp_origination.Lakehouse/
    Files/
      bronze/
        boe/raw/year=YYYY/month=MM/day=DD/
          BOE-A-YYYY-N.pdf            ← one PDF per relevant disposition
          sumario.json                ← daily index, full and unfiltered
        boe/raw/_manifests/year=YYYY/month=MM/day=DD/
          _manifest.json              ← run telemetry + per-item metadata
        boa/raw/...                   ← when BOA loader ships (same shape)
        endesa/raw/...                ← owned by Endesa loader (this ADR does not constrain)
        ree/raw/...                   ← owned by REE loader (this ADR does not constrain)
      silver/                         ← reserved; this loader does not write here
    Tables/                           ← reserved; this loader does not write here
```

## Conventions

### Partitioning

**Hive-style date partitions under `raw/`.** `year=YYYY/month=MM/day=DD/` with zero-padding so lexical sort equals chronological sort. Standard for daily automated loads — enables partition pruning when downstream code reads bronze with Spark or PyArrow.

The manual files already in `Files/bronze/boe/raw/` (pre-automation drops) stay where they are. Automated ingestion writes only into partitioned paths going forward.

### File naming

- **ASCII-safe identifiers as filenames.** BOE's `BOE-A-YYYY-N` and BOA's `BOA-YYYYMMDD-NN` are both ASCII; never derive a filename from user-supplied or free-text strings.
- **Leading-underscore folders for metadata.** `_manifests/`, plus any future `_schemas/`, `_attribution/`. Convention from Spark/Hive world — readers know to skip these when globbing data.

### Source naming

- Short, lowercase, no separator — `boe`, `boa`, `endesa`, `ree`. Stable identifier; treated as a primary key in code.
- A source name once chosen is **breaking to rename** (downstream code globs by these paths). Pick once and live with it.

### Format choice per source

- **BOE: PDF.** One PDF per disposition that passes the relevance filter. The sumario JSON is also stored (unfiltered, full daily index) as the audit trail for what was published vs what was selected. The PDF URL and the XML URL are both captured in the manifest for downstream replay.

## Manifest contract

The manifest is the **stable interface** between the loader and any downstream consumer. Downstream code reads the manifest first to know what landed; it does not glob blindly.

Path: `Files/bronze/{source}/raw/_manifests/year=YYYY/month=MM/day=DD/_manifest.json`

Schema (BOE):

```jsonc
{
  "schema_version": "1.0",
  "source":         "boe",
  "run": {
    "started_at":             "2026-06-02T07:00:00Z",
    "ended_at":               "2026-06-02T07:01:42Z",
    "date":                   "2026-06-02",
    "sumario_items_total":    312,
    "items_filtered_in":      14,
    "items_written":          14,
    "items_failed":           [],
    "attribution":            "Fuente de los datos: Agencia Estatal Boletín Oficial del Estado"
  },
  "items": [
    {
      "identifier":          "BOE-A-2026-XXXX",
      "section":             "III",
      "departamento_codigo": "9575",
      "departamento":        "Ministerio para la Transición Ecológica ...",
      "published_at":        "2026-06-02",
      "url_pdf":             "https://www.boe.es/boe/dias/2026/06/02/pdfs/BOE-A-2026-XXXX.pdf",
      "url_xml":             "https://www.boe.es/diario_boe/xml.php?id=BOE-A-2026-XXXX",
      "url_html":            "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-XXXX",
      "eli":                 null,
      "pdf_path":            "boe/raw/year=2026/month=06/day=02/BOE-A-2026-XXXX.pdf",
      "sha256":              "9e8d…",
      "size_bytes":          123456
    }
  ]
}
```

Field choices:

- **Paths are lakehouse-relative** (`boe/raw/...`), so the manifest is portable across workspaces — the workspace name doesn't appear in the value.
- **All three URL flavours** (`url_pdf`, `url_xml`, `url_html`) are recorded, even though we land only the PDF. This lets a future consumer fetch the XML on demand without re-walking the sumario.
- **`schema_version`** so the manifest can evolve without breaking older consumers; bumps are deliberate, reviewed via `_schemas/` JSON Schemas if/when those land.

## Auth and identity

- Loader runs in an ACA Job with a user-assigned managed identity (see [ADR-001](0001-compute-platform.md)).
- That MI is granted **workspace `Contributor`** on each Fabric workspace it writes into. Per environment grant: DEV, STG, PROD.
- The workspace grant is the only piece that lives outside Azure RBAC (it's done in the Fabric Admin Portal or workspace settings).

## What this ADR does **not** cover

- Silver / gold tier transformations
- Extracted/typed event schema
- The location, shape, or contents of Delta tables produced from bronze
- The BFF API, React app, or any consumer-side architecture
- Document Intelligence / Azure OpenAI / Foundry usage
- Maps, dashboards, daily insights

Those will be addressed in subsequent ADRs once the bronze landing is shipped and a downstream team is ready to consume from it.

## Verification

A downstream consumer should be able to do the following in any environment:

```python
# Pseudocode — illustrative
manifest = read_json(
    f"{lakehouse_root}/Files/bronze/boe/raw/_manifests/"
    f"year=2026/month=06/day=02/_manifest.json"
)
assert manifest["schema_version"] == "1.0"
assert manifest["source"] == "boe"

for item in manifest["items"]:
    pdf_bytes = read_bytes(f"{lakehouse_root}/Files/{item['pdf_path']}")
    assert sha256(pdf_bytes).hexdigest() == item["sha256"]
```

The loader emits a contract; the consumer validates it. If the contract changes, `schema_version` bumps and consumers can branch on the version.

## Open questions (deferred)

These are out of scope for this ADR but tracked here so they aren't forgotten when silver/gold work begins:

1. **Retention.** How long do we keep bronze files? Forever (regulatory provenance) or rolling window? Default position: forever for state-gazette material given low storage cost; revisit when storage growth becomes meaningful.
2. **Backfill of dev/stg.** Do we backfill historical BOE into all three workspaces, or just prod? Likely prod-only; dev/stg get small sample subsets via a separate backfill argument.
3. **Schema-version evolution policy.** Who signs off on bumping `schema_version`? Probably a code-owner review of a checked-in JSON Schema under `Files/bronze/_schemas/`.

## Reversibility

Changing the bronze layout post-launch is a breaking change for any downstream consumer that has started reading from it. Plan to nail this layout before the first prod run. Silver/gold layouts can evolve independently and are addressed later.

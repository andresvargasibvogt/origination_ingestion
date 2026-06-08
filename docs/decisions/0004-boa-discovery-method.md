# ADR-004 — BOA daily discovery method

- **Status:** Proposed — sequenced, contingent recommendation
- **Date:** 2026-05-29
- **Owners:** Data platform team
- **Related:** [ADR-001 compute platform](0001-compute-platform.md), [ADR-002 network posture](0002-network-posture.md)

## Context

BOA (Boletín Oficial de Aragón) has **no clean list API**. SPARQL on `opendata.aragon.es` has been stale since 2025-01-23; the CKAN dataset exposes no per-day URL; the only operational interface is the Angular SPA calendar at `boa.aragon.es`. Document IDs (MLKOB) are internal database identifiers, not derivable from the publication date.

The BOA deep-dive flags daily discovery as the single load-bearing technical decision for the BOA loader. Without a list endpoint, the loader must learn each day's MLKOB IDs somehow — and BOA is the only place in this pipeline where a headless-browser scenario is currently on the table.

[ADR-001](0001-compute-platform.md) was framed against **maximalist** Apify usage (host the whole scraper, store results in Apify Dataset, pull back via webhooks). Two narrower modes deserve separate evaluation:

- The `apify-client` SDK invoking an Apify-hosted Actor — *some* SaaS in the data path (Option D below).
- The Apify SDK / Crawlee for Python used as a **local library only** — *no* SaaS in the data path, the framework runs inside our container (Option C2 below).

The Apify Python SDK (Apache-2.0) is documented at <https://docs.apify.com/sdk/python/docs/overview>. It ships three independently usable layers: the **Platform** (cloud Actors), the **client SDK** (talks to the platform), and **Crawlee for Python** (local scraping framework). Local-only usage requires zero connection to Apify's cloud.

## Options

| ID | Option | Headless browser owner | Container | Cost | Vendor in data path |
|----|--------|-----------------------|-----------|------|---------------------|
| **A** | Publisher feed (email Servicio del BOA / Aragón open-data contact, ask for a JSON list endpoint) | None | Slim | €0 | None |
| **B** | Undocumented RSS / Atom / sitemap / SPA's own XHR endpoints (one-hour spike to find) | None | Slim | €0 | None |
| **C** | In-container Playwright on Microsoft's `playwright/python` base image, bare API | Us | +700 MB | €0 marginal | None |
| **C2** | In-container **Apify SDK / Crawlee for Python** (Apache-2.0) on Microsoft's `playwright/python` base image. Local storage; no calls to Apify cloud. | Us | Same as C + ~50 MB | €0 marginal | **None** — framework runs locally |
| **D** | "Narrow Apify" — Apify-hosted Actor for SPA render only, invoked via `apify-client` from our ACA Job; we fetch PDFs directly afterwards | Apify | Slim | $49/mo (Personal) or ~€5/mo (PAYG if available) | Apify — but **only** for the SPA index JSON; content stays in Azure |

A fifth option — **Maximalist Apify** (host the whole scraper + Apify Dataset + webhooks pulling to OneLake) — was evaluated and **rejected in [ADR-001](0001-compute-platform.md)**; not reopened here. Apify in the full-platform mode puts the vendor in the entire data path, which is incompatible with the network posture chosen in [ADR-002](0002-network-posture.md).

## Decision criteria

- Reliability of discovery (does the option exist? does it stay stable as the publisher evolves their UI?)
- Recurring cost
- Maintenance burden (Chromium CVE patching is the worst case)
- Network posture (alignment with ADR-002)
- Vendor risk and procurement overhead
- Data residency for the *content* (load-bearing) vs the *index* (low-sensitivity)
- Reversibility (if it fails or the vendor changes terms)

## Sequenced decision tree

```
Step 1  →  Option A: email Servicio del BOA / open-data Aragón contact.
            Cheap, polite, aligns with PSI / CC BY 4.0 spirit.
            Wait one week.

Step 2  →  Option B: one-hour spike — curl candidate RSS / Atom /
            sitemap URLs; capture XHR/fetch calls the SPA itself
            makes via browser dev tools.

If A or B succeeds → done. Use the discovered endpoint. ADR-004 is
                     closed; the BOA container stays slim and uses
                     no headless browser at all.

If neither resolves within a week, choose between:
   Step 3a → Option C2 (Apify SDK local — recommended fallback)
   Step 3b → Option C  (bare Playwright)
   Step 3c → Option D  (Narrow Apify, hosted Actor)
```

## Step 3 — C / C2 / D criteria (only if A and B fail)

| Criterion | Favors **C** (bare Playwright) | Favors **C2** (Apify SDK local) | Favors **D** (Narrow Apify) |
|-----------|-------------------------------|---------------------------------|------------------------------|
| ADR-002 = Option C strict ("no SaaS in data path") | ✓ | ✓ | |
| ADR-002 = Option A (public-tier accepted) — opens the door to SaaS | | | ✓ |
| Team has appetite to own a Chromium-equipped image (Defender for Containers will scan it) | ✓ | ✓ | |
| Team strongly prefers supply-chain simplification (smaller image, no Chromium CVE patching) | | | ✓ |
| Procurement onboarding of a new vendor is friction | ✓ | ✓ | |
| Pure-OSS dependency only (Apache-2.0 acceptable) | ✓ | ✓ | |
| Team values framework-managed retry / queue / dedup over hand-rolled `tenacity` code | | ✓ | ✓ |
| Likely to add more scrapers in the next 12 months (BOA is not the only one) | | ✓ | |
| BOA is the only headless-browser case forever | ✓ | | |
| Team values "burst onto Apify Platform" as a future option | | ✓ | ✓ |
| Recurring SaaS cost (~€5–49/mo) is acceptable | | | ✓ |
| Crawlee for Python maturity (released ~2024) is acceptable | | ✓ | (irrelevant — hosted) |

## Decision

**Default path:** A → B → **C2** (in-container Apify SDK / Crawlee for Python, local mode). Reasoning:

- Same private posture as bare Playwright — no SaaS in the data path, all-OSS dependency stack (Apache-2.0).
- Battle-tested retry / request-queue / dedup primitives mean less code we own and less custom hand-rolling.
- If the team adds more scrapers later (other regional gazettes, MITECO dossier portals, distributor sites), the framework compounds; bare Playwright would force each new scraper to re-invent the basics.
- Switching costs in either direction (C ↔ C2, or C2 → D) remain low — a day's refactor.

**Fall back to C (bare Playwright) if** Crawlee for Python turns out to be unstable in our environment (it is younger than its Node.js sibling). In that case the Crawlee imports come out and the code reverts to bare `playwright.async_api`.

**Switch to D (Narrow Apify hosted) if** any of:

1. The team decides supply-chain simplification (no Chromium in our image) is a hard requirement.
2. ADR-002 downgrades to Option A (public-tier) — the SaaS-in-data-path argument weakens because the data path is already public.
3. The in-container approach (whether C or C2) proves unreliable in the first month (selectors break, SPA reskins, etc.) and we want a vendor to own the SPA-render problem.

## Operational note for Option D

If we ever take Option D, the procurement / governance work is:

1. Pick Apify region (Frankfurt for EU residency on paid plans).
2. Sign DPA — Apify acts as data processor for the index JSON only.
3. Vendor risk review — typically tractable; Apify is SOC 2 Type II.
4. Add `api.apify.com` to any egress allowlist (matters only under [ADR-002](0002-network-posture.md) Option D — Azure Firewall with FQDN rules).
5. Decide whether the Actor is dev-owned (we push code via Apify CLI to a private Actor) or community-actor-derived (less work, but the Actor's code is then someone else's to maintain).

## Reversibility

Low cost in either direction. Switching from C to D later is a small refactor — replace the in-container Playwright call with an `apify-client` call to a hosted Actor — about a day's work. Switching from D to C later is similar.

## Note on the maximalist Apify option

The "use Apify for everything" mode (host scraper, Apify Dataset, webhooks pulling to OneLake) remains rejected for the reasons in [ADR-001](0001-compute-platform.md): private-VNet bridging, recurring subscription floor, vendor in the full data path, procurement overhead. ADR-004 reopens *only* the narrow Apify mode where Apify renders the SPA and we keep the rest of the pipeline in Azure.

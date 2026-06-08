# ADR-001 — Compute platform for daily BOE ingestion

- **Status:** Proposed
- **Date:** 2026-05-29
- **Owners:** Data platform team
- **Related:** [ADR-002 network posture](0002-network-posture.md), [ADR-003 supply-chain controls](0003-supply-chain-controls.md)

## Context

BOE (Boletín Oficial del Estado) publishes a daily XML/JSON edition via an open Datos Abiertos API. We need a private-VNet workload that fetches the sumario and the per-item XML for the MITECO-renewable corpus (>50 MW state-competence projects) and lands it in Microsoft Fabric OneLake — daily forward plus historical backfill.

Compute options span Azure-native services and third-party SaaS scrapers. Apify was reopened explicitly because it's a well-known platform for this kind of work; this ADR records the reasoning for picking against it.

## Decision criteria

| Criterion | Weight | Why it matters here |
|-----------|--------|---------------------|
| Network fit | High | Enterprise private VNet + private endpoints is non-negotiable per platform policy. |
| Source posture fit | High | BOE has an open, cooperating API — anti-bot tooling is dead weight. |
| Total cost of ownership | High | Tiny workload (~5–20 items/day after filter); pay-per-use should beat subscription floors. |
| Operational burden | Medium | Idle 23 h/day means ops/patching cost dominates if non-serverless. |
| Data residency / governance | Medium | Sovereign data; EU-only data path; minimise processors. |
| Vendor risk + exit cost | Medium | Prefer portable artifacts and incumbent vendor. |

## Options compared

| Option | Network fit | Source fit | TCO (daily) | Ops | Governance | Exit cost |
|--------|-------------|------------|-------------|-----|------------|-----------|
| **Azure Container Apps Jobs** (chosen) | Native VNet + PEs | Excellent — code calls the open API directly | ≈ €5–15/mo + PE infra fee | Low — managed runtime, no OS | All-Azure, EU data path | Container is portable; OCI image runs anywhere |
| Azure Functions Consumption | VNet only on Premium/Flex tiers | Excellent | Cheapest, but tier upgrade kills the savings | Low | All-Azure | Functions runtime lock-in |
| Azure VM (IaaS) | Native | OK | High constant cost (~€30+/mo) | High — OS, patching, monitoring | All-Azure | Manual rebuild |
| Apify (maximalist) | SaaS in vendor cloud — needs bridging to VNet | Overkill — proxy / HBR / CAPTCHA-solving unused on BOE | $49/mo Personal floor + CU usage; $499/mo Team | Very low | New processor in data path; needs DPA + vendor risk review | Actor SDK lock-in; rewrite to leave |
| Bright Data / Zyte | Same as Apify | Overkill | Higher subscription floors | Very low | Same as Apify | Per-vendor lock-in |

## Decision

Use **Azure Container Apps (ACA) Jobs** in Spain Central, VNet-injected, writing to OneLake via the workspace's inbound private link, pulling images from the existing ACR via a private endpoint, identity via a user-assigned managed identity, observability into the existing Log Analytics workspace.

The same compute platform hosts the BOA loader (parallel source #2) using a different image. See [ADR-004](0004-boa-discovery-method.md) for the BOA-specific sub-decision about how to handle BOA's SPA discovery — that ADR re-examines a narrow form of Apify usage and reaches its own conclusion.

## Why not Apify (even on a fresh look)

1. **Network fit is the hard rule.** Apify runs in Apify's AWS infrastructure. Honouring "private VNet + no public storage egress" means either exposing outbound from the VNet to Apify's API (defeats the private posture) or building a webhook → public-endpoint → bridge path (adds a public hop into a regulated data plane). Neither is acceptable when an in-VNet option exists.
2. **We pay for capabilities we cannot use.** Apify's value is its anti-bot stack: proxy rotation, headless-browser farms, CAPTCHA solving, fingerprint rotation. BOE has none of those defences. We would be paying premium SaaS for features that never fire.
3. **Cost shape is wrong at this volume.** Apify Personal starts at $49/mo before usage; Team is $499/mo. ACA Jobs running daily costs ≈ €5–15/mo on consumption, scaling to zero between runs.
4. **Governance overhead.** New SaaS vendor → procurement onboarding, vendor risk assessment, DPA, periodic vendor review, additional data subject path to document for GDPR. ACA Jobs adds nothing — the enterprise Azure agreement already covers it.

## Where Apify *would* win (kept for future judgment)

- If we later need to scrape sources that **actively block** (anti-bot, JS-heavy SPAs without APIs, sites requiring CAPTCHA solving, sites that need residential proxies). Then Apify's bag of tricks earns its price.
- If the project loses its in-house Azure ops capacity and wants full vendor outsourcing.

Neither applies to BOE today. The BOA case is more nuanced — see [ADR-004](0004-boa-discovery-method.md) for the narrow-Apify reconsideration.

## Reversibility

Medium-easy. The Python container, the BOE client code, and the OneLake writer have no Azure-specific contracts beyond `DefaultAzureCredential` and `azure-storage-file-datalake`. Re-targeting to AKS, on-prem, or even Apify is a rewrite of the deployment surface, not the application.

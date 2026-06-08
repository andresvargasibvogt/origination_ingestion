# ADR-002 — Network posture (private VNet vs public-tier)

- **Status:** Proposed — contingent recommendation
- **Date:** 2026-05-29
- **Owners:** Data platform team + Platform / security team
- **Related:** [ADR-001 compute platform](0001-compute-platform.md), [ADR-003 supply-chain controls](0003-supply-chain-controls.md), [ADR-004 BOA discovery](0004-boa-discovery-method.md)

## Context

Both the source (BOE / BOA) and the data we ingest are fully public. There is no upstream confidentiality requirement. The only asset worth defending in this pipeline is **OneLake itself** — because the same Fabric workspace likely holds (or will hold) other data classes alongside this corpus, and because a leaked managed-identity token is a credential leak we want to contain.

This ADR exists because "everything must be in a private VNet" is a common enterprise default that may or may not be a real policy requirement. Without a real requirement, the simpler posture is materially cheaper and faster to ship without changing the risk model for this particular corpus.

## Decision criteria

| Criterion | What it means for this workload |
|-----------|---------------------------------|
| Source data sensitivity | BOE and BOA are public-domain. Zero. |
| Sink data sensitivity | The OneLake workspace may co-locate other classes — assume **moderate** unless confirmed otherwise. |
| Token blast radius | If a MI / SP token leaks, what's the geographic + network scope of damage? |
| Egress observability | Can the security team audit what we sent to whom? |
| Policy alignment | Is there an Azure Policy that *denies* public-network resources or *requires* private endpoints? |
| Complexity / time-to-deploy | Bicep modules, day-2 ops, and time-to-first-deploy. |
| Recurring cost | €/month of network plumbing on top of compute + storage. |
| Compliance ceiling | What's the highest assurance standard this can pass? ISO 27001, ENS, SOC 2, GDPR Art. 32. |

## Options compared

| # | Posture | Token blast radius | Egress observability | Policy fit | TTD | Recurring € | Compliance ceiling |
|---|---------|--------------------|----------------------|------------|------|-------------|--------------------|
| **A** | **Public ACA Job + public OneLake (MI auth only)** | Leaked token usable from anywhere on the internet until expiry. | None at network layer; only Fabric audit logs. | Fails any policy that denies public storage. | ~2 hours. | €0 network. | Low — relies entirely on identity-plane control. |
| **B** | Public ACA Job + OneLake behind workspace inbound access protection (no compute VNet) | Token usable only from approved IPs/services configured on the workspace. | Limited; ACA in multi-tenant pool. | Partial — workspace is private, compute is public. | ~4 hours. | ~€0–10. | Medium. Workable but awkward — most teams either go fully public or fully private. |
| **C** *(plan's current proposal)* | **VNet-injected ACA Job + OneLake workspace private link + ACR PE** | Token usable only from inside our spoke VNet. PE on OneLake means workspace FQDN resolves to a private IP from the spoke. | Full — VNet flow logs capture all egress. | Aligns with "VNet-only prod" policies. | ~1–2 days (incl. Fabric admin handshake). | ~€10–30. | High — ISO 27001, ENS, SOC 2, GDPR Art. 32 all satisfied. |
| **D** | C + hub peering + NAT Gateway / Azure Firewall for deterministic, inspected egress | Same as C, plus static egress IP and L7 inspection of egress to `boe.es` / `boa.aragon.es`. | Full + L7. | Aligns with mature landing-zone policies. | ~3–5 days (depends on hub team's queue). | ~€30–80 (NAT) to €600+ (Firewall). | Highest. Required only by the most regulated estates. |

## Risk model per option

- **A** — Identity-plane only. If anything bypasses Microsoft Entra controls (token theft from CI, leaked PAT, misconfigured RBAC), an attacker can write or read against OneLake from any internet location. Defensible **only** if the Fabric workspace contains exclusively public-domain data and you actively rotate / monitor MI token usage.
- **B** — Locks down the storage tier but leaves compute exposed. Asymmetric — uncommon to deploy this way in practice. Mostly interesting as a transitional state.
- **C** — Network and identity planes both gate access. A leaked token without VNet connectivity to OneLake's private endpoint is useless. This is the default for any data plane shared with other classes of corporate data.
- **D** — Adds L4/L7 egress controls. Useful only if outbound to `boe.es` / `boa.aragon.es` needs to be inspected or if a static egress IP is required by a downstream consumer.

## Decision

**Default: Option C.** It is the smallest configuration that satisfies a typical enterprise security baseline without overbuilding.

Three yes/no checks for the platform / security team that flip the default:

1. Is there an Azure Policy in your subscription that denies public-network resources? (`az policy assignment list --scope /subscriptions/<id> --query "[?contains(policyDefinitionId, 'private')]"`)
2. Is there a stated security baseline requiring "all prod workloads must be VNet-injected"?
3. Will OneLake ever land sensitive data alongside this BOE / BOA corpus?

If **all three are "no"** → downgrade to **Option A** (public-tier). Same workload code, ~€10–30/mo saved, ~1 day of infra work removed.

If **any is "yes"** → stay on **Option C**.

**Upgrade to Option D** only if a downstream consumer requires a static, whitelisted egress IP, or if the security team requires L7 inspection of outbound traffic to BOE / BOA. Neither is on the table today.

## Important note — ADR-002 does NOT defend against supply-chain attacks

A leaked MI token used from inside our container is a different threat from a leaked MI token used from an attacker's laptop. Option C blocks the second but not the first. If the threat we worry about is a malicious Python package exfiltrating data or tokens from inside the container, **the relevant decision is [ADR-003 supply-chain controls](0003-supply-chain-controls.md)**, not this one. The two ADRs compose; neither replaces the other.

## Reversibility

Switching A ↔ C later is a Bicep refactor — Container Apps environments can be created with or without VNet integration, but **converting an existing environment is destructive** (must be recreated). Plan to deploy the chosen posture from day one; switching later means a fresh environment and re-deploying the Jobs. Estimate ~half a day if needed.

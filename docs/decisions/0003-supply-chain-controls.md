# ADR-003 — Supply-chain controls

- **Status:** Proposed
- **Date:** 2026-05-29
- **Owners:** Data platform team
- **Related:** [ADR-001 compute platform](0001-compute-platform.md), [ADR-002 network posture](0002-network-posture.md)

## Context

The ingest container will pull dependencies from PyPI (`httpx`, `lxml`, `azure-identity`, `azure-storage-file-datalake`, `tenacity`, `pyyaml`, `structlog`, plus Playwright for the BOA loader). Any of those — or one of their transitive dependencies — could ship malicious code via a compromised maintainer account, a typo-squat we accidentally install, or a poisoned release.

Inside the container, malicious code runs with the same privileges as the loader: it can read the managed-identity token from IMDS, hit OneLake, and exfiltrate to any reachable endpoint. **Network posture (ADR-002) does not prevent this on its own.** Supply-chain controls are a separate, layered defence we need *in addition to* whatever network posture is chosen.

## Threat model

| Attack | Mechanism | What stops it |
|--------|-----------|---------------|
| Compromised release of a real package | Attacker pushes a malicious version to a package we depend on | Pinned versions + hash verification |
| Typo-squat | We accidentally install `azuer-identity` (sic) | Curated mirror with name allowlist |
| New CVE in an existing dependency | A vulnerability gets disclosed for a package we ship | Image scanning + dependency-graph alerting |
| Build-time tampering | Attacker injects code into the image during CI | Provenance attestation + ACR content trust |
| Token exfiltration from inside the container | Malicious code dials `attacker.com` to leak the MI token | Egress allowlist (network) **+** scoped RBAC (identity) |

## Controls (layered, cheapest first)

| # | Control | Cost | ROI | Recommendation |
|---|---------|------|-----|---------------|
| 1 | **Pinned lock file with hashes** (`uv lock` or `pip-compile --generate-hashes`, install with `--require-hashes`) | None | Very high — blocks all "compromised release" attacks unless the attacker also forges a sha256 collision | **Must-have** |
| 2 | **Minimal base image** (`python:3.12-slim` for BOE; `mcr.microsoft.com/playwright/python` for BOA — both minimal for their needs) | None | Reduces post-compromise pivots; shrinks attack surface | **Must-have** |
| 3 | **Tight MI RBAC scope** (only the specific OneLake path; `AcrPull` only on the specific registry) | None | Bounds blast radius of a leaked token | **Must-have** |
| 4 | **Dependabot / Renovate** on the repo to auto-PR security updates | None | Surfaces CVEs in dependencies and lets us patch before they're exploited | **Must-have** |
| 5 | **ACR image scanning via Defender for Containers** (or built-in ACR vulnerability scanning) | ~€5–10/mo per registry | Blocks pushes with critical/high CVEs in the image; catches transitive issues | **Must-have** |
| 6 | **Curated package mirror** — Azure Artifacts feed or Artifactory proxying PyPI with an allowlist of approved packages | ~€5/user/mo on Azure Artifacts, free up to 2 GB | Blocks typo-squats and unknown-publisher packages at install time. Bigger investment if starting from scratch but pays compound interest across all projects. | **Should-have** if the org already runs one; build it later otherwise |
| 7 | **Image signing + verification** (Notation / Cosign, ACR content trust, ACA verifying signature on pull) | Free tooling; some CI integration work | Detects tampering between build and run. Less common than the above; high ceiling but high investment. | **Optional** for now; revisit when the org has signing infra |
| 8 | **Egress allowlist via Azure Firewall** (FQDN rules: only `*.boe.es`, `*.boa.aragon.es`, `*.fabric.microsoft.com`, `*.azurecr.io`, IMDS, MS Entra endpoints) | ~€600+/mo (Standard) or ~€200+/mo (Basic) | Network-layer block on token/data exfiltration. The only control that defeats a malicious package from sending data to `attacker.com`. | **Should-have if exfiltration is a stated concern**; tied to ADR-002 Option D |
| 9 | **SBOM publishing + storage** (CycloneDX or SPDX, generated at build, stored alongside the image) | Free | Lets the security team audit what's in production; required by some procurement standards (e.g. EU CRA). | **Should-have** — cheap and increasingly expected |

## Decision

Adopt the must-have stack (#1–5) as the target baseline. **For the pilot ship, #1–3 land in week 1 and #4–5 are explicitly deferred** (see "Pilot vs target state" below).

Target state (must-have once production):

- **`uv` for dependency management** with a checked-in lockfile that includes hashes; CI installs with `--require-hashes`.
- **Minimal base images** per loader (slim Python for BOE, Microsoft's Playwright image for BOA); build with multi-stage where it helps.
- **MI RBAC scoped** to the specific OneLake path and the specific ACR — nothing more.
- **Dependabot enabled** on the repo with security + version alerts on the PyPI and GitHub Actions ecosystems.
- **Defender for Containers** (or built-in ACR scanning) enabled on the registry; CI fails on critical/high CVEs.

Add #6 (curated mirror) and #9 (SBOM) when the org has the infrastructure — they're "should-have" not "must-have."

Add #8 (egress allowlist) only if [ADR-002](0002-network-posture.md) Option D is chosen *and* exfiltration is a stated threat the team wants to defend against. Otherwise this is overspend.

Add #7 (image signing) only when the rest of the org adopts container signing — it's not worth being the first.

## Pilot vs target state

The pilot ship deploys DEV manually (per [ADR-007](0007-compute-platform-confirmed-aca-jobs.md)'s az checklist) to prove the loader works against the live BOE API and writes to OneLake. CI / Dependabot / scanning are **not** part of the pilot.

| Control | Pilot (week 1) | Target (post-pilot) | Why deferred |
|---------|----------------|---------------------|---------------|
| **#1 Pinned `uv.lock` w/ hashes** | ✅ in repo | ✅ same | Free; no CI needed |
| **#2 Minimal base image** | ✅ in Dockerfile | ✅ same | Free; intrinsic to the image |
| **#3 Scoped MI RBAC** | ✅ in the deploy script | ✅ same | Free; one-time setup |
| **#4 Dependabot** | ⏳ deferred | ✅ add when team has bandwidth to review PRs | Toil reduction, not a release gate. The pilot has a small dep surface and stable libs. |
| **#5 Defender for Containers / ACR scanning** | ⏳ deferred | ✅ enable when CI workflow lands | Only meaningful if image pushes are gated — which requires CI. Pilot pushes images manually via `az acr build`. |
| **#9 SBOM publishing** | ⏳ deferred | ✅ optional, when procurement asks | Should-have, not must-have at our maturity |

**This is deliberate, not oversight.** Shipping the pilot first lets us prove the data path works against real BOE; the supply-chain hardening lands once the deploy pattern is settled. Defender + Dependabot + CI together are ~1 day of post-pilot work.

### Triggers to fast-track the deferred items

- **Multiple developers** start committing → enable CI to gate pushes.
- **A high/critical CVE** appears in a dep we ship → enable Defender immediately, not weeks later.
- **A second source loader** lands (BOA, Endesa, REE) → CI becomes the right vehicle for building both images consistently.

The pilot's image (`{acr}/boe-ingest:<git-sha>`) is identical with or without CI — the same `containers/boe.Dockerfile` + `uv.lock`. Adding CI later doesn't require rebuilding the image differently.

## Where this lives in the deliverables

Mostly **repo + CI** artifacts, not Bicep:

- `pyproject.toml` includes a `[tool.uv]` block; lockfile is `uv.lock` and is checked in.
- `containers/boe.Dockerfile` and `containers/boa.Dockerfile` use multi-stage with `--require-hashes` install.
- `.github/workflows/build-and-push.yml` runs lockfile sync check, builds, scans (Defender for Containers webhook or `trivy image`), pushes only on clean scan.
- `.github/dependabot.yml` enables PyPI + GitHub Actions ecosystems.
- `infra/modules/identity.bicep` reflects scoped RBAC (per ADR-001's resource list).

## Verification (CI-time)

Add a release-gate test: a CI job builds the image with an intentionally-known-CVE package added (kept in a separate `tests/cve-fixture/` requirements file), and confirms the scanner blocks the push. This proves the scanner is wired up correctly. Remove the fixture before merge — or keep it as a periodic smoke test in a separate workflow.

## Reversibility

All controls except #6 (curated mirror) and #7 (image signing) are configuration changes with low switching cost. The lockfile + scanning controls can be added or removed at any time without architectural impact. The curated mirror, once adopted, becomes part of the build path — adoption is a one-way move in practice (worth doing deliberately).

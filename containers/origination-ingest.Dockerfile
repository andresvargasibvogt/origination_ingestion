# Unified container image for the origination ingestion pipeline.
#
# Single image, multiple sources. Each ACA Job runs the same image with a
# different `command` override (boe-ingest, boa-ingest, boe-promoter, ...).
# Adding source N+1 = add the new src/{name}_ingest/ module + console-script
# entry in pyproject.toml + a new ACA Job — no new container.
#
# Base: python:3.12-slim. BOA discovery turned out to be pure HTTP (the SPA's
# JSON XHR is callable directly when the SECC-C parameter is omitted), so
# Chromium is NOT required. ~150 MB runtime image, same as the previous BOE-only
# image. If a future source forces a headless browser, that's the trigger
# for switching the base to mcr.microsoft.com/playwright/python.

ARG PYTHON_VERSION=3.12.7

# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Layer 1: dependencies only (rarely changes).
COPY pyproject.toml uv.lock /build/
RUN uv sync --frozen --no-install-project --no-dev

# Layer 2: install our packages (changes on every code edit).
# README.md is referenced in pyproject.toml; hatchling fails without it.
# --no-editable: install as real wheels so /opt/venv is self-contained
# (editable .pth would point back to /build/src/, which doesn't exist in
# the runtime stage — see week-1 recap gotcha).
COPY README.md /build/
COPY src/ /build/src/
RUN uv sync --frozen --no-dev --no-editable

# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Default lakehouse name; workspace name is set on each ACA Job.
    FABRIC_LAKEHOUSE_NAME=lh_esp_origination

# Non-root user — defence in depth even though ACA runs containers
# rootless by default.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /opt/venv /opt/venv

USER app

# No ENTRYPOINT — each ACA Job sets its own `command` to one of the console
# scripts installed by uv into /opt/venv/bin/ (which is on PATH):
#
#   command=["boe-ingest"]      args=["--date=today"]      → daily BOE Job
#   command=["boa-ingest"]      args=["--date=today"]      → daily BOA Job
#   command=["boe-promoter"]    args=[]                    → shared promoter Job
#
# `--help` is the default if the Job spec omits the command (useful for the
# `docker run` sanity check).
CMD ["boe-ingest", "--help"]

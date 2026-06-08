# Multi-stage build: builder installs deps from the locked manifest,
# runtime stage gets only the wheels + source + runtime Python.
# Image size: ~150 MB. Supply-chain controls per ADR-003.
#
# This Dockerfile uses plain COPY + RUN (no BuildKit `--mount` directives).
# `az acr build` uses classic Docker; the BuildKit cache-mount optimisation
# isn't available there. Build is still fast enough for our cadence
# (~weekly). If we ever switch to ACR Tasks YAML with DOCKER_BUILDKIT=1,
# we can reintroduce --mount=type=cache for uv cache reuse.

ARG PYTHON_VERSION=3.12.7

# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

# Pin uv to the same version used to generate uv.lock locally.
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Layer 1: dependency install (rarely changes — cached by Docker layer).
# --frozen: fail if uv.lock is out of sync with pyproject.toml (ADR-003 #1).
# --no-install-project: skip building our own package here; do it in layer 2.
# --no-dev: omit dev dependencies (pytest, ruff, mypy) — runtime stays slim.
COPY pyproject.toml uv.lock /build/
RUN uv sync --frozen --no-install-project --no-dev

# Layer 2: install our project (changes on every code edit).
# README.md is referenced in pyproject.toml; hatchling fails the build
# without it. Same for src/.
# --no-editable: install as a real wheel into the venv, not as a .pth
# pointing back to /build/src/. Editable installs break in the runtime
# stage because we only COPY /opt/venv across — /build/src/ doesn't exist.
COPY README.md /build/
COPY src/ /build/src/
RUN uv sync --frozen --no-dev --no-editable

# ─────────────────────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Default lakehouse name; workspace name is set on the ACA Job.
    FABRIC_LAKEHOUSE_NAME=lh_esp_origination

# Non-root user — defence in depth even though ACA runs containers
# rootless by default.
RUN groupadd --system --gid 1000 app && \
    useradd  --system --uid 1000 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /opt/venv /opt/venv

USER app

# Entry: argparse CLI in __main__.py. ACA Job overrides CMD with the
# actual run arguments (e.g. ["--date", "today"]) or switches the entry
# point to the promoter via `--command python --args -m boe_ingest.promoter`.
ENTRYPOINT ["python", "-m", "boe_ingest"]
CMD ["--help"]

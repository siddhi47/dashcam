# ---------------------------------------------------------------------------
# Stage 1 — build the Vite/React frontend bundle.
# ---------------------------------------------------------------------------
FROM node:20-alpine AS frontend-builder

WORKDIR /src

# Copy only the package manifests first so npm install layers cache cleanly
# when only source files change.
COPY webapp/frontend/package.json webapp/frontend/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY webapp/frontend/ ./
RUN npm run build
# Result: /src/dist

# ---------------------------------------------------------------------------
# Stage 2 — sync the Python backend's virtualenv via uv.
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS python-builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY shared ./shared
COPY capture ./capture
COPY webapp/backend ./webapp/backend

RUN uv sync --frozen --package oak-dashcam-webapp --no-dev

# ---------------------------------------------------------------------------
# Stage 3 — minimal runtime image.
# ---------------------------------------------------------------------------
FROM python:3.11-slim

# The backend reads segment files directly off the shared volume; no extra
# codecs or ffmpeg are needed on this side.

COPY --from=python-builder /app/.venv /app/.venv
COPY --from=python-builder /src /src
# The frontend bundle is mounted at `/app/frontend` — `__main__._resolve_frontend_dist`
# checks this exact path as its third fallback, so no env var is needed.
COPY --from=frontend-builder /src/dist /app/frontend

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /src

EXPOSE 8080
ENTRYPOINT ["python", "-m", "oak_dashcam_webapp", "--config", "/config/dashcam.yaml"]

# oak-dashcam

Multi-camera dashcam system for Raspberry Pi + Luxonis OAK cameras, with a web UI
for browsing, streaming, and downloading recordings.

See [CLAUDE.md](CLAUDE.md) for the full design.

## Layout

```
shared/          # Pydantic config + shared schemas
capture/         # Capture service (DepthAI → segmented MP4)
webapp/backend/  # FastAPI API for browsing/streaming clips
webapp/frontend/ # React + Vite SPA (tbd)
config/          # dashcam.yaml — single source of truth
deploy/          # docker-compose + systemd units
```

## Development

This repo uses a [uv](https://docs.astral.sh/uv/) workspace.

```bash
# install everything (shared + capture + webapp + dev tools)
uv sync

# run the capture service against the default config
uv run python -m oak_dashcam_capture --config config/dashcam.yaml

# run the webapp backend
uv run python -m oak_dashcam_webapp --config config/dashcam.yaml

# checks
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Deploy

```bash
docker compose -f deploy/docker-compose.yml up -d
```

FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /src
COPY pyproject.toml uv.lock ./
COPY shared ./shared
COPY capture ./capture
COPY webapp/backend ./webapp/backend

RUN uv sync --frozen --package oak-dashcam-capture --no-dev


FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libusb-1.0-0 \
    udev \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /src /src

ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /src

ENTRYPOINT ["python", "-m", "oak_dashcam_capture", "--config", "/config/dashcam.yaml"]

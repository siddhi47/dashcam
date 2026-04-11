from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn
from oak_dashcam_shared import load_config

from oak_dashcam_webapp.app import create_app

log = logging.getLogger("oak_dashcam_webapp")


def _resolve_frontend_dist(explicit: Path | None) -> Path | None:
    """Pick the directory to serve the built frontend from.

    Priority: CLI flag → `OAK_DASHCAM_FRONTEND_DIST` env var → docker image
    default (`/app/frontend`) → repo dev build (`webapp/frontend/dist`
    relative to the CWD). Returns `None` if none of those exist, in which
    case the backend runs API-only and you're expected to serve the
    frontend from a separate Vite dev server.
    """
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(explicit)
    env_path = os.environ.get("OAK_DASHCAM_FRONTEND_DIST")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path("/app/frontend"))
    candidates.append(Path("webapp/frontend/dist"))

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="oak-dashcam-webapp")
    parser.add_argument("--config", type=Path, default=Path("config/dashcam.yaml"))
    parser.add_argument(
        "--frontend-dist",
        type=Path,
        default=None,
        help="Path to the built frontend directory (index.html + assets/).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = load_config(args.config)
    frontend_dist = _resolve_frontend_dist(args.frontend_dist)
    if frontend_dist is None:
        log.info("no frontend bundle found; running API-only")

    host, _, port = config.webapp.bind.partition(":")
    uvicorn.run(
        create_app(config, frontend_dist=frontend_dist),
        host=host or "0.0.0.0",
        port=int(port or "8080"),
    )


if __name__ == "__main__":
    main()

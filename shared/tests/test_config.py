from pathlib import Path

import pytest
from oak_dashcam_shared import load_config
from oak_dashcam_shared.config import CameraRole, Codec, DashcamConfig, Resolution


def test_load_default_yaml() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_config(repo_root / "config" / "dashcam.yaml")
    assert isinstance(cfg, DashcamConfig)
    assert {c.id for c in cfg.cameras} == {"front", "rear"}
    front = next(c for c in cfg.cameras if c.id == "front")
    assert front.role is CameraRole.FRONT
    assert front.resolution is Resolution.R_1080P
    assert front.codec is Codec.H265


def test_duplicate_camera_ids_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        DashcamConfig.model_validate(
            {
                "cameras": [
                    {"id": "a", "role": "front"},
                    {"id": "a", "role": "rear"},
                ]
            }
        )


def test_bad_camera_id_rejected() -> None:
    with pytest.raises(ValueError, match="slug"):
        DashcamConfig.model_validate({"cameras": [{"id": "not a slug", "role": "front"}]})

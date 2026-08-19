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


def test_detection_defaults() -> None:
    cfg = DashcamConfig.model_validate({"cameras": [{"id": "a", "role": "front"}]})
    assert cfg.detection.enabled is True
    assert cfg.detection.s3 is None
    assert cfg.detection.model_dir is None
    assert cfg.detection.confidence_threshold == 0.5


def test_detection_s3_section_parses() -> None:
    cfg = DashcamConfig.model_validate(
        {
            "cameras": [{"id": "a", "role": "front"}],
            "detection": {
                "confidence_threshold": 0.7,
                "s3": {"bucket": "models", "prefix": "dashcam/", "region": "us-east-1"},
            },
        }
    )
    assert cfg.detection.s3 is not None
    assert cfg.detection.s3.bucket == "models"
    assert cfg.detection.s3.endpoint_url is None
    assert cfg.detection.confidence_threshold == 0.7


def test_detection_confidence_bounds_enforced() -> None:
    with pytest.raises(ValueError):
        DashcamConfig.model_validate(
            {
                "cameras": [{"id": "a", "role": "front"}],
                "detection": {"confidence_threshold": 1.5},
            }
        )

from __future__ import annotations
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import torch
from PIL import Image
from elo_bot.features.bases.fingerprints.artifacts import MANIFEST
from elo_bot.features.bases.fingerprints.slicing import Detection
from elo_bot.db.fingerprint_types import PIPELINE_ID
from elo_bot.features.bases.fingerprints.types import ImageInput
from tests._relative_geometry_fixture import points


def detections(
    data: list[tuple[str, float, float]] | None = None,
    halls: tuple[tuple[str, float, float], ...] = (),
    confidence: float = .9,
) -> tuple[Detection, ...]:
    data = points() if data is None else data
    return tuple(Detection(name, (x * 1000 - 2, y * 1000 - 2, x * 1000 + 2, y * 1000 + 2), confidence)
                 for name, x, y in [*data, *halls])

def tile_model(rows: list[list[float]]) -> SimpleNamespace:
    value = SimpleNamespace(names=dict(enumerate(MANIFEST["classes"])),
                            boxes=SimpleNamespace(data=torch.tensor(rows, dtype=torch.float32).reshape(-1, 6)))
    return SimpleNamespace(names=dict(enumerate(MANIFEST["classes"])), overrides={"task": "detect"},
                           predict=MagicMock(return_value=[value]))

class SahiExtractionStateMixin:
    def setUp(self) -> None:
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "source.png"
        with Image.new("RGB", (1000, 800)) as image:
            image.save(self.path)

    def source(self, pipeline: str = PIPELINE_ID, district: int = 1, revision: int = 0) -> ImageInput:
        return ImageInput(path=str(self.path), source_revision=revision, source_image_url="synthetic",
                          pipeline_id=pipeline, district_id=district)

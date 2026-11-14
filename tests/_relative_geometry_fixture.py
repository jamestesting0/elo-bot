"""Synthetic geometry with known correspondences; never image accuracy evidence."""
from types import SimpleNamespace
from elobot.features.bases.fingerprints.artifacts import MANIFEST
from elobot.features.bases.fingerprints.matcher import score_layout
from elobot.features.bases.fingerprints.types import ComparisonSummary, PIPELINE_ID

from datetime import UTC, datetime
import random

from elobot.features.bases.fingerprints.types import BaseFingerprint, PIPELINE_IDENTITIES


def points(count=30, seed=41):
    generator = random.Random(seed)
    classes = ["cannon", "tesla", "air_bomb", "crusher", "wizard_tower"]
    return [(classes[index % len(classes)], generator.uniform(.12, .70), generator.uniform(.12, .70))
            for index in range(count)]


def layout(data=None, *, version=2, district=1, width=1000, height=800, hall=False):
    data = points() if data is None else data
    hall_type = "capital_peak" if district == 0 else "district_hall"
    buildings = [{"type": name, "x": x * 1000 / width, "y": y * 1000 / height, "conf": .9}
                 for name, x, y in data]
    if version == 1:
        for building in buildings:
            building["x"] -= .1
            building["y"] -= .1
        buildings.append({"type": hall_type, "x": 0, "y": 0, "conf": 1})
    elif hall:
        buildings.append({"type": hall_type, "x": .1, "y": .1, "conf": 1})
    return BaseFingerprint.model_validate({
        "schema_version": version,
        "pipeline_id": "capital-v1-4d63ddd9" if version == 1 else PIPELINE_ID,
        "model_sha256": "4d63ddd9a95351c6cc9da0fe964e1e715357f268b9efa01ee3874d43d332a271",
        "extractor_revision": "elo-1" if version == 1 else PIPELINE_IDENTITIES[PIPELINE_ID][2],
        "coordinate_system": "hall-relative-image-xy-v1" if version == 1 else "image-normalized-xy-v2",
        "source_revision": 0, "source_image_sha256": "a" * 64, "source_image_url": "synthetic",
        "district_id": district, "generated_at": datetime(2026, 1, 1, tzinfo=UTC),
        "image_width": width, "image_height": height,
        "anchor_type": hall_type if version == 1 or hall else None, "anchor_found": version == 1 or hall,
        "total_detections": len(buildings), "confidence_threshold": .25, "inference_runs": 1,
        "expected_count_match": False, "warnings": [] if version == 1 or hall else ["hall_absent"],
        "buildings": buildings,
    })

def prediction(data, halls=(), confidence=.9):
    rows = [[x * 1000 - 2, y * 1000 - 2, x * 1000 + 2, y * 1000 + 2,
             confidence, MANIFEST["classes"].index(name)] for name, x, y in [*data, *halls]]
    tensor = SimpleNamespace(cpu=lambda: SimpleNamespace(tolist=lambda: rows))
    return SimpleNamespace(names=dict(enumerate(MANIFEST["classes"])), boxes=SimpleNamespace(data=tensor))

def summary():
    return ComparisonSummary(score=score_layout(layout(), layout(points()[2:]), False),
                             query_unmatched=2, candidate_unmatched=0)

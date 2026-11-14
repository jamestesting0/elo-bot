from elobot.features.bases.fingerprints.types import BaseFingerprint, Building, MODEL_SHA256, utcnow


def fingerprint(revision=0, buildings=None):
    buildings = buildings or [Building(type="district_hall", x=0, y=0, conf=1),
                              Building(type="cannon", x=0.1, y=0.2, conf=0.9)]
    return BaseFingerprint(
        pipeline_id="capital-v1-4d63ddd9", model_sha256=MODEL_SHA256, extractor_revision="elo-1",
        coordinate_system="hall-relative-image-xy-v1", source_revision=revision,
        source_image_sha256="a" * 64, source_image_url="https://cdn.example/base.png", district_id=1,
        generated_at=utcnow(), image_width=100, image_height=100, anchor_type="district_hall",
        total_detections=len(buildings), confidence_threshold=0.25, inference_runs=1,
        expected_count_match=False, warnings=["Unexpected count"], buildings=buildings,
    )

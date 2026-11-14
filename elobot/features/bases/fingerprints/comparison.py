import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageEnhance

from elobot.features.bases.fingerprints import extractor
from elobot.features.bases.fingerprints.artifacts import MANIFEST
from elobot.features.bases.fingerprints.comparison_matcher import align_comparison
from elobot.features.bases.fingerprints.extractor import decode_image, verify_source
from elobot.features.bases.fingerprints.presentation import render_panels
from elobot.features.bases.fingerprints.types import (
    HALL_TYPES,
    PIPELINE_ID,
    BaseFingerprint,
    Building,
    ComparisonFingerprint,
    ComparisonSummary,
    ExtractionResult,
    FingerprintError,
    ImageInput,
    utcnow,
)

if TYPE_CHECKING:
    from ultralytics import YOLO


PANEL_SIZE = (800, 760)


def difference_panel(image: Image.Image, result: ExtractionResult, matched: set[int]) -> Image.Image:
    fingerprint = result.fingerprint
    if (fingerprint is None or fingerprint.schema_version != 2 or
            image.size != (fingerprint.image_width, fingerprint.image_height) or
            len(result.bounds) != len(fingerprint.buildings)):
        raise FingerprintError("comparison_detections_invalid")
    original = image.copy()
    original.thumbnail(PANEL_SIZE, Image.Resampling.LANCZOS)
    panel = ImageEnhance.Brightness(original).enhance(.84)
    mask = Image.new("L", original.size, 0)
    mask_draw = ImageDraw.Draw(mask)
    for index, (building, bounds) in enumerate(zip(fingerprint.buildings, result.bounds, strict=True)):
        if index in matched or building.type in HALL_TYPES:
            continue
        x1, y1, x2, y2 = bounds
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise FingerprintError("comparison_detections_invalid")
        box = (max(0, round(x1 * original.width) - 3), max(0, round(y1 * original.height) - 3),
               min(original.width - 1, round(x2 * original.width) + 3),
               min(original.height - 1, round(y2 * original.height) + 3))
        mask_draw.rounded_rectangle(box, radius=5, fill=255)
    with ImageEnhance.Brightness(original).enhance(1.2) as highlighted:
        panel.paste(highlighted, mask=mask)
    mask.close()
    original.close()
    return panel


def render_comparison(paths: tuple[Path, Path], results: tuple[ExtractionResult, ExtractionResult],
                      pairs: tuple[tuple[int, int], ...], output: Path) -> None:
    panels = []
    try:
        for side, (path, result) in enumerate(zip(paths, results, strict=True)):
            with decode_image(path) as image:
                panels.append(difference_panel(image, result, {pair[side] for pair in pairs}))
        render_panels((panels[0], panels[1]), output)
    finally:
        for panel in panels:
            panel.close()


def comparison_detections(model: "YOLO", source: ImageInput, device: str
                          ) -> tuple[ComparisonFingerprint, list[tuple[float, float, float, float]]]:
    with decode_image(Path(source.path)) as image:
        width, height = image.size
        detections = tuple(box for box in extractor.sliced_detections(model, image, device)
                           if box.label not in HALL_TYPES)
    fingerprint = ComparisonFingerprint(
        schema_version=2, pipeline_id=PIPELINE_ID, model_sha256=MANIFEST["sha256"],
        extractor_revision=MANIFEST["extractor_revision"], coordinate_system=MANIFEST["coordinate_system"],
        source_revision=source.source_revision, source_image_sha256=hashlib.sha256(
            Path(source.path).read_bytes()).hexdigest(), source_image_url=source.source_image_url,
        district_id=source.district_id, generated_at=utcnow(), image_width=width, image_height=height,
        anchor_type=None, anchor_found=False, total_detections=len(detections),
        confidence_threshold=MANIFEST["confidence"], inference_runs=1, expected_count_match=False,
        buildings=[Building(type=box.label, x=(box.xyxy[0] + box.xyxy[2]) / (2 * width),
                            y=(box.xyxy[1] + box.xyxy[3]) / (2 * height), conf=round(box.score, 3))
                   for box in detections],
    )
    bounds = [(x1 / width, y1 / height, x2 / width, y2 / height)
              for box in detections for x1, y1, x2, y2 in [box.xyxy]]
    return fingerprint, bounds


def create_comparison(model: "YOLO", inputs: list[ImageInput], expected: tuple[BaseFingerprint, BaseFingerprint],
                      device: str, fuzzy: bool) -> ComparisonSummary:
    if len(inputs) != 2:
        raise FingerprintError("comparison_input_invalid")
    for source, fingerprint in zip(inputs, expected, strict=True):
        verify_source(source, fingerprint)
    detected = tuple(comparison_detections(model, source, device) for source in inputs)
    query, candidate = (fingerprint for fingerprint, _ in detected)
    results = tuple(ExtractionResult(fingerprint=fingerprint, bounds=bounds) for fingerprint, bounds in detected)
    alignment = align_comparison(query, candidate, fuzzy)
    counts = [sum(building.type not in HALL_TYPES for building in fp.buildings) for fp in (query, candidate)]
    summary = ComparisonSummary(score=alignment.score, query_unmatched=counts[0] - len(alignment.pairs),
                                candidate_unmatched=counts[1] - len(alignment.pairs))
    paths = tuple(Path(source.path) for source in inputs)
    render_comparison(paths, results, alignment.pairs, paths[0].parent / "comparison.png")
    return summary

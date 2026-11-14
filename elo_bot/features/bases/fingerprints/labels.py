from contextlib import ExitStack
from pathlib import Path

import numpy as np
from PIL import Image

from elo_bot.features.bases.fingerprints.artifacts import MANIFEST
from elo_bot.features.bases.fingerprints.extractor import decode_image, extract_batch, verify_source
from elo_bot.features.bases.fingerprints.presentation import render_panels
from elo_bot.db.fingerprint_types import BaseFingerprint
from elo_bot.features.bases.fingerprints.types import ExtractionResult, FingerprintError, ImageInput


def render_labels(path: Path, result: ExtractionResult, output: Path) -> None:
    from ultralytics.utils.plotting import Annotator, colors

    fingerprint = result.fingerprint
    if fingerprint is None or fingerprint.schema_version != 2 or len(result.bounds) != len(fingerprint.buildings):
        raise FingerprintError("labels_detections_invalid")
    with decode_image(path) as image:
        if image.size != (fingerprint.image_width, fingerprint.image_height):
            raise FingerprintError("labels_detections_invalid")
        scale = min(1600, max(1000, max(image.size))) / max(image.size)
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        with image.resize(size, Image.Resampling.LANCZOS) as resized:
            annotator = Annotator(np.ascontiguousarray(np.asarray(resized)[:, :, ::-1]), line_width=1, pil=False)
        for building, bounds in zip(fingerprint.buildings, result.bounds, strict=True):
            x1, y1, x2, y2 = bounds
            if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                raise FingerprintError("labels_detections_invalid")
            box = (x1 * size[0], y1 * size[1], min(size[0] - 1, x2 * size[0]), min(size[1] - 1, y2 * size[1]))
            annotator.box_label(box, f"{building.type} {building.conf:.2f}",
                                color=colors(MANIFEST["classes"].index(building.type), True))
        with Image.fromarray(annotator.result()[:, :, ::-1]) as labeled:
            labeled.save(output, format="PNG")


def create_labels(model, inputs: list[ImageInput], expected: tuple[BaseFingerprint, BaseFingerprint], device: str) -> None:
    if len(inputs) != 2 or inputs[0].district_id != inputs[1].district_id:
        raise FingerprintError("labels_input_invalid")
    for source, fingerprint in zip(inputs, expected, strict=True):
        verify_source(source, fingerprint)
    results = [extract_batch(model, [source], device, include_bounds=True)[0] for source in inputs]
    if any(result.fingerprint is None for result in results):
        raise FingerprintError("labels_extraction_failed: could not locate enough buildings")
    directory = Path(inputs[0].path).parent
    with ExitStack() as stack:
        panels = []
        for side, (source, result) in enumerate(zip(inputs, results, strict=True)):
            output = directory / f"labels-{side}.png"
            render_labels(Path(source.path), result, output)
            panels.append(stack.enter_context(Image.open(output)))
        render_panels((panels[0], panels[1]), directory / "labels.png")

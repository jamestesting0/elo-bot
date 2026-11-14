"""Verified two-column SAHI inference and deterministic cross-class overlap cleanup."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from elo_bot.features.bases.fingerprints.artifacts import MANIFEST
from elo_bot.features.bases.fingerprints.types import FingerprintError

if TYPE_CHECKING:
    import numpy as np
    from numpy.typing import NDArray
    from PIL import Image
    from ultralytics import YOLO


@dataclass(frozen=True, slots=True)
class Detection:
    label: str
    xyxy: tuple[float, float, float, float]
    score: float

    def __post_init__(self) -> None:
        a, b, c, d = self.xyxy
        if not all(math.isfinite(value) for value in (*self.xyxy, self.score)) or not (
            0 <= a < c and 0 <= b < d and 0 <= self.score <= 1
        ):
            raise FingerprintError("invalid_detection")


def overlap(left: Detection, right: Detection) -> float:
    """Use intersection over union, not intersection over the smaller box, for cleanup."""
    a, b = left.xyxy, right.xyxy
    intersection = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1])
    return intersection / (area - intersection)


def deduplicate(detections: tuple[Detection, ...], threshold: float, maximum: int) -> tuple[Detection, ...]:
    """Retain highest scores across classes with deterministic ties and a global output cap."""
    retained: list[Detection] = []
    for detection in sorted(detections, key=lambda item: (-item.score, item.label, item.xyxy)):
        if not any(overlap(detection, previous) >= threshold for previous in retained):
            retained.append(detection)
            if len(retained) == maximum:
                break
    return tuple(retained)


def sliced_detections(model: YOLO, image: Image.Image, device: str) -> tuple[Detection, ...]:
    """Match the measured RGB/BGR conversion, letterboxing, tile merge and final cleanup."""
    from sahi.models.ultralytics import UltralyticsDetectionModel
    from sahi.predict import get_sliced_prediction

    options = MANIFEST["sahi"]

    class VerifiedOptionsModel(UltralyticsDetectionModel):
        def perform_batch_inference(self, images: list[NDArray[np.uint8]]) -> None:
            predictions = model.predict(
                source=[pixels[:, :, ::-1] for pixels in images], device=device, batch=len(images),
                conf=MANIFEST["confidence"], iou=MANIFEST["iou"], imgsz=MANIFEST["imgsz"],
                max_det=MANIFEST["max_det"], rect=False, half=False, augment=False,
                save=False, save_txt=False, save_crop=False, verbose=False,
            )
            if len(predictions) != len(images):
                raise FingerprintError("inference_batch_mismatch")
            if any(list(prediction.names.values()) != MANIFEST["classes"] for prediction in predictions):
                raise FingerprintError("model_class_map_mismatch")
            self._original_predictions = self._extract_predictions(predictions)
            self._original_shapes = [pixels.shape for pixels in images]

    adapter = VerifiedOptionsModel(
        model=model, device=device, confidence_threshold=MANIFEST["confidence"], image_size=MANIFEST["imgsz"],
    )
    result = get_sliced_prediction(
        image, adapter, slice_height=image.height,
        slice_width=math.ceil(image.width / (options["columns"] - options["overlap"])),
        overlap_height_ratio=options["overlap"], overlap_width_ratio=options["overlap"],
        perform_standard_pred=options["standard_prediction"],
        postprocess_type=options["postprocess_type"], postprocess_match_metric=options["match_metric"],
        postprocess_match_threshold=options["match_threshold"],
        postprocess_class_agnostic=options["class_agnostic"], auto_slice_resolution=False,
        batch_size=options["tile_batch_size"], verbose=0, progress_bar=False,
    )
    detections = tuple(
        Detection(prediction.category.name, tuple(prediction.bbox.to_xyxy()), float(prediction.score.value))
        for prediction in result.object_prediction_list
    )
    return deduplicate(detections, options["cleanup_iou"], MANIFEST["max_det"])

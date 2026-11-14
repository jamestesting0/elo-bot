import hashlib
from collections import defaultdict
from pathlib import Path
from statistics import median

from PIL import Image, UnidentifiedImageError

from elobot.features.bases.fingerprints.artifacts import MANIFEST, MAX_IMAGE_BYTES, MAX_PIXELS, PIPELINE_MANIFESTS
from elobot.features.bases.fingerprints.slicing import sliced_detections
from elobot.features.bases.fingerprints.types import (
    BaseFingerprint, Building, ExtractionResult, FingerprintError, HALL_TYPES, ImageInput,
    LEGACY_PIPELINE_ID, PIPELINE_ID, PIPELINE_IDENTITIES, utcnow,
)


def decode_image(path: Path) -> Image.Image:
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise FingerprintError("image_too_large")
    try:
        with Image.open(path) as image:
            if image.width * image.height > MAX_PIXELS or image.width < 32 or image.height < 32:
                raise FingerprintError("invalid_image_dimensions")
            if image.format not in {"JPEG", "PNG", "WEBP"} or getattr(image, "is_animated", False):
                raise FingerprintError("unsupported_image")
            image.load()
            return image.convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise FingerprintError("invalid_image") from exc


def verify_source(source: ImageInput, expected: BaseFingerprint) -> None:
    if (source.pipeline_id != PIPELINE_ID or source.district_id != expected.district_id or
            source.source_revision != expected.source_revision or
            hashlib.sha256(Path(source.path).read_bytes()).hexdigest() != expected.source_image_sha256):
        raise FingerprintError("source_changed: run a new search")


def extract_batch(model, inputs: list[ImageInput], device: str, *, include_bounds: bool = False) -> list[ExtractionResult]:
    images = {}
    results = [ExtractionResult() for _ in inputs]
    for index, source in enumerate(inputs):
        if source.pipeline_id not in PIPELINE_IDENTITIES:
            results[index] = ExtractionResult(error="unsupported_pipeline")
            continue
        try:
            images[index] = decode_image(Path(source.path))
        except (FingerprintError, OSError) as exc:
            results[index] = ExtractionResult(error=str(exc) if isinstance(exc, FingerprintError) else "source_unavailable")
    settings = {index: PIPELINE_MANIFESTS[inputs[index].pipeline_id] for index in images}
    pending = {index: settings[index]["confidence"] for index in images}
    best = {}
    attempts = defaultdict(int)
    try:
        for _ in range(max(options["max_runs"] for options in PIPELINE_MANIFESTS.values())):
            groups = defaultdict(list)
            for index, confidence in pending.items():
                options = settings[index]
                if attempts[index] < options["max_runs"]:
                    groups[(confidence, options["iou"], options["imgsz"], options["max_det"],
                            "sahi" in options)].append(index)
            pending = {}
            for (confidence, iou, imgsz, max_det, sliced), indices in groups.items():
                if sliced:
                    names = MANIFEST["classes"]
                    detected = []
                    for index in indices:
                        found = sliced_detections(model, images[index], device)
                        if settings[index].get("hall_limit") == 1:
                            hall = min((box for box in found if box.label in HALL_TYPES), default=None,
                                       key=lambda box: (-box.score, box.label, box.xyxy))
                            found = tuple(box for box in found if box.label not in HALL_TYPES or box is hall)
                        detected.append((names, [[*box.xyxy, box.score, names.index(box.label)] for box in found]))
                else:
                    predictions = model.predict(
                        source=[images[index] for index in indices], device=device, batch=len(indices),
                        conf=confidence, iou=iou, imgsz=imgsz, max_det=max_det,
                        rect=False, half=False, augment=False, save=False, save_txt=False, save_crop=False, verbose=False,
                    )
                    if len(predictions) != len(indices):
                        raise FingerprintError("inference_batch_mismatch")
                    detected = [([prediction.names[i] for i in range(len(prediction.names))],
                                 prediction.boxes.data.cpu().tolist()) for prediction in predictions]
                for index, (names, detections) in zip(indices, detected, strict=True):
                    options = settings[index]
                    attempts[index] += 1
                    if names != MANIFEST["classes"]:
                        raise FingerprintError("model_class_map_mismatch")
                    width, height = images[index].size
                    boxes = [(names[int(row[5])], (row[0] + row[2]) / (2 * width),
                              (row[1] + row[3]) / (2 * height), row[4]) for row in detections]
                    anchors = [box for box in boxes if box[0] in HALL_TYPES]
                    nonhall = [box for box in boxes if box[0] not in HALL_TYPES]
                    legacy = inputs[index].pipeline_id == LEGACY_PIPELINE_ID
                    counts = options["expected_counts"][inputs[index].district_id]
                    observed = len(boxes) if legacy else len(nonhall)
                    expected_counts = counts if legacy else [count - 1 for count in counts]
                    difference = min(abs(observed - expected) for expected in expected_counts)
                    valid_anchor = len(anchors) == 1 and ((inputs[index].district_id == 0) ==
                                                          (anchors[0][0] == "capital_peak"))
                    floor = MANIFEST["extraction_floor"]
                    usable = valid_anchor if legacy else (
                        len(nonhall) >= floor["nonhall"] and len({box[0] for box in nonhall}) >= floor["classes"] and
                        len({box[1] for box in nonhall}) > 1 and len({box[2] for box in nonhall}) > 1)
                    rank = (difference, 0 if legacy or not nonhall else -median(box[3] for box in nonhall))
                    if usable and (index not in best or rank < best[index][0]):
                        bounds = [(row[0] / width, row[1] / height, row[2] / width, row[3] / height)
                                  for row in detections] if include_bounds else []
                        best[index] = rank, boxes, anchors, confidence, bounds
                    if options["max_runs"] == 1 or (difference == 0 and usable):
                        continue
                    closest = min(expected_counts, key=lambda expected: abs(expected - observed))
                    step = options["confidence_step"] * (1 if observed > closest else -1)
                    pending[index] = round(min(options["confidence_max"], max(
                        options["confidence_min"], confidence + step)), 2)
        for index in images:
            source = inputs[index]
            legacy = source.pipeline_id == LEGACY_PIPELINE_ID
            if index not in best:
                results[index] = ExtractionResult(error=("missing_or_ambiguous_hall" if legacy else
                                                        "insufficient_layout_evidence"))
                continue
            rank, boxes, anchors, confidence, bounds = best[index]
            expected = "capital_peak" if source.district_id == 0 else "district_hall"
            found = len(anchors) == 1 and anchors[0][0] == expected
            manifest = settings[index]
            warnings = []
            if not anchors:
                warnings.append("hall_absent")
            elif len(anchors) > 1:
                warnings.append("hall_multiple")
            elif not found:
                warnings.append("hall_wrong_class")
            origin = anchors[0][1:3] if legacy else (0, 0)
            fingerprint = BaseFingerprint(
                schema_version=1 if legacy else 2,
                pipeline_id=manifest["pipeline_id"], model_sha256=manifest["sha256"],
                extractor_revision=manifest["extractor_revision"], coordinate_system=manifest["coordinate_system"],
                source_revision=source.source_revision, source_image_url=source.source_image_url,
                source_image_sha256=hashlib.sha256(Path(source.path).read_bytes()).hexdigest(),
                district_id=source.district_id, generated_at=utcnow(),
                image_width=images[index].width, image_height=images[index].height,
                anchor_type=expected if found else None, anchor_found=found,
                total_detections=len(boxes), confidence_threshold=confidence,
                inference_runs=attempts[index], expected_count_match=rank[0] == 0,
                warnings=warnings,
                buildings=[Building(type=name, x=round(x - origin[0], 4) if legacy else x,
                                    y=round(y - origin[1], 4) if legacy else y,
                                    conf=round(conf, 3)) for name, x, y, conf in boxes],
            )
            results[index] = ExtractionResult(fingerprint=fingerprint, bounds=bounds)
        return results
    finally:
        for image in images.values():
            image.close()

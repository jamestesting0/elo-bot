import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import sys
import threading
import time

from pydantic import BaseModel, Field, ValidationError

from elobot.features.bases.fingerprints.artifacts import MANIFEST, MAX_IPC_BYTES, MODEL_DIR, SCRATCH, verify_model
from elobot.features.bases.fingerprints.types import BaseFingerprint, FingerprintError, ImageInput


class WorkerRequest(BaseModel):
    images: list[ImageInput] = Field(default_factory=list, max_length=32)
    query: BaseFingerprint | None = None
    candidates: list[BaseFingerprint] = Field(default_factory=list, max_length=100)
    fuzzy: bool = False
    ranking: bool = False
    compare: tuple[BaseFingerprint, BaseFingerprint] | None = None
    labels: tuple[BaseFingerprint, BaseFingerprint] | None = None


def guard_parent(parent: int) -> None:
    while os.getppid() == parent:
        time.sleep(1)
    os._exit(0)


def run(device: str, check: bool) -> None:
    threading.Thread(target=guard_parent, args=(os.getppid(),), daemon=True).start()
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    (MODEL_DIR / "ultralytics").mkdir(exist_ok=True)
    (MODEL_DIR / "matplotlib").mkdir(exist_ok=True)
    os.environ["YOLO_CONFIG_DIR"] = str(MODEL_DIR / "ultralytics")
    os.environ["MPLCONFIGDIR"] = str(MODEL_DIR / "matplotlib")
    os.environ["YOLO_OFFLINE"] = "true"
    with contextlib.ExitStack() as stack:
        if device != "cpu":
            lock = stack.enter_context((MODEL_DIR / "cuda.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise FingerprintError("gpu_busy") from exc
        path = verify_model()
        with contextlib.redirect_stdout(sys.stderr):
            import torch
            from PIL import Image
            from ultralytics import YOLO, settings
            from elobot.features.bases.fingerprints.extractor import extract_batch
            from elobot.features.bases.fingerprints.matcher import rank_layout, score_layout
            from elobot.features.bases.fingerprints.comparison import create_comparison
            from elobot.features.bases.fingerprints.labels import create_labels
            from elobot.features.bases.fingerprints.slicing import sliced_detections

            settings.update({"sync": False, "runs_dir": str(MODEL_DIR / "runs")})
            if device != "cpu" and not torch.cuda.is_available():
                raise FingerprintError("gpu_unavailable: batch_size greater than 1 requires CUDA")
            torch.set_num_threads(2)
            model = YOLO(str(path))
            if list(model.names.values()) != MANIFEST["classes"]:
                raise FingerprintError("model_class_map_mismatch")
            with Image.new("RGB", (64, 64)) as warmup:
                sliced_detections(model, warmup, device)
        print(json.dumps({"ready": True, "device": device, "gpu_available": torch.cuda.is_available()}), flush=True)
        if check:
            return
        while line := sys.stdin.buffer.readline(MAX_IPC_BYTES + 1):
            if len(line) > MAX_IPC_BYTES:
                raise FingerprintError("ipc_too_large")
            try:
                request = WorkerRequest.model_validate_json(line)
                if any(not Path(image.path).resolve().is_relative_to(SCRATCH.resolve())
                       for image in request.images):
                    raise FingerprintError("invalid_scratch_path")
                with contextlib.redirect_stdout(sys.stderr):
                    if request.labels is not None:
                        create_labels(model, request.images, request.labels, device)
                        response = {"labels": True}
                    elif request.compare is not None:
                        response = {"comparison": create_comparison(
                            model, request.images, request.compare, device, request.fuzzy).model_dump(mode="json")}
                    elif request.query is not None:
                        score = rank_layout if request.ranking else score_layout
                        response = {"scores": [score(request.query, base, request.fuzzy).model_dump(mode="json")
                                               for base in request.candidates]}
                    else:
                        response = {"results": [result.model_dump(mode="json") for result in
                                                 extract_batch(model, request.images, device)]}
                print(json.dumps(response, allow_nan=False), flush=True)
            except (FingerprintError, ValidationError, OSError, RuntimeError) as exc:
                code = str(exc) if isinstance(exc, FingerprintError) else type(exc).__name__
                print(json.dumps({"error": code}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", choices=["cpu", "cuda:0"], default="cpu")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        run(arguments.device, arguments.check)
    except (FingerprintError, OSError, RuntimeError, ImportError) as error:
        code = str(error) if isinstance(error, FingerprintError) else type(error).__name__
        print(json.dumps({"error": code}), flush=True)
        sys.exit(1)

import hashlib
import json
import os
import sys
from pathlib import Path

from elo_bot.features.bases.fingerprints.types import FingerprintError
from elo_bot.db.fingerprint_types import (
    LEGACY_PIPELINE_ID, MODEL_SHA256, PIPELINE_ID, PREVIOUS_PIPELINE_ID, PREVIOUS_SAHI_PIPELINE_ID,
)


ROOT = Path(__file__).resolve().parents[4]
MANIFEST = json.loads((Path(__file__).with_name("model_manifest.json")).read_text())
LEGACY_MANIFEST = json.loads((Path(__file__).with_name("legacy_manifest.json")).read_text())
PREVIOUS_MANIFEST = json.loads((Path(__file__).with_name("previous_manifest.json")).read_text())
PREVIOUS_SAHI_MANIFEST = json.loads((Path(__file__).with_name("previous_sahi_manifest.json")).read_text())
MANIFEST_SHA256 = "7010a197f07a25036ba739fa09594134722b308be2477b8a476db80577e9b654"
LEGACY_MANIFEST_SHA256 = "b33b43dbf04c7b6f86fd466ea3338f5593a0d24399e4a92a4d6e7c812fb312f9"
PREVIOUS_MANIFEST_SHA256 = "a6f6adcafcca4a779a3db3fc475235e49a78c0946e11f31ba0614b876c1bf63e"
PREVIOUS_SAHI_MANIFEST_SHA256 = "0db29de435fadf6e371c27d5f0ea9f501d10ca4ae2cfaf9c2a0dae1905182363"
PIPELINE_MANIFESTS = {
    PIPELINE_ID: MANIFEST, PREVIOUS_PIPELINE_ID: PREVIOUS_MANIFEST, LEGACY_PIPELINE_ID: LEGACY_MANIFEST,
    PREVIOUS_SAHI_PIPELINE_ID: PREVIOUS_SAHI_MANIFEST,
}
MODEL_DIR = Path(os.environ.get("BASE_FINDER_MODEL_DIR", "models/base_finder"))
if not MODEL_DIR.is_absolute():
    MODEL_DIR = ROOT / MODEL_DIR
MODEL_PATH = MODEL_DIR / "best.pt"
WORKER_PYTHON = Path(sys.executable)
SCRATCH = ROOT / "temp" / "base_fingerprints"
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 24_000_000
MAX_IPC_BYTES = 4 * 1024 * 1024


def verify_model() -> Path:
    manifest_digest = hashlib.sha256(Path(__file__).with_name("model_manifest.json").read_bytes()).hexdigest()
    legacy_digest = hashlib.sha256(Path(__file__).with_name("legacy_manifest.json").read_bytes()).hexdigest()
    previous_digest = hashlib.sha256(Path(__file__).with_name("previous_manifest.json").read_bytes()).hexdigest()
    previous_sahi_digest = hashlib.sha256(Path(__file__).with_name("previous_sahi_manifest.json").read_bytes()).hexdigest()
    if (manifest_digest != MANIFEST_SHA256 or MANIFEST["pipeline_id"] != PIPELINE_ID or
            MANIFEST["sha256"] != MODEL_SHA256 or legacy_digest != LEGACY_MANIFEST_SHA256 or
            LEGACY_MANIFEST["pipeline_id"] != LEGACY_PIPELINE_ID or
            previous_digest != PREVIOUS_MANIFEST_SHA256 or PREVIOUS_MANIFEST["pipeline_id"] != PREVIOUS_PIPELINE_ID or
            previous_sahi_digest != PREVIOUS_SAHI_MANIFEST_SHA256 or
            PREVIOUS_SAHI_MANIFEST["pipeline_id"] != PREVIOUS_SAHI_PIPELINE_ID):
        raise FingerprintError("pipeline_manifest_mismatch")
    try:
        if MODEL_PATH.stat().st_size != MANIFEST["bytes"]:
            raise FingerprintError("model_checksum_mismatch")
        with MODEL_PATH.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise FingerprintError("model_missing: run scripts/setup_base_finder.py download") from exc
    if digest != MODEL_SHA256:
        raise FingerprintError("model_checksum_mismatch")
    return MODEL_PATH

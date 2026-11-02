import argparse
import fcntl
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.base_fingerprints.artifacts import MANIFEST, MODEL_PATH, ROOT, WORKER_PYTHON, verify_model
from common.base_fingerprints.types import FingerprintError


def download() -> None:
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MODEL_PATH.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            verify_model()
            print(f"Verified installed model: {MODEL_PATH}")
            return
        except FingerprintError:
            pass
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=MODEL_PATH.parent, suffix=".partial", delete=False) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                size = 0
                with urllib.request.urlopen(MANIFEST["url"], timeout=30) as response:
                    if response.url != MANIFEST["url"] or "text/html" in response.headers.get("Content-Type", ""):
                        raise FingerprintError("unexpected_artifact_response")
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > MANIFEST["bytes"]:
                            raise FingerprintError("artifact_too_large")
                        digest.update(chunk)
                        output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if size != MANIFEST["bytes"] or digest.hexdigest() != MANIFEST["sha256"]:
                raise FingerprintError("model_checksum_mismatch")
            os.replace(temporary, MODEL_PATH)
            print(f"Installed verified model: {MODEL_PATH}")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["download", "check"])
    args = parser.parse_args()
    if args.action == "download":
        download()
        return
    verify_model()
    if not WORKER_PYTHON.is_file():
        raise FingerprintError("runtime_missing: restore the bot's Python environment and install requirements.txt")
    subprocess.run([str(WORKER_PYTHON), "-m", "common.base_fingerprints.worker", "--check"],
                   cwd=ROOT, check=True, timeout=120)


if __name__ == "__main__":
    try:
        main()
    except (FingerprintError, OSError, subprocess.SubprocessError) as error:
        sys.exit(str(error))

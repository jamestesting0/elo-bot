import asyncio
import json
import os

from pydantic import ValidationError

from elo_bot.features.bases.fingerprints.artifacts import MAX_IPC_BYTES, MODEL_DIR, ROOT, WORKER_PYTHON
from elo_bot.features.bases.fingerprints.types import ExtractionResult, FingerprintError, ImageInput


def selected_device(batch_size: int) -> str:
    if type(batch_size) is not int or not 1 <= batch_size <= 32:
        raise FingerprintError("batch_size must be an integer from 1 to 32")
    return "cpu" if batch_size == 1 else "cuda:0"


class InferenceWorker:
    def __init__(self, device: str, deployment: str):
        self.device = device
        self.deployment = deployment
        self.process = None
        self.lock = asyncio.Lock()
        self.waiting = 0

    async def start(self) -> None:
        if self.process is not None and self.process.returncode is None:
            return
        if not WORKER_PYTHON.is_file():
            raise FingerprintError("runtime_missing: restore the bot's Python environment and install requirements.txt")
        log_dir = MODEL_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / f"{self.deployment}-{self.device.replace(':', '-')}.log").open("wb") as log:
            self.process = await asyncio.create_subprocess_exec(
                str(WORKER_PYTHON), "-m", "elo_bot.features.bases.fingerprints.worker", "--device", self.device,
                cwd=ROOT, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=log, limit=MAX_IPC_BYTES + 1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        try:
            ready = await self._receive(120)
            if not ready.get("ready") or ready.get("device") != self.device:
                raise FingerprintError("worker_start_failed")
        except BaseException:
            await self.close()
            raise

    async def _receive(self, timeout: int) -> dict:
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
            if not line or len(line) > MAX_IPC_BYTES:
                raise FingerprintError("worker_disconnected")
            response = json.loads(line)
            if not isinstance(response, dict):
                raise FingerprintError("worker_invalid_response")
            if "error" in response:
                if not isinstance(response["error"], str):
                    raise FingerprintError("worker_invalid_response")
                raise FingerprintError(response["error"])
            return response
        except (ValueError, asyncio.TimeoutError) as exc:
            raise FingerprintError("worker_timeout_or_invalid_response") from exc

    async def request(self, payload: dict) -> dict:
        if self.waiting >= 8:
            raise FingerprintError("worker_busy: try again shortly")
        self.waiting += 1
        try:
            async with self.lock:
                await self.start()
                encoded = json.dumps(payload, allow_nan=False).encode() + b"\n"
                if len(encoded) > MAX_IPC_BYTES:
                    raise FingerprintError("request_too_large")
                try:
                    self.process.stdin.write(encoded)
                    await self.process.stdin.drain()
                    return await self._receive(180)
                except BaseException:
                    await self.close()
                    raise
        except (BrokenPipeError, ConnectionError) as exc:
            raise FingerprintError("worker_disconnected") from exc
        finally:
            self.waiting -= 1

    async def extract(self, images: list[ImageInput]) -> list[ExtractionResult]:
        response = await self.request({"images": [image.model_dump() for image in images]})
        try:
            results = [ExtractionResult.model_validate(item) for item in response["results"]]
        except (KeyError, TypeError, ValidationError) as exc:
            raise FingerprintError("worker_invalid_response") from exc
        if len(results) != len(images):
            raise FingerprintError("inference_batch_mismatch")
        for source, result in zip(images, results, strict=True):
            fingerprint = result.fingerprint
            if (fingerprint is None) == (result.error is None):
                raise FingerprintError("worker_invalid_response")
            if fingerprint and (fingerprint.pipeline_id != source.pipeline_id or
                                fingerprint.source_revision != source.source_revision or
                                fingerprint.district_id != source.district_id or
                                fingerprint.source_image_url != source.source_image_url):
                raise FingerprintError("inference_source_mismatch")
        return results

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

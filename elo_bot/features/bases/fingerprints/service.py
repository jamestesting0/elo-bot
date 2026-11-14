import asyncio
import logging
import shutil
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import interactions as ipy
from bson import ObjectId
from pymongo.errors import DuplicateKeyError, PyMongoError
from pydantic import ValidationError

from elo_bot.features.bases.fingerprints.artifacts import SCRATCH
from elo_bot.features.bases.fingerprints.images import download_image
from elo_bot.features.bases.fingerprints.ingestion import source_filter
from elo_bot.features.bases.fingerprints.jobs import claim_tasks, commit_result, enroll_batch, materialize_outbox, run_counts
from elo_bot.features.bases.fingerprints.runtime import InferenceWorker, selected_device
from elo_bot.features.bases.fingerprints.types import (
    DISTRICTS, ComparisonSummary, ExtractionResult, FingerprintError, ImageInput,
)
from elo_bot.db.fingerprint_types import PIPELINE_ID, PIPELINE_IDENTITIES, BaseFingerprint, ExtractionState, utcnow
from elo_bot.config.ids import BASE_SUBMISSION_CHANNEL, LVL1_ROLES, WHITELISTED_STAFFS
from elo_bot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout


async def read_rendered_image(path: Path) -> bytes:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise FingerprintError("rendered_image_too_large")
    content = await asyncio.to_thread(path.read_bytes)
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FingerprintError("worker_invalid_response")
    return content


class FingerprintService:
    def __init__(self, bot, deployment: str):
        self.bot = bot
        self.deployment = deployment
        self.owner = str(uuid4())
        self.cpu = InferenceWorker("cpu", deployment)
        self.gpu = InferenceWorker("cuda:0", deployment)
        self.run_lock = asyncio.Lock()
        self.loop_task = None
        self.last_publication = {}

    def start(self) -> None:
        if self.loop_task is None:
            self.loop_task = asyncio.create_task(self.run(), name="base-fingerprint-recovery")

    async def close(self) -> None:
        if self.loop_task is not None:
            self.loop_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.loop_task
            self.loop_task = None
        await self.cpu.close()
        await self.gpu.close()

    async def begin_run(self, requester_id: str, batch_size: int) -> tuple[BaseExtractionRun, bool]:
        device = selected_device(batch_size)
        existing = await BaseExtractionRun.find_one({"active": True})
        if existing:
            return existing, False
        async with self.run_lock:
            existing = await BaseExtractionRun.find_one({"active": True})
            if existing:
                return existing, False
            worker = self.cpu if device == "cpu" else self.gpu
            async with worker.lock:
                await worker.start()
            run = BaseExtractionRun(requester_id=requester_id, deployment=self.deployment,
                                    batch_size=batch_size, requested_device=device, actual_device=device,
                                    pipeline_id=PIPELINE_ID,
                                    cutoff=ObjectId.from_datetime(utcnow()))
            try:
                await run.insert()
            except DuplicateKeyError:
                existing = await BaseExtractionRun.find_one({"active": True})
                if existing is None:
                    raise
                return existing, False
            except PyMongoError:
                if device != "cpu":
                    await self.gpu.close()
                raise
            return run, True

    async def request_extraction(self, base: BaseLayout) -> tuple[ExtractionState, bool]:
        async with self.cpu.lock:
            await self.cpu.start()
        query = source_filter(base)
        query.update(quality=base.quality, builder_id=base.builder_id)
        current = await BaseLayout.find_one(query)
        if current is None:
            raise FingerprintError("Base changed or was deleted; select it again")
        previous = current.fingerprint_extraction
        if (previous and previous.status in {"pending", "running"} and previous.pipeline_id == PIPELINE_ID and
                previous.source_revision == current.image_revision):
            return previous, False
        state = ExtractionState(source_revision=current.image_revision, pipeline_id=PIPELINE_ID)
        query["fingerprint_extraction.request_id"] = previous.request_id if previous else None
        result = await current.get_pymongo_collection().update_one(
            query, {"$set": {"fingerprint_extraction": state.model_dump()}},
        )
        if result.matched_count != 1:
            raise FingerprintError("Base or extraction request changed; retry to retrieve its current status")
        return state, True

    async def status(self, run: BaseExtractionRun) -> str:
        counts = await run_counts(run.id)
        return (f"Fingerprint run `{run.id}`: **{run.status}**; device **{run.actual_device}**, "
                f"batch size **{run.batch_size}**. " + ", ".join(
                    f"{name}: {counts.get(name, 0)}" for name in
                    ("succeeded", "failed", "superseded", "deleted", "pending", "running")) +
                (". Enrollment in progress." if not run.enrollment_complete else "."))

    async def publish(self, run: BaseExtractionRun) -> None:
        text = await self.status(run)
        if self.last_publication.get(run.id) == text:
            return
        try:
            channel = await self.bot.fetch_channel(BASE_SUBMISSION_CHANNEL)
            guild = channel.guild
            everyone = guild.default_role
            everyone_overwrite = next((entry for entry in channel.permission_overwrites
                                       if int(entry.id) == int(everyone.id)), None)
            for role in guild.roles:
                if int(role.id) in LVL1_ROLES:
                    continue
                permissions = everyone.permissions | role.permissions
                if permissions & ipy.Permissions.ADMINISTRATOR:
                    raise FingerprintError("status_channel_not_staff_only")
                if everyone_overwrite:
                    permissions = (permissions & ~everyone_overwrite.deny) | everyone_overwrite.allow
                overwrite = next((entry for entry in channel.permission_overwrites
                                  if int(entry.id) == int(role.id)), None)
                if overwrite:
                    permissions = (permissions & ~overwrite.deny) | overwrite.allow
                if permissions & ipy.Permissions.VIEW_CHANNEL:
                    raise FingerprintError("status_channel_not_staff_only")
            if any(overwrite.type == ipy.OverwriteType.MEMBER and
                   int(overwrite.id) not in {*WHITELISTED_STAFFS, int(self.bot.user.id)} and
                   overwrite.allow & ipy.Permissions.VIEW_CHANNEL for overwrite in channel.permission_overwrites):
                raise FingerprintError("status_channel_not_staff_only")
            if run.notification_message_id:
                message = await channel.fetch_message(run.notification_message_id)
                await message.edit(content=text)
            else:
                message = await channel.send(text)
                await run.set({"notification_channel_id": str(channel.id), "notification_message_id": str(message.id)})
            await run.set({"notification_error": None})
            self.last_publication = {run.id: text}
        except (ipy.errors.HTTPException, FingerprintError) as exc:
            error_code = str(exc) if isinstance(exc, FingerprintError) else type(exc).__name__
            if run.notification_error != error_code:
                await run.set({"notification_error": error_code})
                logging.warning("Fingerprint run status unavailable for run %s: %s", run.id, error_code)

    async def query(self, url: str, district: str, attachment=None):
        directory = SCRATCH / self.deployment / f"query-{uuid4()}"
        directory.mkdir(parents=True)
        try:
            source_url = await download_image(self.bot, url, attachment, directory / "image")
            results = await self.cpu.extract([ImageInput(path=str(directory / "image"), source_revision=0,
                                                       source_image_url=source_url, district_id=DISTRICTS[district],
                                                       pipeline_id=PIPELINE_ID)])
            if results[0].fingerprint is None:
                raise FingerprintError(results[0].error or "invalid_query")
            return results[0].fingerprint
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)

    async def compare(self, query: BaseFingerprint, base: BaseLayout, *, fuzzy: bool, attachment=None
                      ) -> tuple[bytes, ComparisonSummary]:
        candidate = base.fingerprint
        if candidate is None:
            raise FingerprintError("comparison_source_unavailable")
        directory = SCRATCH / self.deployment / f"compare-{uuid4()}"
        try:
            directory.mkdir(parents=True)
            inputs = []
            for index, (fingerprint, url, identity) in enumerate((
                    (query, query.source_image_url, attachment),
                    (candidate, base.image_url, base.image_attachment))):
                path = directory / str(index)
                source_url = await download_image(self.bot, url, identity, path)
                inputs.append(ImageInput(path=str(path), source_revision=fingerprint.source_revision,
                                         source_image_url=source_url, district_id=fingerprint.district_id,
                                         pipeline_id=PIPELINE_ID))
            response = await self.cpu.request({"images": [source.model_dump() for source in inputs],
                                               "compare": [fp.model_dump(mode="json") for fp in (query, candidate)],
                                               "fuzzy": fuzzy})
            try:
                summary = ComparisonSummary.model_validate(response["comparison"])
            except (KeyError, TypeError, ValidationError) as exc:
                raise FingerprintError("worker_invalid_response") from exc
            content = await read_rendered_image(directory / "comparison.png")
            return content, summary
        except OSError as exc:
            raise FingerprintError("comparison_image_unavailable") from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)

    async def labels(self, query: BaseFingerprint, base: BaseLayout, *, attachment=None) -> bytes:
        fingerprint = base.fingerprint
        if fingerprint is None:
            raise FingerprintError("labels_source_unavailable")
        directory = SCRATCH / self.deployment / f"labels-{uuid4()}"
        try:
            directory.mkdir(parents=True)
            inputs = []
            for index, (expected, url, identity) in enumerate((
                    (query, query.source_image_url, attachment),
                    (fingerprint, base.image_url, base.image_attachment))):
                path = directory / str(index)
                source_url = await download_image(self.bot, url, identity, path)
                inputs.append(ImageInput(path=str(path), source_revision=expected.source_revision,
                                         source_image_url=source_url, district_id=expected.district_id,
                                         pipeline_id=PIPELINE_ID))
            response = await self.cpu.request({"images": [source.model_dump() for source in inputs],
                                               "labels": [fp.model_dump(mode="json") for fp in (query, fingerprint)]})
            if response.get("labels") is not True:
                raise FingerprintError("worker_invalid_response")
            return await read_rendered_image(directory / "labels.png")
        except OSError as exc:
            raise FingerprintError("labels_image_unavailable") from exc
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)

    async def process(self, tasks: list[BaseFingerprintTask], worker: InferenceWorker) -> None:
        inputs = []
        ready = []
        directories = []
        semaphore = asyncio.Semaphore(4)

        async def prepare(task):
            async with semaphore:
                directory = SCRATCH / self.deployment / f"{task.id}-{task.claim_token}"
                directory.mkdir(parents=True, exist_ok=True)
                directories.append(directory)
                base = await BaseLayout.get(task.base_id)
                if base is None:
                    await commit_result(task, ExtractionResult(error="source_unavailable"))
                    return
                try:
                    url = await download_image(self.bot, task.source_image_url, base.image_attachment, directory / "image")
                    source = ImageInput(path=str(directory / "image"), source_revision=task.source_revision,
                                        source_image_url=url, district_id=DISTRICTS[task.district],
                                        pipeline_id=task.pipeline_id)
                    inputs.append(source)
                    ready.append(task)
                except FingerprintError as exc:
                    await commit_result(task, ExtractionResult(error=str(exc)))

        try:
            async with asyncio.TaskGroup() as group:
                for task in tasks:
                    group.create_task(prepare(task))
            if inputs:
                try:
                    results = await worker.extract(inputs)
                except FingerprintError as exc:
                    results = [ExtractionResult(error=str(exc)) for _ in inputs]
                for task, result in zip(ready, results, strict=True):
                    await commit_result(task, result)
        finally:
            for directory in directories:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    async def clean_abandoned(self) -> None:
        root = SCRATCH / self.deployment
        if not root.exists():
            return
        cutoff = (utcnow() - timedelta(hours=1)).timestamp()
        for directory in root.iterdir():
            if directory.is_symlink() or not directory.is_dir() or directory.stat().st_mtime > cutoff:
                continue
            task_id = directory.name[:36]
            active = await BaseFingerprintTask.find_one({"_id": task_id, "state": "running",
                                                        "lease_expires_at": {"$gt": utcnow()}})
            if active is None:
                await asyncio.to_thread(shutil.rmtree, directory)

    async def tick(self) -> None:
        await materialize_outbox()
        automatic = await claim_tasks(None, 1, self.owner)
        if automatic:
            await self.process(automatic, self.cpu)
        async with self.run_lock:
            run = await BaseExtractionRun.find_one({"active": True})
            if run is None:
                return
            if run.pipeline_id not in PIPELINE_IDENTITIES:
                await run.set({"status": "waiting: restore the run's pipeline before continuing"})
                await self.publish(run)
                return
            worker = self.cpu if run.actual_device == "cpu" else self.gpu
            try:
                async with worker.lock:
                    await worker.start()
            except FingerprintError as exc:
                await run.set({"status": f"waiting: {exc}"})
                await self.publish(run)
                return
            if not run.enrollment_complete:
                await enroll_batch(run)
            tasks = await claim_tasks(run.id, run.batch_size, self.owner)
            if tasks:
                await run.set({"status": "running"})
                await self.process(tasks, worker)
            counts = await run_counts(run.id)
            if run.enrollment_complete and not counts.get("pending") and not counts.get("running"):
                await run.set({"active": False, "completed_at": utcnow(),
                               "status": "completed-with-errors" if counts.get("failed") else "completed"})
                await self.gpu.close()
            await self.publish(run)

    async def run(self) -> None:
        await self.clean_abandoned()
        while True:
            try:
                await self.tick()
            except Exception:
                logging.exception("Fingerprint recovery tick failed; durable work will be retried")
            await asyncio.sleep(5)

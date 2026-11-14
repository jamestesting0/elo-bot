"""Remove fingerprints while fencing prior requests and the active bulk run."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from elobot.features.bases.fingerprints.ingestion import source_filter
from elobot.features.bases.fingerprints.types import (
    BaseFingerprint,
    ExtractionState,
    FingerprintError,
    utcnow,
)
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout

REMOVAL_REASON: Final = "unextracted"


@dataclass(frozen=True, slots=True)
class FingerprintRemoval:
    removed: int = 0
    cancelled: int = 0
    changed: int = 0


@dataclass(frozen=True, slots=True)
class FingerprintRemovalProgress:
    processed: int
    total: int
    result: FingerprintRemoval


class RemovalSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)
    fingerprint: BaseFingerprint | None = None
    fingerprint_extraction: ExtractionState | None = None


class BulkRemovalError(FingerprintError):
    def __init__(self, progress: FingerprintRemoval) -> None:
        self.progress = progress
        super().__init__(f"Confirmed removal of {progress.removed} fingerprints and stopped {progress.cancelled} "
                         "pending extractions. Could not confirm the remaining removals; please retry")


async def _remove(base: BaseLayout, active_run_id: str | None) -> FingerprintRemoval:
    previous = base.fingerprint_extraction
    query = source_filter(base)
    query.update(quality=base.quality, builder_id=base.builder_id)
    query["fingerprint_extraction.request_id"] = previous.request_id if previous else None
    reason = f"{REMOVAL_REASON}:{active_run_id}" if active_run_id else REMOVAL_REASON
    cleared = ExtractionState(source_revision=base.image_revision, status="failed", error_code=reason,
                              completed_at=utcnow())
    before = await BaseLayout.get_pymongo_collection().find_one_and_update(
        query, {"$unset": {"fingerprint": ""}, "$set": {"fingerprint_extraction": cleared.model_dump()}},
        projection={"_id": 0, "fingerprint": 1, "fingerprint_extraction": 1},
        return_document=ReturnDocument.BEFORE,
    )
    if before is None:
        return FingerprintRemoval(changed=1)
    snapshot = RemovalSnapshot.model_validate(before)
    pending = snapshot.fingerprint_extraction
    return FingerprintRemoval(removed=int(snapshot.fingerprint is not None),
                              cancelled=int(pending is not None and pending.status in {"pending", "running"}))


async def remove_fingerprint(base: BaseLayout, lock: AbstractAsyncContextManager[None]) -> FingerprintRemoval:
    async with lock:
        run = await BaseExtractionRun.find_one({"active": True})
        result = await _remove(base, run.id if run else None)
    if result.changed:
        raise FingerprintError("Base or extraction request changed; select it again")
    return result


async def remove_all_fingerprints(
    lock: AbstractAsyncContextManager[None], release_gpu: Callable[[], Awaitable[None]],
    on_progress: Callable[[FingerprintRemovalProgress], Awaitable[None]] | None = None,
) -> FingerprintRemoval:
    progress = FingerprintRemoval()
    async with lock:
        try:
            run = await BaseExtractionRun.find_one({"active": True})
            last = await BaseLayout.find_all().sort("-_id").first_or_none()
            processed = total = 0
            if on_progress is not None:
                total = await BaseLayout.find({"_id": {"$lte": last.id}}).count() if last is not None else 0
                await on_progress(FingerprintRemovalProgress(processed, total, progress))
            cursor = None
            while last is not None:
                bounds = {"$lte": last.id}
                if cursor is not None:
                    bounds["$gt"] = cursor
                bases = await BaseLayout.find({"_id": bounds}).sort("_id").limit(100).to_list()
                for base in bases:
                    result = await _remove(base, run.id if run else None)
                    progress = FingerprintRemoval(progress.removed + result.removed,
                                                  progress.cancelled + result.cancelled,
                                                  progress.changed + result.changed)
                    cursor = base.id
                    processed += 1
                    if on_progress is not None:
                        await on_progress(FingerprintRemovalProgress(processed, total, progress))
                if len(bases) < 100:
                    break
            if run is not None:
                await run.set({"active": False, "enrollment_complete": True, "status": "cancelled: unextract_all",
                               "completed_at": utcnow()})
                await BaseFingerprintTask.get_pymongo_collection().update_many(
                    {"run_id": run.id, "state": {"$in": ["pending", "running"]}},
                    {"$set": {"state": "superseded", "error_code": REMOVAL_REASON, "completed_at": utcnow()}},
                )
                await release_gpu()
            return progress
        except PyMongoError as error:
            raise BulkRemovalError(progress) from error

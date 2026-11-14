from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from common.base_fingerprints.ingestion import source_filter
from common.base_fingerprints.removal import REMOVAL_REASON
from common.base_fingerprints.types import (
    DISTRICTS, ExtractionResult, ExtractionState, FingerprintError, LEGACY_PIPELINE_ID, PIPELINE_IDENTITIES, utcnow,
)
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout


async def materialize_outbox() -> None:
    async for base in BaseLayout.find({"fingerprint_extraction.status": {"$in": ["pending", "running"]}}):
        state = base.fingerprint_extraction
        task = BaseFingerprintTask(id=state.request_id, base_id=base.id, source_revision=state.source_revision,
                                   source_image_url=base.image_url, district=base.district, pipeline_id=state.pipeline_id)
        try:
            await task.insert()
        except DuplicateKeyError:
            pass


async def enroll_batch(run: BaseExtractionRun) -> None:
    query = {"_id": {"$lt": run.cutoff}, "fingerprint": None, "quality": {"$ne": "Private"},
             "fingerprint_extraction.error_code": {"$ne": f"{REMOVAL_REASON}:{run.id}"}}
    if run.enrollment_cursor is not None:
        query["_id"]["$gt"] = run.enrollment_cursor
    bases = await BaseLayout.find(query).sort("_id").limit(100).to_list()
    for base in bases:
        task = BaseFingerprintTask(
            base_id=base.id, source_revision=base.image_revision, source_image_url=base.image_url,
            district=base.district, run_id=run.id, pipeline_id=run.pipeline_id,
            predecessor_request_id=base.fingerprint_extraction.request_id if base.fingerprint_extraction else None,
        )
        try:
            await task.insert()
        except DuplicateKeyError:
            pass
        await run.set({"enrollment_cursor": base.id})
    if len(bases) < 100:
        await run.set({"enrollment_complete": True, "status": "running"})


async def finish_task(task: BaseFingerprintTask, state: str, error: str | None = None) -> None:
    await task.get_pymongo_collection().update_one(
        {"_id": task.id, "claim_token": task.claim_token},
        {"$set": {"state": state, "completed_at": utcnow(), "error_code": error,
                  "completion_receipt": (task.completion_receipt or task.claim_token) if state == "succeeded" else None}},
    )


async def claim_tasks(run_id: str | None, limit: int, owner: str) -> list[BaseFingerprintTask]:
    claimed = []
    collection = BaseFingerprintTask.get_pymongo_collection()
    for _ in range(limit):
        now = utcnow()
        raw = await collection.find_one_and_update(
            {"run_id": run_id, "$or": [
                {"state": "pending", "next_attempt_at": {"$lte": now}},
                {"state": "running", "lease_expires_at": {"$lte": now}},
            ]},
            {"$set": {"state": "running", "lease_owner": owner, "claim_token": str(uuid4()),
                      "lease_expires_at": now + timedelta(minutes=10)}, "$inc": {"attempts": 1}},
            sort=[("created_at", 1), ("_id", 1)], return_document=ReturnDocument.AFTER,
        )
        if raw is None:
            break
        task = BaseFingerprintTask.model_validate(raw)
        if task.pipeline_id not in PIPELINE_IDENTITIES:
            await collection.update_one({"_id": task.id, "claim_token": task.claim_token}, {
                "$set": {"state": "pending", "error_code": "unsupported_pipeline",
                         "next_attempt_at": now + timedelta(minutes=10)}, "$inc": {"attempts": -1}})
            continue
        base = await BaseLayout.get(task.base_id)
        if base is None:
            await finish_task(task, "deleted")
            continue
        state = base.fingerprint_extraction
        if (state and state.request_id == task.id and state.completion_receipt and
                state.pipeline_id == task.pipeline_id and state.source_revision == task.source_revision and
                base.image_revision == task.source_revision and base.district == task.district):
            task.completion_receipt = state.completion_receipt
            await finish_task(task, "succeeded" if state.status == "ready" else "failed", state.error_code)
            continue
        if task.run_id is not None and (base.quality == "Private" or base.fingerprint is not None or
                (state is not None and state.error_code == f"{REMOVAL_REASON}:{task.run_id}")):
            await finish_task(task, "superseded")
            continue
        valid_request = state is not None and state.request_id == task.id and state.pipeline_id == task.pipeline_id
        if task.run_id is not None:
            valid_request |= (state.request_id if state else None) == task.predecessor_request_id
        if (base.image_revision != task.source_revision or base.district != task.district or
                not valid_request):
            await finish_task(task, "superseded")
            continue
        query = source_filter(base)
        if task.run_id is not None:
            query["fingerprint"] = None
            query["quality"] = {"$ne": "Private"}
        query["fingerprint_extraction.request_id"] = state.request_id if state else None
        query["fingerprint_extraction.pipeline_id"] = state.pipeline_id if state else None
        if state and state.pipeline_id == LEGACY_PIPELINE_ID:
            query.pop("fingerprint_extraction.pipeline_id")
            query["$and"] = [{"$or": [{"fingerprint_extraction.pipeline_id": state.pipeline_id},
                                      {"fingerprint_extraction.pipeline_id": {"$exists": False}}]}]
        if state and state.status == "running" and state.lease_expires_at and state.request_id == task.id:
            query["fingerprint_extraction.claim_token"] = state.claim_token
        extraction = ExtractionState(request_id=task.id, source_revision=task.source_revision,
                                     pipeline_id=task.pipeline_id, status="running", claim_token=task.claim_token,
                                     lease_expires_at=task.lease_expires_at)
        result = await base.get_pymongo_collection().update_one(
            query, {"$set": {"fingerprint_extraction": extraction.model_dump()}},
        )
        if result.matched_count == 0:
            await finish_task(task, "superseded")
        elif task.attempts > 3:
            await commit_result(task, ExtractionResult(error="retry_limit"))
        else:
            claimed.append(task)
    return claimed


async def commit_result(task: BaseFingerprintTask, result: ExtractionResult) -> None:
    if (result.fingerprint is None) == (result.error is None):
        raise FingerprintError("invalid_extraction_result")
    now = utcnow()
    query = {
        "_id": task.base_id, "district": task.district,
        "fingerprint_extraction.request_id": task.id,
        "fingerprint_extraction.pipeline_id": task.pipeline_id,
        "fingerprint_extraction.source_revision": task.source_revision,
        "fingerprint_extraction.claim_token": task.claim_token,
        "fingerprint_extraction.lease_expires_at": {"$gt": now},
    }
    if task.run_id is not None:
        query["fingerprint"] = None
    if task.source_revision == 0:
        query["$or"] = [{"image_revision": 0}, {"image_revision": {"$exists": False}}]
    else:
        query["image_revision"] = task.source_revision
    fields = {
        "fingerprint_extraction.status": "ready" if result.fingerprint else "failed",
        "fingerprint_extraction.completed_at": now,
        "fingerprint_extraction.error_code": result.error,
        "fingerprint_extraction.completion_receipt": task.claim_token,
    }
    if result.fingerprint is not None:
        if (result.fingerprint.source_revision != task.source_revision or
                result.fingerprint.pipeline_id != task.pipeline_id or
                result.fingerprint.district_id != DISTRICTS[task.district]):
            raise ValueError("Worker result does not belong to task")
        fields["fingerprint"] = result.fingerprint.model_dump()
    written = await BaseLayout.get_pymongo_collection().update_one(query, {"$set": fields})
    if written.matched_count:
        await finish_task(task, "succeeded" if result.fingerprint else "failed", result.error)
    else:
        base = await BaseLayout.get(task.base_id)
        if task.run_id is not None and base is not None and base.fingerprint is not None:
            await finish_task(task, "superseded")
            return
        if (base and base.image_revision == task.source_revision and base.district == task.district and
                base.fingerprint_extraction and base.fingerprint_extraction.request_id == task.id and
                base.fingerprint_extraction.claim_token == task.claim_token):
            return
        await finish_task(task, "deleted" if base is None else "superseded")


async def run_counts(run_id: str) -> dict[str, int]:
    rows = await BaseFingerprintTask.get_pymongo_collection().aggregate([
        {"$match": {"run_id": run_id}}, {"$group": {"_id": "$state", "count": {"$sum": 1}}},
    ])
    return {row["_id"]: row["count"] async for row in rows}

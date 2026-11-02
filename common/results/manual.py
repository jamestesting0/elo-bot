import hashlib
import json
import secrets

from pymongo.errors import DuplicateKeyError, PyMongoError

from common.results.operations import ResultOperationConflict
from database_models import RecordedMatch


class ManualResultConfirmationUnknown(RuntimeError):
    def __init__(self, record_id: str) -> None:
        self.record_id = record_id
        super().__init__(f"Unable to verify the completion marker for manual record {record_id}")


def manual_result_key(
    actor_id: str,
    player_ids: list[str],
    response: dict[str, str],
    districts: list[str],
) -> str:
    payload = json.dumps([actor_id, player_ids, response, districts], sort_keys=True).encode()
    return "manual:" + hashlib.sha256(payload).hexdigest()


async def persist_manual_result(record: RecordedMatch, key: str) -> RecordedMatch:
    existing = await RecordedMatch.find_one({"manual_submission_key": key, "manual_submission_completed": {"$ne": True}})
    if existing is not None:
        return existing
    record.manual_submission_key = key
    record.operation_key = f"{key}:{secrets.token_hex(16)}"
    while True:
        try:
            await record.insert()
            return record
        except DuplicateKeyError:
            record.id = str(int(record.id) + 1)


async def complete_manual_result(record: RecordedMatch) -> None:
    query = {"_id": record.id, "operation_key": record.operation_key}
    try:
        result = await RecordedMatch.get_pymongo_collection().update_one(
            query, {"$set": {"manual_submission_completed": True}},
        )
    except PyMongoError:
        try:
            current = await RecordedMatch.find_one(query)
        except PyMongoError as read_error:
            raise ManualResultConfirmationUnknown(record.id) from read_error
        if current is not None and current.manual_submission_completed:
            return
        raise
    if not result.matched_count:
        raise ResultOperationConflict(
            record.operation_key or record.id,
            "The manual match record was deleted before submission completed.",
        )

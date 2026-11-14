import re

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from common.matches.season_stats import (
    RecordedStatsRow,
    SeasonStats,
    SeasonStatsError,
    SeasonWindow,
    StatsAccumulator,
)
from elobot.db import Match, RecordedMatch


class RecordIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    operation_key: str | None = None


class StatsOperation(BaseModel):
    model_config = ConfigDict(frozen=True)
    record: RecordIdentity | None = None
    previous_record: RecordIdentity | None = None
    completed: bool = False
    cancelled: bool = False


class StatsParent(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: PydanticObjectId = Field(alias="_id")
    result_operation: StatsOperation | None = None


def _parent_id(record: RecordedStatsRow) -> PydanticObjectId | None:
    parsed = re.fullmatch(r"([a-fA-F0-9]{24}):[^:]+", record.operation_key or "")
    return PydanticObjectId(parsed[1]) if parsed else None


async def load_season_stats(window: SeasonWindow) -> SeasonStats:
    query = {"time": {"$gt": window.start if window.start is not None else -1, "$lt": window.end}}
    projection = {
        "_id": 1, "time": 1, "matched_time": 1, "players": 1, "is_challenge": 1, "operation_key": 1,
        "manual_submission_key": 1, "manual_submission_completed": 1,
    }
    accumulator = StatsAccumulator()
    parents: dict[PydanticObjectId, StatsParent | None] = {}
    identities: dict[tuple[str, str | None], StatsOperation] = {}
    legacy_rows: list[RecordedStatsRow] = []
    rows: list[RecordedStatsRow] = []

    def add_official(row: RecordedStatsRow) -> None:
        parent = parents.get(_parent_id(row))
        operation = identities.get((row.id, row.operation_key))
        if operation is None and parent is not None:
            operation = parent.result_operation
        if operation is not None:
            effective = operation.record if operation.completed else operation.previous_record
            if (operation.cancelled or effective is None
                    or (effective.id, effective.operation_key) != (row.id, row.operation_key)):
                return
        accumulator.add(row, window)

    async def accumulate_batch() -> None:
        ids = {parent for row in rows if (parent := _parent_id(row)) is not None and parent not in parents}
        if ids:
            parents.update(dict.fromkeys(ids))
            parent_projection = {
                "_id": 1, "result_operation.record.id": 1, "result_operation.record.operation_key": 1,
                "result_operation.previous_record.id": 1, "result_operation.previous_record.operation_key": 1,
                "result_operation.completed": 1, "result_operation.cancelled": 1,
            }
            async with Match.get_pymongo_collection().find({"_id": {"$in": list(ids)}}, parent_projection) as cursor:
                async for document in cursor:
                    parent = StatsParent.model_validate(document)
                    parents[parent.id] = parent
                    if parent.result_operation is not None:
                        for identity in (parent.result_operation.record, parent.result_operation.previous_record):
                            if identity is not None:
                                identities[(identity.id, identity.operation_key)] = parent.result_operation
        for row in rows:
            if row.manual_submission_key and not row.manual_submission_completed:
                continue
            if row.operation_key is None:
                legacy_rows.append(row)
            else:
                add_official(row)

    try:
        async with RecordedMatch.get_pymongo_collection().find(query, projection) as cursor:
            async for document in cursor:
                rows.append(RecordedStatsRow.model_validate(document))
                if len(rows) == 100:
                    await accumulate_batch()
                    rows.clear()
            if rows:
                await accumulate_batch()
    except ValidationError as error:
        raise SeasonStatsError("Some stored match results could not be read.") from error
    for row in legacy_rows:
        add_official(row)
    return accumulator.finish(window)

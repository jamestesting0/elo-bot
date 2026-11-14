import secrets
from typing import Literal
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from elobot.db import BaseLayout, Match, Player, RecordedMatch
from elobot.db.result_models import MatchResultOperation, ResultBaseRollback, ResultRecordSnapshot
from elobot.features.results.records import remove_base_record
from elobot.config.emojis import LOSS_EMOJI, WIN_EMOJI
from elobot.core.parsing import get_destruction, get_stars


class ResultOperationConflict(RuntimeError):
    def __init__(self, token: str, message: str | None = None) -> None:
        self.token = token
        self.message = message or "The stored results changed. Review them before submitting again."
        super().__init__(self.message)


def result_input_error(match: Match) -> str | None:
    if getattr(match, "final_superseded", False) is True:
        return "This pairing was superseded. Use `/final match view` for its current contestants."
    if getattr(match, "format", None) == "elo_final_seven" and (
        match.final_result_operation is not None or match.final_result_id is not None
    ):
        return "This Final result is official. Use `/match resubmit` or `/final match result` to correct it."
    operation = match.result_operation
    if operation:
        if operation.cancelled:
            return "The recorded match was deleted and will not be recreated."
        if operation.completed:
            return "This match result is already recorded. Use `/match resubmit` to correct it."
        return "The match result is being recorded. Wait for that submission to finish before changing it."
    if match.autowin_winner_id and not match.autowin_completed:
        return "An auto-win is being recorded. Wait for it to finish before storing results."
    return None


async def cancel_result_operation(match: Match, recorded: RecordedMatch) -> None:
    operation = match.result_operation
    if not operation or not operation.record:
        return
    result = await Match.get_pymongo_collection().update_one(
        {"_id": match.id, "result_operation.key": operation.key,
         "result_operation.record.id": recorded.id, "result_operation.record.operation_key": recorded.operation_key},
        {"$set": {"result_operation.cancelled": True}},
    )
    if result.matched_count != 1:
        raise ResultOperationConflict(match.token)


async def completed_result_changes(match: Match) -> tuple[str, str]:
    operation = match.result_operation
    if not operation or not operation.record:
        raise ResultOperationConflict(match.token)
    recorded = await RecordedMatch.find_one(RecordedMatch.id == operation.record.id)
    if not recorded or recorded.operation_key != operation.record.operation_key:
        raise ResultOperationConflict(match.token, "The recorded match was deleted and will not be recreated.")
    changes = []
    for participant in match.participants[:2]:
        delta = recorded.players[participant.user_id].elo_change
        changes.append(f"{WIN_EMOJI if delta >= 0 else LOSS_EMOJI}{'+' if delta >= 0 else ''}{delta}")
    return changes[0], changes[1]


async def begin_result_operation(
        match: Match,
        response: dict[str, str],
        previous: RecordedMatch | None = None,
) -> MatchResultOperation:
    if match.autowin_winner_id and not match.autowin_completed:
        raise ResultOperationConflict(match.token)
    current = match.result_operation
    if current and current.cancelled:
        raise ResultOperationConflict(match.token, result_input_error(match))
    if current and (not current.completed or current.response == response):
        if current.response != response:
            raise ResultOperationConflict(match.token, result_input_error(match))
        return current
    if current and previous is None:
        raise ResultOperationConflict(match.token)
    rollbacks: list[ResultBaseRollback] = []
    if previous:
        for side, participant in enumerate(match.participants[:2]):
            opponent = match.participants[1 - side]
            for index, base in enumerate(participant.bases):
                if not base or not opponent.result[index]:
                    continue
                base_doc = await BaseLayout.find_one(BaseLayout.link == base.link)
                if not base_doc:
                    continue
                expected = [opponent.user_id, get_stars(opponent.result[index]), get_destruction(opponent.result[index])]
                row = next((row for row in reversed(base_doc.records) if row[:3] == expected), None)
                if row is not None:
                    rollbacks.append(ResultBaseRollback(base_id=base_doc.id, record=row))
    operation = MatchResultOperation(
        key=secrets.token_hex(16), response=response, created_at=datetime.now().timestamp(),
        previous_record=ResultRecordSnapshot.model_validate(previous.model_dump()) if previous else None,
        base_rollbacks=rollbacks,
        summary_message_id=current.summary_message_id if current else None,
        public_message_id=current.public_message_id if current else None,
    )
    result = await Match.get_pymongo_collection().update_one(
        {
            "_id": match.id, "result_operation.key": current.key if current else None,
            "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
            "participants.0.result": list(match.participants[0].result),
            "participants.1.result": list(match.participants[1].result),
        },
        {"$set": {
            "result_operation": operation.model_dump(),
            "participants.0.result": response["player1"].split("\n"),
            "participants.1.result": response["player2"].split("\n"),
        }},
    )
    if not result.matched_count:
        fresh = await Match.find_one(Match.id == match.id)
        if not fresh or not fresh.result_operation or fresh.result_operation.response != response:
            raise ResultOperationConflict(match.token)
        if fresh.autowin_winner_id and not fresh.autowin_completed:
            raise ResultOperationConflict(match.token)
        if fresh.result_operation.cancelled:
            raise ResultOperationConflict(match.token, result_input_error(fresh))
        operation = fresh.result_operation
    match.result_operation = operation
    for index, key in enumerate(("player1", "player2")):
        match.participants[index].result = operation.response[key].split("\n")
    saved = match.get_saved_state()
    if saved is not None:
        saved["result_operation"] = operation.model_dump()
        for index, participant in enumerate(match.participants[:2]):
            saved["participants"][index]["result"] = list(participant.result)
    return operation


async def persist_result_record(match: Match, candidate: RecordedMatch) -> RecordedMatch:
    operation = match.result_operation
    if not operation:
        raise ResultOperationConflict(match.token)
    if operation.cancelled:
        raise ResultOperationConflict(match.token, result_input_error(match))
    operation_key = f"{match.id}:{operation.key}"
    if operation.record is None:
        candidate.operation_key = operation_key
        snapshot = ResultRecordSnapshot.model_validate(candidate.model_dump())
        result = await Match.get_pymongo_collection().update_one(
            {"_id": match.id, "result_operation.key": operation.key, "result_operation.record": None},
            {"$set": {"result_operation.record": snapshot.model_dump()}},
        )
        if result.matched_count:
            operation.record = snapshot
        else:
            fresh = await Match.find_one(Match.id == match.id)
            if not fresh or not fresh.result_operation or fresh.result_operation.key != operation.key:
                raise ResultOperationConflict(match.token)
            operation.record = fresh.result_operation.record
    while operation.record:
        recorded = RecordedMatch.model_validate(operation.record.model_dump())
        try:
            await recorded.insert()
            return recorded
        except DuplicateKeyError:
            existing = await RecordedMatch.find_one(RecordedMatch.id == recorded.id)
            if existing and existing.operation_key == operation_key:
                return existing
            replacement_id = str(int(recorded.id) + 1)
            result = await Match.get_pymongo_collection().update_one(
                {"_id": match.id, "result_operation.key": operation.key, "result_operation.record.id": recorded.id},
                {"$set": {"result_operation.record.id": replacement_id}},
            )
            if result.matched_count:
                operation.record.id = replacement_id
            else:
                fresh = await Match.find_one(Match.id == match.id)
                if not fresh or not fresh.result_operation or fresh.result_operation.key != operation.key:
                    raise ResultOperationConflict(match.token)
                operation.record = fresh.result_operation.record
    raise ResultOperationConflict(match.token)


async def rollback_result_operation(match: Match) -> None:
    operation = match.result_operation
    if not operation or not operation.previous_record:
        return
    previous = RecordedMatch.model_validate(operation.previous_record.model_dump())
    for player_id, stats in previous.players.items():
        await Player.get_pymongo_collection().update_one(
            {"user_id": player_id, "matches": previous.to_ref()},
            {"$inc": {"elo": -stats.elo_change}, "$pull": {"matches": previous.to_ref()}},
        )
    for index, rollback in enumerate(operation.base_rollbacks):
        base = await BaseLayout.find_one(BaseLayout.id == rollback.base_id)
        if base:
            await remove_base_record(base, rollback.record, receipt=f"{operation.key}:rollback:{index}")


async def complete_result_operation(match: Match) -> None:
    operation = match.result_operation
    if not operation or not operation.record:
        raise ResultOperationConflict(match.token)
    if operation.previous_record:
        await RecordedMatch.model_validate(operation.previous_record.model_dump()).delete()
    result = await Match.get_pymongo_collection().update_one(
        {
            "_id": match.id, "result_operation.key": operation.key,
            "result_operation.cancelled": {"$ne": True},
            "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
        },
        {"$set": {"result_operation.completed": True, "recorded_match_id": operation.record.id}},
    )
    if not result.matched_count:
        raise ResultOperationConflict(match.token)
    operation.completed = True
    match.recorded_match_id = operation.record.id


async def publish_result_operation(match: Match) -> None:
    operation = match.result_operation
    if not operation or not operation.completed:
        raise ResultOperationConflict(match.token)
    result = await Match.get_pymongo_collection().update_one(
        {"_id": match.id, "result_operation.key": operation.key, "result_operation.completed": True,
         "result_operation.cancelled": {"$ne": True},
         "participants.0.base_store": None, "participants.1.base_store": None,
         "participants.0.opponent_bases_stored": {"$ne": True},
         "participants.1.opponent_bases_stored": {"$ne": True}},
        {"$set": {"result_operation.published": True}},
    )
    if not result.matched_count:
        raise ResultOperationConflict(match.token)
    operation.published = True


async def bind_result_message(match: Match, message_id: int, *, target: Literal["summary", "public"]) -> None:
    operation = match.result_operation
    if not operation or not operation.completed:
        raise ResultOperationConflict(match.token)
    field = "result_operation.summary_message_id" if target == "summary" else "msg"
    previous = operation.summary_message_id if target == "summary" else match.msg
    stored_id = message_id if target == "summary" else str(message_id)
    updates = {field: stored_id}
    if target == "public":
        updates["result_operation.public_message_id"] = message_id
    result = await Match.get_pymongo_collection().update_one(
        {"_id": match.id, "result_operation.key": operation.key, "result_operation.completed": True,
         "result_operation.cancelled": {"$ne": True}, field: previous},
        {"$set": updates},
    )
    if result.matched_count != 1:
        raise ResultOperationConflict(match.token)
    if target == "summary":
        operation.summary_message_id = message_id
    else:
        match.msg = str(message_id)
        operation.public_message_id = message_id

from datetime import datetime, timedelta, timezone

from beanie import PydanticObjectId

from elo_bot.core.finals import ROUND_NODES, FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_draft_models import FinalResultDraft
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.features.finals.lifecycle import ongoing_final
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.results import FinalResultSubmission, submission_snapshot, submit_result


class ResultDraftExpired(FinalError):
    def __init__(self) -> None:
        super().__init__("Confirmation expired. Submit the result again.")


async def create_result_draft(final: EloFinal, request: FinalResultSubmission) -> FinalResultDraft:
    snapshot = submission_snapshot(final, request)
    return await FinalResultDraft(final_id=final.id, guild_id=final.guild_id, owner_id=request.actor_id,
        node_id=request.node_id, expected_version=request.expected_version, contestant_revision=request.contestant_revision,
        attempt=request.attempt, source_match_id=final.nodes[request.node_id].match_id, snapshot=snapshot,
        explicit_winner=request.winner, reason=request.reason,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)).insert()


async def result_draft(draft_id: str, owner: str, guild_id: int) -> FinalResultDraft:
    draft = await FinalResultDraft.find_one({"_id": PydanticObjectId(draft_id),
                                             "expires_at": {"$gt": datetime.now(timezone.utc)}})
    if draft is None:
        raise ResultDraftExpired()
    if draft.owner_id != owner:
        raise FinalError("You cannot interact with another user's components.")
    if draft.guild_id != guild_id:
        raise FinalError("Use this command in the Final's server.")
    return draft


async def finish_result_draft(draft: FinalResultDraft, *, cancel: bool = False) -> tuple[FinalMatchResult | None, bool]:
    async def finish(session):
        collection = FinalResultDraft.get_pymongo_collection()
        row = await collection.find_one({"_id": draft.id, "expires_at": {"$gt": datetime.now(timezone.utc)}},
                                         session=session)
        if row is None:
            raise ResultDraftExpired()
        current = FinalResultDraft.model_validate(row)
        final = await ongoing_final(current.final_id, session=session)
        if current.state == "CANCELLED":
            raise FinalError("Submission cancelled.")
        if cancel:
            if current.state == "CONFIRMED":
                raise FinalError("This result was already saved.")
            await collection.update_one({"_id": current.id}, {"$set": {"state": "CANCELLED"}}, session=session)
            return None, False
        node = final.nodes.get(current.node_id)
        if node is None or node.match_id != current.source_match_id:
            raise FinalError("The match changed. Open its current result form.")
        request = FinalResultSubmission(final_id=current.final_id, node_id=current.node_id,
            actor_id=current.owner_id, operation_key=f"final-draft:{current.id}", expected_version=current.expected_version,
            contestant_revision=current.contestant_revision, attempt=current.attempt, scores=current.snapshot.scores,
            winner=current.explicit_winner, outcome=current.snapshot.outcome, reason=current.reason)
        round_ids = next(ids for ids in ROUND_NODES.values() if current.node_id in ids)
        round_completed = current.round_completed if current.state == "CONFIRMED" else (
            not final.nodes[current.node_id].winner and current.snapshot.winner is not None
            and all(final.nodes[key].winner for key in round_ids if key != current.node_id))
        result = await submit_result(request, session=session)
        await collection.update_one({"_id": current.id}, {"$set": {
            "state": "CONFIRMED", "result_id": result.id, "round_completed": round_completed}},
                                     session=session)
        return result, round_completed
    return await transaction(finish)

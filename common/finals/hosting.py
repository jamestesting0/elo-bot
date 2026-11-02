from common.finals.types import is_final_match
import time

from beanie import PydanticObjectId

from common.finals.bracket import require_round_ready
from common.finals.persistence import transaction
from common.finals.types import FinalError, ROUND_NODES
from database_models import EloFinal, Match
from database_models import Clan
from database_models.token_models import FinalBettingMarket


async def accept_final_start(source: Match, actor_id: str, *, clan_tag: str | None = None,
                             expected_source: dict | None = None) -> None:
    if not is_final_match(source):
        return

    async def accept(session):
        match_row = await Match.get_pymongo_collection().find_one({"_id": source.id, **(expected_source or {})}, session=session)
        final_row = await EloFinal.get_pymongo_collection().find_one(
            {"_id": PydanticObjectId(source.final_id)}, session=session)
        if match_row is None or final_row is None:
            raise FinalError("This Final match is unavailable.")
        final = EloFinal.model_validate(final_row)
        node = final.nodes[source.final_node_id]
        if (match_row.get("final_superseded") or node.match_id != source.id
                or node.contestant_revision != source.final_contestant_revision
                or node.attempt != source.final_attempt
                or node.contestants != [participant.user_id for participant in source.participants]
                or node.result_id is not None or final.state in {"CANCELLING", "CANCELLED", "COMPLETED"}):
            raise FinalError("This match belongs to an earlier pairing or an official result. Use `/final match view`.")
        round_name = next(name for name, nodes in ROUND_NODES.items() if node.id in nodes)
        require_round_ready(final, round_name)
        if final.prediction_state == "OPEN":
            raise FinalError("Predictions are open. A manager must use `/final predictions close` before hosting.")
        claim = f"{source.id}:{node.contestant_revision}:{node.attempt}"
        if node.start_claim == claim:
            return
        if clan_tag is not None:
            clan = await Clan.get_pymongo_collection().find_one({"tag": clan_tag}, session=session)
            if clan is None or clan.get("availability_str") != "Available":
                raise FinalError("The hosting clan is unavailable. Betting remains open until a start is accepted.")
        now = int(time.time())
        node.start_claim = claim
        node.first_actual_started_at = node.first_actual_started_at or now
        node.state = "PLAYING"
        final.first_actual_started_at = final.first_actual_started_at or now
        final.state = "RUNNING"
        final.revision += 1
        await EloFinal.get_pymongo_collection().replace_one({"_id": final.id}, final.model_dump(by_alias=True),
                                                           session=session)
        await FinalBettingMarket.get_pymongo_collection().update_one({
            "final_id": str(final.id), "node_id": node.id, "contestant_revision": node.contestant_revision,
            "state": {"$in": ["OPEN", "CLOSED"]},
        }, {"$set": {"state": "CLOSED", "start_claim": claim},
            "$inc": {"version": 1}}, session=session)
        await FinalBettingMarket.get_pymongo_collection().update_one({"final_id": str(final.id), "node_id": node.id,
            "contestant_revision": node.contestant_revision, "first_actual_started_at": None},
            {"$set": {"first_actual_started_at": now}}, session=session)
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {"$set": {
            "final_start_claim": claim, "final_started_at": now,
        }}, session=session)

    await transaction(accept)

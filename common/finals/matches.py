import time
from secrets import token_hex

from beanie import PydanticObjectId

from common.finals.bracket import require_round_ready
from common.finals.persistence import transaction
from common.finals.types import FINAL_DISTRICTS, FinalError, ROUND_NODES
from database_models import EloFinal, Match, Participant, Player


async def prepare_match(final: EloFinal, node_id: str) -> Match | None:
    async def prepare(session):
        row = await EloFinal.get_pymongo_collection().find_one({"_id": final.id}, session=session)
        if row is None:
            raise FinalError("The Final is unavailable.")
        current = EloFinal.model_validate(row)
        round_name = next((name for name, nodes in ROUND_NODES.items() if node_id in nodes), None)
        if round_name is None:
            raise FinalError("Choose a match from `/final match list`.")
        require_round_ready(current, round_name)
        node = current.nodes[node_id]
        if node.bye:
            return None
        if node.result_id:
            raise FinalError("This pairing already has an official result.")
        if node.match_id:
            existing = await Match.get_pymongo_collection().find_one({"_id": node.match_id}, session=session)
            if existing is None:
                raise FinalError("The earlier match is missing. A manager must resolve this pairing.")
            return Match.model_validate(existing)
        if not all(node.contestants):
            raise FinalError("Both players must be decided before creating their match thread.")
        occupied = await Match.get_pymongo_collection().find_one({
            "participants.user_id": {"$in": node.contestants}, "recorded_match_id": None,
            "final_result_id": None, "is_autowin": {"$ne": True}, "final_superseded": {"$ne": True},
        }, session=session)
        if occupied:
            raise FinalError("An entrant has another unfinished match. Finish it before creating this match thread.")
        for user in node.contestants:
            if await Player.get_pymongo_collection().find_one({"user_id": user}, session=session) is None:
                raise FinalError("An entrant is no longer registered.")
        node.attempt += 1
        match = Match(id=PydanticObjectId(), token=token_hex(8), matched_time=time.time(), format="elo_final_seven",
                      final_id=str(current.id), final_node_id=node_id, final_contestant_revision=node.contestant_revision,
                      final_attempt=node.attempt, routing_channel_id=current.channel_id, routing_guild_id=current.guild_id,
                      base_submission=True, final_districts=list(FINAL_DISTRICTS), participants=[
                          Participant(user_id=user, format="elo_final_seven", districts=list(FINAL_DISTRICTS),
                                      bases=[None] * 7, result=[None] * 7, confirmed=True, submit_bases=True)
                          for user in node.contestants
                      ])
        node.match_id = match.id
        node.provisioning = True
        current.current_round = round_name
        current.revision += 1
        await Match.get_pymongo_collection().insert_one(match.model_dump(by_alias=True), session=session)
        await EloFinal.get_pymongo_collection().replace_one({"_id": current.id}, current.model_dump(by_alias=True),
                                                           session=session)
        return match

    return await transaction(prepare)


async def bind_thread(final: EloFinal, match: Match, thread_id: str) -> None:
    field = f"nodes.{match.final_node_id}"
    bound = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, f"{field}.match_id": match.id,
        f"{field}.contestant_revision": match.final_contestant_revision, f"{field}.attempt": match.final_attempt,
    }, {"$set": {f"{field}.thread_id": thread_id, f"{field}.provisioning": False,
                 f"{field}.provisioning_error": None}, "$inc": {"revision": 1}})
    if not bound.matched_count:
        raise FinalError("The thread exists, but its pairing changed. A manager must recover the binding.")

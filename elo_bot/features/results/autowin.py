from elo_bot.core.finals import is_final_match
from pydantic import JsonValue

from elo_bot.db import Match, Player
from elo_bot.core.finals import FinalError


async def claim_autowin(match: Match, winner_id: str, actor_id: str, reason: str) -> Match:
    if is_final_match(match):
        raise FinalError("Use `/final match forfeit` with zero scores and an explicit winner.")
    if match.autowin_winner_id is None:
        await Match.get_pymongo_collection().update_one(
            {"_id": match.id, "is_autowin": False, "autowin_winner_id": None,
             "$or": [{"result_operation": None}, {"result_operation.published": True}]},
            {"$set": {"autowin_winner_id": winner_id, "autowin_actor_id": actor_id, "autowin_reason": reason}},
        )
    current = await Match.find_one({"_id": match.id})
    if current is None:
        raise LookupError(match.token)
    return current


async def apply_autowin_player(user_id: str, match: Match, delta: int) -> int:
    if is_final_match(match):
        raise FinalError("Final forfeits do not apply ranked Elo penalties.")
    receipt = str(match.id)
    while True:
        player = await Player.find_one({"user_id": user_id})
        if player is None:
            raise LookupError(user_id)
        recorded_elo = player.autowin_receipts.get(receipt)
        if recorded_elo is not None:
            return recorded_elo
        new_elo = player.elo + delta
        update: dict[str, dict[str, JsonValue]] = {
            "$inc": {"elo": delta}, "$set": {f"autowin_receipts.{receipt}": new_elo},
        }
        if delta > 0:
            update["$set"]["priority"] = True
            update["$max"] = {"highest_elo": new_elo}
        result = await Player.get_pymongo_collection().update_one(
            {"user_id": user_id, "elo": player.elo, f"autowin_receipts.{receipt}": {"$exists": False}}, update,
        )
        if result.matched_count:
            return new_elo


async def mark_autowin(match: Match, *, completed: bool = False) -> None:
    values = {"is_autowin": True}
    if completed:
        values["autowin_completed"] = True
    result = await Match.get_pymongo_collection().update_one(
        {"_id": match.id, "autowin_winner_id": match.autowin_winner_id}, {"$set": values},
    )
    if not result.matched_count:
        raise LookupError(match.token)
    match.is_autowin = True
    if completed:
        match.autowin_completed = True

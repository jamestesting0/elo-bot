from elo_bot.db import BaseLayout, Player, RecordedMatch


async def add_recorded_match(player: Player, recorded_match: RecordedMatch) -> int:
    player_id = player.user_id
    reference = recorded_match.to_ref()
    stats = recorded_match.players[player_id]
    while True:
        new_elo = player.elo + stats.elo_change
        result = await Player.get_pymongo_collection().update_one(
            {"user_id": player_id, "elo": player.elo, "matches": {"$ne": reference}},
            {
                "$inc": {"elo": stats.elo_change, "wins": int(stats.won)},
                "$addToSet": {"matches": reference},
                "$max": {"highest_elo": new_elo},
            },
        )
        if result.matched_count:
            return new_elo
        player = await Player.find_one(Player.user_id == player_id)
        if not player:
            raise LookupError(player_id)
        if any(match.to_ref() == reference for match in player.matches):
            return player.elo


async def remove_base_record(
        base: BaseLayout,
        record: list[str | int | float],
        *,
        receipt: str | None = None,
) -> None:
    while base:
        if receipt and receipt in base.operation_receipts:
            return
        index = next((i for i in range(len(base.records) - 1, -1, -1) if base.records[i] == record), None)
        if index is None and not receipt:
            return
        query = {"_id": base.id, "records": base.records}
        updates = {}
        if index is not None:
            updates["$set"] = {"records": base.records[:index] + base.records[index + 1:]}
        if receipt:
            query["operation_receipts"] = {"$ne": receipt}
            updates["$addToSet"] = {"operation_receipts": receipt}
        result = await BaseLayout.get_pymongo_collection().update_one(
            query, updates,
        )
        if result.matched_count:
            return
        base = await BaseLayout.find_one(BaseLayout.id == base.id)

import time

from pymongo.errors import DuplicateKeyError
from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.features.finals.bracket import automatic_pairs, build_bracket
from elo_bot.core.finals import FinalError
from elo_bot.features.finals.persistence import transaction
from elo_bot.db import EloFinal, Player, Season
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.db.match_models import Match
from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from elo_bot.features.finals.profiles import rebuild_profiles


async def resolve_final(season: int | None = None) -> EloFinal:
    if season is not None:
        final = await EloFinal.find_one({"season_number": season})
    else:
        final = await EloFinal.find_one({"active": True})
        if final is None:
            final = await EloFinal.find_all().sort("-season_number").first_or_none()
    if final is None:
        raise FinalError("No Final exists for this scope. Managers can use `/final start`.")
    if final.schema_version != 2 or final.season_id is None:
        raise FinalError("This legacy Final needs an explicit archived-season mapping before use.")
    return final


async def start_final(season: int | None, guild_id: int, channel_id: int) -> EloFinal:
    try:
        async with PLAYER_DATA_BOUNDARY.write(PLAYER_DATA_BOUNDARY.generation):
            return await _start_final(season, guild_id, channel_id)
    except PlayerDataReset as error:
        raise FinalError("Player data is being reset. Start the Final after it finishes.") from error


async def _start_final(season: int | None, guild_id: int, channel_id: int) -> EloFinal:
    query = {"end": {"$lte": int(time.time())}}
    if season is not None:
        query["season_number"] = season
    archive = await Season.find(query).sort("-season_number").first_or_none()
    if archive is None:
        raise FinalError("Choose an existing completed season.")
    if await Season.find({"season_number": archive.season_number}).count() != 1:
        raise FinalError("This season is ambiguous. Resolve its archive mapping before starting a Final.")
    final = EloFinal(season_number=archive.season_number, season_id=archive.id, schema_version=2,
                     guild_id=guild_id, channel_id=channel_id, active=True)
    try:
        await final.insert()
    except DuplicateKeyError as error:
        raise FinalError("An ongoing Final or an edition for this season already exists.") from error
    return final


async def update_setup(final: EloFinal, changes: dict, *, roster_change: bool = False,
                       session: AsyncClientSession | None = None) -> EloFinal:
    increments = {"revision": 1}
    if roster_change:
        increments["setup_revision"] = 1
    update = {"$set": changes, "$inc": increments}
    saved = await EloFinal.get_pymongo_collection().update_one(
        {"_id": final.id, "revision": final.revision, "state": "REGISTRATION", "active": True}, update,
        session=session)
    if not saved.matched_count:
        raise FinalError("The setup changed or is frozen. Refresh and try again.")
    saved = await EloFinal.find_one({"_id": final.id}, session=session)
    if saved is None:
        raise FinalError("The Final was removed. Refresh before continuing.")
    return saved


async def add_entrant(final: EloFinal, user_id: str, nickname: str) -> EloFinal:
    player = await Player.find_one({"user_id": user_id})
    if player is None:
        raise FinalError("This player is not registered.")
    archive = await Season.get(final.season_id)
    if archive is None:
        raise FinalError("The captured season archive is missing.")
    elo = next((entry.elo for entry in archive.players if entry.player_id in {user_id, player.id}), None)
    saved = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "revision": final.revision, "state": "REGISTRATION", "active": True,
        "entrant_ids": {"$ne": user_id}, "$expr": {"$lt": [{"$size": "$entrant_ids"}, 8]},
    }, {"$push": {"entrant_ids": user_id}, "$set": {
        f"player_nicknames.{user_id}": nickname.strip()[:80] or user_id, f"archived_elo.{user_id}": elo,
    }, "$inc": {"revision": 1, "setup_revision": 1}})
    if not saved.matched_count:
        raise FinalError("The Final is full, this player already joined, or the setup changed. Refresh first.")
    return await resolve_final(final.season_number)


async def remove_entrant(final: EloFinal, user_id: str) -> None:
    if user_id not in final.entrant_ids:
        raise FinalError("This player is not an entrant.")
    result = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "revision": final.revision, "state": "REGISTRATION", "active": True,
        "entrant_ids": user_id,
    }, {"$unset": {f"player_nicknames.{user_id}": "", f"seeds.{user_id}": "", f"archived_elo.{user_id}": ""},
        "$pull": {"entrant_ids": user_id, "byes": user_id}, "$inc": {"revision": 1, "setup_revision": 1}})
    if result.matched_count != 1:
        raise FinalError("The roster changed or setup is frozen. Refresh the Final.")


async def set_seed(final: EloFinal, user_id: str, position: int) -> EloFinal:
    if user_id not in final.entrant_ids or not 1 <= position <= 8 or any(
        seed == position and user != user_id for user, seed in final.seeds.items()
    ):
        raise FinalError("Choose an entrant and an unused seed from 1 to 8.")
    return await update_setup(final, {"seeds": {**final.seeds, user_id: position}}, roster_change=True)


async def set_bye(final: EloFinal, user_id: str, enabled: bool) -> EloFinal:
    if user_id not in final.entrant_ids:
        raise FinalError("Choose a registered entrant.")
    byes = [user for user in final.byes if user != user_id]
    if enabled:
        byes.append(user_id)
    return await update_setup(final, {"byes": byes}, roster_change=True)


async def save_pairing_draft(final: EloFinal, owner: str, pairs: dict[str, list[str | None]]) -> EloFinal:
    used = [user for pair in pairs.values() for user in pair if user is not None]
    if len(used) != len(set(used)) or not set(used) <= set(final.entrant_ids):
        raise FinalError("Draft opponents must be distinct unused entrants.")
    if final.draft_owner not in {None, owner}:
        raise FinalError("Another manager owns this pairing draft.")
    return await update_setup(final, {
        "manual_draft": pairs, "draft_owner": owner, "draft_revision": final.draft_revision + 1,
        "draft_setup_revision": final.setup_revision,
    })


async def confirm_bracket(final: EloFinal, owner: str, *, manual: bool = False) -> EloFinal:
    if manual and (final.draft_owner != owner or final.draft_setup_revision != final.setup_revision):
        raise FinalError("This draft belongs to another manager or an earlier roster. Rebuild it first.")
    pairs = final.manual_draft if manual else automatic_pairs(final)
    nodes = build_bracket(final, pairs)
    async def confirm(session):
        saved = await update_setup(final, {
            "nodes": {key: node.model_dump() for key, node in nodes.items()}, "state": "BRACKET_READY",
            "setup_revision": final.setup_revision + 1, "publication_pending": True,
        }, session=session)
        await rebuild_profiles(saved, session)
        return saved
    return await transaction(confirm)


async def reset_bracket(final: EloFinal) -> None:
    async def reset(session):
        collection = EloFinal.get_pymongo_collection()
        current = EloFinal.model_validate(await collection.find_one({"_id": final.id}, session=session))
        if (not current.active or current.state != "BRACKET_READY" or current.predictions_ever_opened
                or await Match.get_pymongo_collection().find_one({"final_id": str(final.id)}, session=session)
                or await FinalBettingMarket.get_pymongo_collection().find_one({"final_id": str(final.id)}, session=session)):
            raise FinalError("Reset is available only before predictions, markets, and match preparation. Use result correction later.")
        await collection.update_one({"_id": final.id}, {"$set": {
            "nodes": {}, "state": "REGISTRATION", "manual_draft": {}, "draft_owner": None,
            "publication_pending": True}, "$inc": {"revision": 1, "setup_revision": 1}}, session=session)
        current.nodes = {}
        await rebuild_profiles(current, session)
    await transaction(reset)


async def request_cancellation(final: EloFinal, reason: str) -> None:
    if not reason.strip():
        raise FinalError("Enter a cancellation reason.")
    async def cancel(session):
        collection = EloFinal.get_pymongo_collection()
        current = EloFinal.model_validate(await collection.find_one({"_id": final.id}, session=session))
        if current.state in {"CANCELLING", "CANCELLED"}:
            return
        if not current.active or current.repair_pending or await FinalBettingMarket.get_pymongo_collection().find_one({
            "final_id": str(final.id), "state": {"$in": ["SETTLING", "CORRECTING"]}}, session=session):
            raise FinalError("Finish pending result and payout recovery before cancelling the ongoing Final.")
        await collection.update_one({"_id": final.id}, {"$set": {"state": "CANCELLING", "prediction_state": "CLOSED",
            "cancel_reason": reason.strip(), "repair_pending": True, "publication_pending": True},
            "$inc": {"revision": 1, "result_revision": 1}}, session=session)
        await FinalBettingMarket.get_pymongo_collection().update_many({"final_id": str(final.id), "state": "OPEN"},
            {"$set": {"state": "CLOSED"}, "$inc": {"version": 1}}, session=session)
    await transaction(cancel)


async def end_final(final: EloFinal) -> None:
    async def end(session):
        collection = EloFinal.get_pymongo_collection()
        current = EloFinal.model_validate(await collection.find_one({"_id": final.id}, session=session))
        if current.state == "COMPLETED":
            return
        if (not current.active or not current.champion or current.repair_pending
                or any(node.state not in {"DECIDED", "BYE"} for node in current.nodes.values())
                or current.predictions_scoring_revision != current.result_revision
                or any(not current.result_progress[node.result_id.replace(".", "_")].completed
                       for node in current.nodes.values() if node.result_id)
                or await FinalBettingMarket.get_pymongo_collection().find_one({"final_id": str(final.id), "$or": [
                    {"escrow": {"$ne": 0}}, {"state": {"$nin": ["PAID", "REFUNDED"]}}]}, session=session)):
            raise FinalError("Resolve every pairing and finish result, prediction, and payout recovery before ending the Final.")
        archive = await Season.get_pymongo_collection().find_one({"_id": current.season_id}, session=session)
        if archive is None or archive["season_number"] != current.season_number:
            raise FinalError("The captured season archive is unavailable. Recover its mapping first.")
        await collection.update_one({"_id": current.id}, {"$set": {"state": "COMPLETED", "active": False,
            "completed_at": int(time.time()), "publication_pending": True}, "$inc": {"revision": 1}}, session=session)
        await Season.get_pymongo_collection().update_one({"_id": current.season_id}, {"$set": {"final_summary": {
            "final_id": str(current.id), "champion": current.champion, "revision": current.result_revision}}}, session=session)
    await transaction(end)

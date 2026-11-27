import time

from pymongo.errors import DuplicateKeyError
from pymongo.asynchronous.client_session import AsyncClientSession
from beanie import PydanticObjectId
from interactions import Member

from elo_bot.features.finals.bracket import (
    automatic_pairs, build_bracket, lineup_confirmed, require_lineup_confirmation, seeded_players,
)
from elo_bot.core.finals import FINAL_PICK_COUNT, FINAL_PICK_WINDOW, FINAL_SEATS, FINAL_THREAD_RETENTION, FinalError
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.invitation_store import withdraw_pending
from elo_bot.db import EloFinal, Player
from elo_bot.db.season_models import FinalOpponentPicks, FinalPickTurn
from elo_bot.db.token_models import FinalBettingMarket, ParticipantStake
from elo_bot.db.match_models import Match
from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from elo_bot.features.finals.profiles import rebuild_profiles
from elo_bot.features.seasons.ongoing import ongoing_season_number, season_final
from elo_bot.features.tokens.awards import freeze_prediction_awards
from elo_bot.features.tokens.stakes import reconcile_stake


async def ongoing_final(final_id: PydanticObjectId | str | None = None, *,
                        session: AsyncClientSession | None = None) -> EloFinal:
    """Resolve the sole ongoing edition; an old control cannot select another one."""
    final = await EloFinal.find_one({"active": True}, session=session)
    if final_id is not None and (final is None or str(final.id) != str(final_id)):
        raise FinalError("This view is outdated. Run `/final dashboard` again.")
    if final is None:
        raise FinalError("No Final is running. Start one with `/final start`.")
    return final


async def final_for_view(season: int | None = None) -> EloFinal:
    # A season's Final is its latest edition: a restart after cancellation replaces the cancelled one.
    if season is not None:
        final = await EloFinal.find({"season_number": season}).sort("-_id").first_or_none()
    else:
        final = await EloFinal.find_one({"active": True})
        if final is None:
            final = await EloFinal.find_all().sort([("season_number", -1), ("_id", -1)]).first_or_none()
    if final is None:
        raise FinalError("No Final exists for this scope. Managers can use `/final start`.")
    if final.schema_version != 2:
        raise FinalError("This older Final is unavailable.")
    return final


async def reload_final(final: EloFinal) -> EloFinal:
    current = await EloFinal.get(final.id)
    if current is None:
        raise FinalError("This Final no longer exists.")
    return current


async def start_final(guild_id: int, channel_id: int, *, opponent_picks: bool = True) -> EloFinal:
    try:
        async with PLAYER_DATA_BOUNDARY.write(PLAYER_DATA_BOUNDARY.generation):
            return await _start_final(guild_id, channel_id, opponent_picks=opponent_picks)
    except PlayerDataReset as error:
        raise FinalError("Player data is being reset. Start the Final after it finishes.") from error


async def _start_final(guild_id: int, channel_id: int, *, opponent_picks: bool) -> EloFinal:
    season = await ongoing_season_number()
    if await season_final(season):
        raise FinalError(f"Season {season} already has a Final. Cancel it to start again.")
    if running := await EloFinal.find_one({"active": True}):
        raise FinalError(f"The Season {running.season_number} Final is still running. Finish or cancel it first.")
    final = EloFinal(season_number=season, schema_version=2, guild_id=guild_id, channel_id=channel_id, active=True,
                     opponent_picks=FinalOpponentPicks(confirmed=False) if opponent_picks else None)
    try:
        await final.insert()
    except DuplicateKeyError as error:
        raise FinalError("Another Final was started at the same time. Refresh and try again.") from error
    return final


async def update_setup(final: EloFinal, changes: dict, *,
                       session: AsyncClientSession | None = None) -> EloFinal:
    await ongoing_final(final.id, session=session)
    update = {"$set": changes, "$inc": {"revision": 1, "setup_revision": 1}}
    saved = await EloFinal.get_pymongo_collection().update_one(
        {"_id": final.id, "setup_revision": final.setup_revision, "state": "REGISTRATION", "active": True}, update,
        session=session)
    if not saved.matched_count:
        raise FinalError("The setup changed or is frozen. Refresh and try again.")
    saved = await EloFinal.find_one({"_id": final.id}, session=session)
    if saved is None:
        raise FinalError("The Final was removed. Refresh before continuing.")
    return saved


async def add_entrant(final: EloFinal, user_id: str, nickname: str, *, override: bool = False,
                      session: AsyncClientSession | None = None) -> EloFinal:
    await ongoing_final(final.id, session=session)
    player = await Player.find_one({"user_id": user_id}, session=session)
    if player is None:
        raise FinalError("This player is not registered.")
    fields = {f"player_nicknames.{user_id}": nickname.strip()[:80] or user_id, f"seed_elo.{user_id}": player.elo}
    if final.opponent_picks and final.opponent_picks.seeds:
        fields["opponent_picks"] = FinalOpponentPicks(confirmed=False).model_dump()
    saved = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "setup_revision": final.setup_revision, "state": "REGISTRATION", "active": True,
        "entrant_ids": {"$ne": user_id}, "$expr": {"$lt": [{"$size": "$entrant_ids"}, FINAL_SEATS]},
    }, {"$push": {"entrant_ids": user_id, **({"nickname_overrides": user_id} if override else {})},
        "$set": fields, "$inc": {"revision": 1, "setup_revision": 1}}, session=session)
    if not saved.matched_count:
        raise FinalError("The Final is full, this player already joined, or the setup changed. Refresh first.")
    await withdraw_pending(final.id, session, user_id=user_id)
    return await ongoing_final(final.id, session=session)


async def refresh_display_names(final: EloFinal, members: dict[str, Member | None]) -> EloFinal:
    """Follow current Discord display names for entrants without a manager-set nickname."""
    changed = False
    for user, member in members.items():
        name = member.display_name.strip()[:80] if member else ""
        if not name or user in final.nickname_overrides or final.player_nicknames.get(user, name) == name:
            continue
        saved = await EloFinal.get_pymongo_collection().update_one(
            {"_id": final.id, "nickname_overrides": {"$ne": user}, f"player_nicknames.{user}": {"$exists": True}},
            {"$set": {f"player_nicknames.{user}": name}})
        changed |= bool(saved.modified_count)
    return await reload_final(final) if changed else final


async def remove_entrant(final: EloFinal, user_id: str) -> None:
    await ongoing_final(final.id)
    if user_id not in final.entrant_ids:
        raise FinalError("This player is not an entrant.")
    update = {"$unset": {f"player_nicknames.{user_id}": "", f"seed_elo.{user_id}": ""},
              "$pull": {"entrant_ids": user_id, "nickname_overrides": user_id},
              "$inc": {"revision": 1, "setup_revision": 1}}
    if final.opponent_picks is not None:
        update["$set"] = {"opponent_picks": FinalOpponentPicks(confirmed=False).model_dump()}
    result = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "setup_revision": final.setup_revision, "state": "REGISTRATION", "active": True,
        "entrant_ids": user_id,
    }, update)
    if result.matched_count != 1:
        raise FinalError("The roster changed or setup is frozen. Refresh the Final.")


async def set_nickname(final: EloFinal, user_id: str, nickname: str) -> None:
    async def rename(session):
        current = await ongoing_final(final.id, session=session)
        if current.state in {"CANCELLING", "CANCELLED"}:
            raise FinalError("This Final was cancelled.")
        field = f"player_nicknames.{user_id}"
        changes = {field: nickname.strip()[:80]}
        if current.nodes or current.bracket_send_attempted:
            changes["publication_pending"] = True
        result = await EloFinal.get_pymongo_collection().update_one(
            {"_id": current.id, "active": True, field: {"$exists": True}},
            {"$set": changes, "$addToSet": {"nickname_overrides": user_id}, "$inc": {"revision": 1}},
            session=session,
        )
        if result.matched_count != 1:
            raise FinalError("This player is not an entrant.")
    await transaction(rename)


async def confirm_lineup(final: EloFinal) -> EloFinal:
    if final.opponent_picks is None or lineup_confirmed(final) or not 5 <= len(final.entrant_ids) <= FINAL_SEATS:
        raise FinalError("The lineup is no longer ready to confirm. Refresh the dashboard.")
    turns = []
    if len(final.entrant_ids) == FINAL_SEATS:
        turns = [FinalPickTurn(deadline=int(time.time()) + FINAL_PICK_WINDOW),
                 *(FinalPickTurn() for _ in range(FINAL_PICK_COUNT - 1))]
    picks = FinalOpponentPicks(generation=final.setup_revision + 1, seeds=seeded_players(final),
                              turns=turns, confirmed=True)
    return await update_setup(final, {"opponent_picks": picks.model_dump()})


def current_pairing_draft_owner(final: EloFinal) -> str | None:
    return final.draft_owner if final.draft_setup_revision == final.setup_revision else None


async def save_pairing_draft(final: EloFinal, owner: str, pairs: dict[str, list[str | None]]) -> EloFinal:
    await ongoing_final(final.id)
    require_lineup_confirmation(final)
    used = [user for pair in pairs.values() for user in pair if user is not None]
    if len(used) != len(set(used)) or not set(used) <= set(final.entrant_ids):
        raise FinalError("Draft opponents must be distinct unused entrants.")
    if current_pairing_draft_owner(final) not in {None, owner}:
        raise FinalError("Another manager owns this pairing draft.")
    return await update_setup(final, {
        "manual_draft": pairs, "draft_owner": owner, "draft_revision": final.draft_revision + 1,
        "draft_setup_revision": final.setup_revision + 1,
    })


async def confirm_bracket(final: EloFinal, owner: str, *, manual: bool = False) -> EloFinal:
    await ongoing_final(final.id)
    require_lineup_confirmation(final)
    if manual and (final.draft_owner != owner or final.draft_setup_revision != final.setup_revision):
        raise FinalError("This draft belongs to another manager or an earlier roster. Rebuild it first.")
    pairs = final.manual_draft if manual else automatic_pairs(final)
    nodes = build_bracket(final, pairs)
    async def confirm(session):
        changes = {
            "nodes": {key: node.model_dump() for key, node in nodes.items()}, "state": "BRACKET_READY",
            "publication_pending": True,
        }
        if final.opponent_picks is not None:
            changes["opponent_picks.closed"] = True
        saved = await update_setup(final, changes, session=session)
        await withdraw_pending(final.id, session)
        await rebuild_profiles(saved, session)
        return saved
    return await transaction(confirm)


async def reset_bracket(final: EloFinal) -> None:
    async def reset(session):
        collection = EloFinal.get_pymongo_collection()
        current = await ongoing_final(final.id, session=session)
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
        current = await ongoing_final(final.id, session=session)
        if current.state == "CANCELLING":
            return
        if not current.active or current.repair_pending or await FinalBettingMarket.get_pymongo_collection().find_one({
            "final_id": str(final.id), "state": {"$in": ["SETTLING", "CORRECTING"]}}, session=session):
            raise FinalError("Finish pending result and payout recovery before cancelling the ongoing Final.")
        await collection.update_one({"_id": final.id}, {"$set": {"state": "CANCELLING", "prediction_state": "CLOSED",
            "cancel_reason": reason.strip(), "repair_pending": True, "publication_pending": True,
            "thread_cleanup_at": int(time.time()) + FINAL_THREAD_RETENTION},
            "$inc": {"revision": 1, "result_revision": 1}}, session=session)
        await FinalBettingMarket.get_pymongo_collection().update_many({"final_id": str(final.id), "state": "OPEN"},
            {"$set": {"state": "CLOSED"}, "$inc": {"version": 1}}, session=session)
        await Match.get_pymongo_collection().update_many({
            "final_id": str(final.id), "final_result_id": None,
        }, {"$set": {"final_superseded": True}}, session=session)
        async for stake in ParticipantStake.get_pymongo_collection().find({
                "final_id": str(final.id), "state": {"$in": ["offer", "locked"]}}, session=session):
            await reconcile_stake(stake["_id"], session=session)
        await withdraw_pending(final.id, session)
    await transaction(cancel)


async def end_final(final: EloFinal) -> None:
    async def end(session):
        collection = EloFinal.get_pymongo_collection()
        current = await ongoing_final(final.id, session=session)
        if (not current.active or not current.champion or current.repair_pending
                or any(node.state not in {"DECIDED", "BYE"} for node in current.nodes.values())
                or current.predictions_scoring_revision != current.result_revision
                or any(not current.result_progress[node.result_id.replace(".", "_")].completed
                       for node in current.nodes.values() if node.result_id)
                or await FinalBettingMarket.get_pymongo_collection().find_one({"final_id": str(final.id), "$or": [
                    {"escrow": {"$ne": 0}}, {"state": {"$nin": ["PAID", "REFUNDED"]}}]}, session=session)
                or await ParticipantStake.get_pymongo_collection().find_one({
                    "final_id": str(final.id), "state": {"$in": ["offer", "locked"]}}, session=session)):
            raise FinalError("Resolve every pairing and finish result, prediction, and payout recovery before ending the Final.")
        now = int(time.time())
        await freeze_prediction_awards(current, session)
        await collection.update_one({"_id": current.id}, {"$set": {"state": "COMPLETED", "active": False,
            "completed_at": now, "thread_cleanup_at": now + FINAL_THREAD_RETENTION, "publication_pending": True,
            "prediction_state": "CLOSED"},
            "$inc": {"revision": 1}}, session=session)
        await withdraw_pending(final.id, session)
    await transaction(end)

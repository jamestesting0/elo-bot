from copy import deepcopy
import time
from secrets import token_hex

from beanie import PydanticObjectId
import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.discord.channel_edits import schedule_channel_name_edit
from elo_bot.features.finals.bracket import require_round_ready
from elo_bot.features.finals.labels import final_member_names, thread_label
from elo_bot.features.finals.persistence import transaction
from elo_bot.core.finals import FINAL_DISTRICTS, FinalError, ROUND_NODES
from elo_bot.db import EloFinal, Match, Participant, Player
from elo_bot.db.season_models import EloFinalMatch
from elo_bot.features.matches.messages import MatchMessagePublicationError, MatchMessageState, publish_match_message
from elo_bot.features.matches.message_lock import match_message_lock
from elo_bot.features.matches.timing import final_hosting_format_is_set, hosting_started
from elo_bot.features.finals.lifecycle import ongoing_final


def require_current_match(final: EloFinal, source: Match) -> EloFinalMatch:
    if final.state in {"CANCELLING", "CANCELLED"}:
        raise FinalError("This Final was cancelled.")
    node = final.nodes.get(source.final_node_id)
    if (node is None or source.final_superseded or node.match_id != source.id
            or str(final.id) != source.final_id or node.contestant_revision != source.final_contestant_revision
            or node.attempt != source.final_attempt
            or node.contestants != [participant.user_id for participant in source.participants]):
        raise FinalError("This match was replaced. Use `/final bracket view`.")
    return node


async def require_ongoing_match(source: Match) -> EloFinal:
    """Reject controls from cancelled, archived, or replaced physical matches."""
    final = await EloFinal.get(PydanticObjectId(source.final_id))
    if final is None:
        raise FinalError("This Final match is unavailable.")
    require_current_match(final, source)
    await ongoing_final(final.id)
    return final


async def refresh_match_names(ctx: ipy.InteractionContext, final: EloFinal, user_id: str) -> list[str]:
    failed = []
    for node in final.nodes.values():
        if not node.thread_id or not node.match_id or node.winner or user_id not in node.contestants:
            continue
        try:
            async with match_message_lock(node.thread_id):
                source = await Match.get(node.match_id)
                if source is None:
                    continue
                current = await require_ongoing_match(source)
                if current.nodes[node.id].thread_id != node.thread_id:
                    continue
                players = [await ctx.bot.fetch_user(int(player.user_id)) for player in source.participants]
                thread = await ctx.bot.fetch_channel(int(node.thread_id))
                if not isinstance(thread, ipy.ThreadChannel) or any(player is None for player in players):
                    failed.append(node.id)
                    continue
                status = thread.name.partition(" ")[0]
                label = thread_label(current, node.id, players)
                if thread.name != f"{status} {label}":
                    await schedule_channel_name_edit(thread, status, label)
                if source.msg:
                    channel = await ctx.bot.fetch_channel(source.routing_channel_id)
                    if not isinstance(channel, (ipy.GuildText, ipy.GuildNews)):
                        failed.append(node.id)
                        continue
                    message = await channel.fetch_message(int(source.msg))
                    if not message.embeds:
                        continue
                    embed = deepcopy(message.embeds[0])
                    field = next((field for field in embed.fields if field.name == "**Players**"), None)
                    if field is None:
                        continue
                    user_ids = [player.user_id for player in source.participants]
                    names = await final_member_names(current, channel.guild, user_ids)
                    matchup = " vs. ".join(names[user] for user in user_ids)
                    if field.value != matchup:
                        field.value = matchup
                        await publish_match_message(ctx, source, MatchMessageState.capture(source, 0), embed=embed,
                                                    existing_message=message, public_channel=channel)
        except (ipy.errors.HTTPException, PyMongoError, FinalError, MatchMessagePublicationError):
            failed.append(node.id)
    return failed


async def schedule_final_match(source: Match, timestamp: int, *, player: int | None = None) -> Match:
    """Commit both the physical schedule and its surviving pairing copy together."""

    expected = dict(MatchMessageState.capture(source, player or 0).binding_filter())

    async def schedule(session):
        match_row = await Match.get_pymongo_collection().find_one(expected, session=session)
        final_row = await EloFinal.get_pymongo_collection().find_one(
            {"_id": PydanticObjectId(source.final_id)}, session=session)
        if match_row is None or final_row is None:
            raise FinalError("This Final match changed. Refresh before scheduling it.")
        current = Match.model_validate(match_row)
        final = EloFinal.model_validate(final_row)
        node = require_current_match(final, current)
        await ongoing_final(final.id, session=session)
        require_round_ready(final, next(name for name, nodes in ROUND_NODES.items() if node.id in nodes))
        if node.result_id or final.state in {"CANCELLING", "CANCELLED", "COMPLETED"}:
            raise FinalError("This pairing already has an official result or is closed.")
        sides = (0, 1) if player is None else (player,)
        if (hosting_started(current, player)
                or any(current.final_started_times[side] is not None and current.times[side] is not None
                       for side in sides)
                or (current.is_war != (player is not None) and final_hosting_format_is_set(current))):
            raise FinalError("Hosting has already started or the schedule uses another match format.")
        current.is_war = player is not None
        node.scheduled_is_war = current.is_war
        current.final_scouting_paused_sides = [side for side in current.final_scouting_paused_sides if side not in sides]
        for side in sides:
            current.scheduled_times[side] = timestamp
            current.times[side] = timestamp
            node.scheduled_times[side] = timestamp
            if player is not None:
                current.hosts[side] = None
        final.revision += 1
        await Match.get_pymongo_collection().update_one({"_id": current.id}, {"$set": {
            "is_war": current.is_war, "times": current.times, "scheduled_times": current.scheduled_times,
            "hosts": current.hosts, "final_scouting_paused_sides": current.final_scouting_paused_sides,
        }}, session=session)
        await EloFinal.get_pymongo_collection().replace_one({"_id": final.id}, final.model_dump(by_alias=True),
                                                           session=session)
        return current

    return await transaction(schedule)


async def prepare_match(final: EloFinal, node_id: str) -> Match | None:
    async def prepare(session):
        current = await ongoing_final(final.id, session=session)
        round_name = next((name for name, nodes in ROUND_NODES.items() if node_id in nodes), None)
        if round_name is None:
            raise FinalError("Choose a match from `/final bracket view`.")
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
        node.actual_started_at = None
        match = Match(id=PydanticObjectId(), token=token_hex(8), matched_time=time.time(), format="elo_final_seven",
                      final_id=str(current.id), final_node_id=node_id, final_contestant_revision=node.contestant_revision,
                      final_attempt=node.attempt, routing_channel_id=current.channel_id, routing_guild_id=current.guild_id,
                      base_submission=True, final_districts=list(FINAL_DISTRICTS), participants=[
                          Participant(user_id=user, format="elo_final_seven", districts=list(FINAL_DISTRICTS),
                                      bases=[None] * 7, result=[None] * 7, confirmed=True, submit_bases=True)
                          for user in node.contestants
                      ])
        match.scheduled_times = list(node.scheduled_times)
        match.times = list(node.scheduled_times)
        if node.scheduled_is_war is not None:
            match.is_war = node.scheduled_is_war
        node.match_id = match.id
        node.provisioning = True
        current.revision += 1
        await Match.get_pymongo_collection().insert_one(match.model_dump(by_alias=True), session=session)
        await EloFinal.get_pymongo_collection().replace_one({"_id": current.id}, current.model_dump(by_alias=True),
                                                           session=session)
        return match

    return await transaction(prepare)


async def bind_thread(final: EloFinal, match: Match, thread_id: str) -> None:
    await ongoing_final(final.id)
    field = f"nodes.{match.final_node_id}"
    bound = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "active": True, "state": {"$nin": ["CANCELLING", "CANCELLED"]},
        f"{field}.match_id": match.id,
        f"{field}.contestant_revision": match.final_contestant_revision, f"{field}.attempt": match.final_attempt,
    }, {"$set": {f"{field}.thread_id": thread_id, f"{field}.provisioning": False,
                 f"{field}.provisioning_error": None}, "$inc": {"revision": 1}})
    if not bound.matched_count:
        raise FinalError("The match thread was created, but the pairing changed. Ask a manager to run `/final recover`.")

import random
from typing import Protocol

import interactions as ipy

import elobot.features.hosting.live.eligibility as live_eligibility
from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity
from cogs.match_commands import (
    LivePreflight,
    format_available_resource_clans,
    is_match_finished,
    is_resource_clan_locked,
)
from elobot.features.base_store.live import live_stores, ready_live_reservation_id
from elobot.config.emojis import ERROR_EMOJI
from elobot.features.matches.timing import hosting_started
from elobot.db import Clan, Match, Player


class SelfHostingRuntime(Protocol):
    bot: ipy.Client
    live_requests: LiveRequestCoordinator


async def order_self_host_clans(
        bot: ipy.Client,
        match: Match,
        player: int,
        available_clans: list[Clan],
) -> list[Clan]:
    random.shuffle(available_clans)
    participant = match.participants[player]
    player_doc = await Player.find_one(Player.user_id == participant.user_id)
    player_account = await bot.coc_client.get_player(player_doc.tag)
    player_clan_tag = player_account.clan.tag if player_account.clan else None
    if player_clan_tag:
        available_clans.sort(key=lambda clan: clan.tag != player_clan_tag)

    opponent = 1 - player
    opponent_finished = is_match_finished(match, player=opponent)
    if opponent_finished:
        opponent_clan_tag = match.host_clans[opponent]
    else:
        opponent_doc = await Player.find_one(Player.user_id == match.participants[opponent].user_id)
        opponent_account = await bot.coc_client.get_player(opponent_doc.tag)
        opponent_clan_tag = opponent_account.clan.tag if opponent_account.clan else None
    if opponent_clan_tag and not opponent_finished:
        available_clans.sort(key=lambda clan: clan.tag == opponent_clan_tag)

    persistent_clan_tag = match.persistent_host_clans[player]
    if persistent_clan_tag:
        available_clans.sort(key=lambda clan: clan.tag != persistent_clan_tag)
    if opponent_clan_tag and opponent_finished:
        available_clans.sort(key=lambda clan: clan.tag == opponent_clan_tag)
    return available_clans


async def request_live_match(
        runtime: SelfHostingRuntime,
        ctx: ipy.SlashContext,
) -> None:
    await ctx.defer(ephemeral=True)
    match = await Match.find_one(Match.thread == str(ctx.channel.id))
    participants = match.participants[:2] if match else []
    caller_id = str(ctx.author.id)
    if len(participants) != 2 or caller_id not in [participant.user_id for participant in participants]:
        await ctx.send(f"{ERROR_EMOJI} Only a match participant can request a live match.", ephemeral=True)
        return
    if any(is_match_finished(match, player=index) for index in range(2)):
        await ctx.send(f"{ERROR_EMOJI} This match has already finished.", ephemeral=True)
        return
    if hosting_started(match):
        await ctx.send(f"{ERROR_EMOJI} This match is already being started.", ephemeral=True)
        return
    requester_index = next(
        index for index, participant in enumerate(participants) if participant.user_id == caller_id
    )
    await runtime.live_requests.post(
        ctx, match, requester_index, stored_reservation_id=ready_live_reservation_id(match),
    )


async def start_accepted_live_match(
        runtime: SelfHostingRuntime,
        ctx: ipy.ComponentContext,
        identity: LiveRequestIdentity,
        match: Match,
        requester_index: int,
) -> None:
    stored_reservation_id = ready_live_reservation_id(match, identity.stored_reservation_id)
    if identity.stored_reservation_id and stored_reservation_id is None:
        await ctx.send(f"{ERROR_EMOJI} The stored live bases are no longer ready.", ephemeral=True)
        return
    if stored_reservation_id:
        match_commands = runtime.bot.get_ext("MatchCommands")
        if match_commands is None:
            await ctx.send(f"{ERROR_EMOJI} Automatic live hosting is currently unavailable.", ephemeral=True)
            return
        await match_commands._start_live(ctx, LivePreflight(
            clan=live_stores(match, stored_reservation_id)[0][1].clan_tag,
            host=runtime.bot.user, scout=False, automatic=True, requested_by=identity.requester_id,
            defer_response=False, public_progress=True, allow_opponent_cancel=True,
            stored_reservation_id=stored_reservation_id,
        ))
        return
    clans = await Clan.find_all().to_list()
    available_clans = [
        clan for clan in clans
        if (
            clan.availability_str == "Available"
            and not is_resource_clan_locked(clan.tag)
            and live_eligibility.all_base_store_slots_available(clan.available_slots)
        )
    ]
    if not available_clans:
        await ctx.send(
            f"{ERROR_EMOJI} There are no available helper clans.\n\n{format_available_resource_clans(clans)}",
            ephemeral=True,
        )
        return
    available_clans = await order_self_host_clans(runtime.bot, match, requester_index, available_clans)
    match_commands = runtime.bot.get_ext("MatchCommands")
    if match_commands is None:
        await ctx.send(
            f"{ERROR_EMOJI} Automatic live hosting is currently unavailable. Please contact a helper.",
            ephemeral=True,
        )
        return
    await match_commands._start_live(
        ctx,
        LivePreflight(
            clan=available_clans[0].tag,
            host=runtime.bot.user,
            scout=False,
            automatic=True,
            requested_by=identity.requester_id,
            defer_response=False,
            public_progress=True,
            allow_opponent_cancel=True,
        ),
    )

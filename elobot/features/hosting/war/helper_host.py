from dataclasses import dataclass
from typing import Protocol

import coc
import interactions as ipy

import elobot.extensions.match as match_commands
import elobot.features.hosting.war.result_store_task as result_store_task
import elobot.features.hosting.war.war_start as war_start
from elobot.features.hosting.war.district_selection import match_district_index, started_host_clan_tag
from elobot.integrations.helper_api import (
    HelperJobCreateRequest,
    HelperHostWarMatchPayload,
    HelperHostWarMatchOnStoredPayload,
)
from elobot.features.hosting.war.helper_host_job import (
    HelperHostJobRequest,
    confirm_helper_availability,
    run_helper_host_job,
)
from elobot.features.hosting.war.helper_slot import get_available_helper_slot
from elobot.config.emojis import ERROR_EMOJI
from elobot.features.base_store.start import StoredStartState
from elobot.db import Match, Player
from elobot.features.matches import attack_bases
from elobot.features.matches import resources


@dataclass(frozen=True, slots=True)
class AutomaticWarHost:
    match: Match
    player: int
    clan: coc.Clan
    host: ipy.BaseUser
    scout: bool = False
    submitter_id: str | None = None
    clan_reservation: "match_commands.ResourceClanReservation | None" = None
    side_reservation: "match_commands.ResourceMatchSideReservation | None" = None
    stored_start: StoredStartState | None = None


@dataclass(frozen=True, slots=True)
class AutomaticWarDistrictHost:
    match: Match
    player: int
    clan: coc.Clan
    district: str
    requested_by: str
    public_progress: bool = False


class HelperHostRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str


async def host_war_automatically(
        runtime: HelperHostRuntime,
        ctx: ipy.SlashContext,
        request: AutomaticWarHost,
) -> None:
    slot = None
    if request.stored_start is None:
        slot = await get_available_helper_slot(request.clan.tag, bot=runtime.bot)
        if slot is None:
            await ctx.send(
                f"{ERROR_EMOJI} There are no empty base slots in **{request.clan.name}**.", ephemeral=True,
            )
            return
    match_doc = request.match
    opponent = 1 - request.player
    participant = match_doc.participants[request.player]
    player_doc = await Player.find_one(Player.user_id == participant.user_id)
    if request.stored_start is not None and player_doc is None:
        await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
        return
    player_tag = player_doc.tag
    if request.stored_start is not None:
        store = request.stored_start.stores[0][1]
        if store.clan_tag != request.clan.tag or not all(match_doc.final_districts):
            await ctx.send(f"{ERROR_EMOJI} The stored bases do not match this start request.", ephemeral=True)
            return
        create_request = HelperJobCreateRequest(
            job_type="host_war_match_on_stored",
            requested_by=str(ctx.author.id),
            payload=HelperHostWarMatchOnStoredPayload(
                host_clan_tag=store.clan_tag,
                player_tag=player_tag,
                districts=[district for district in match_doc.final_districts if district],
                slot=1 if store.slot == "Slot 1" else 2,
                remove_bases_after=False,
            ),
        )
    else:
        assert slot is not None
        player_user = await runtime.bot.fetch_user(participant.user_id)
        opponent_user = await runtime.bot.fetch_user(match_doc.participants[opponent].user_id)
        match_doc = await attack_bases.choose_bases(
            match_doc.token, player_user, opponent_user, return_match_data=True,
        )
        base_links = [base.link for base in match_doc.participants[opponent].bases if base and base.link]
        if not base_links:
            await ctx.send(f"{ERROR_EMOJI} No opponent bases are available for Helper hosting.", ephemeral=True)
            return
        opponent_player_tag = None
        if request.clan.tag == match_doc.host_clans[opponent]:
            opponent_player = await Player.find_one(Player.user_id == match_doc.participants[opponent].user_id)
            opponent_player_tag = opponent_player.tag
        create_request = HelperJobCreateRequest(
            job_type="host_war_match", requested_by=str(ctx.author.id),
            payload=HelperHostWarMatchPayload(
                host_clan_tag=request.clan.tag, player_tag=player_tag, base_links=base_links, slot=slot,
                opponent_player_tag=opponent_player_tag, kick_opponent=True if opponent_player_tag else None,
            ),
        )
    if not await confirm_helper_availability(runtime, ctx, create_request.job_type):
        return

    started_message = await war_start.start_war(
        runtime.bot,
        ctx,
        war_start.WarStart(
            match=match_doc,
            player=request.player,
            host=request.host,
            clan=request.clan,
            coc_client=runtime.bot.coc_client,
            reply=war_start.SelfHostedWarStartReply(),
            scout=request.scout,
            clan_reservation=request.clan_reservation,
            side_reservation=request.side_reservation,
            stored_start=request.stored_start,
        ),
    )
    if not started_message:
        await ctx.send(
            f"{ERROR_EMOJI} **{request.clan.name}** is currently occupied.\n\n"
            f"{await resources.available_resource_clans_text()}",
            ephemeral=True,
        )
        return

    async def abort_started(message: ipy.Message) -> bool:
        return await war_start.abort_war_start(
            ctx,
            war_start.WarAbort(match=match_doc, player=request.player, message=message),
        )

    def schedule_result_store() -> None:
        started_at = match_doc.times[request.player]
        if started_at is not None:
            result_store_task.schedule_automatic_result_store(
                runtime.bot,
                result_store_task.ScheduledResultStoreRequest(
                    token=str(match_doc.token),
                    player=request.player,
                    requested_by=str(ctx.author.id),
                    started_at=started_at,
                    submitter_id=request.submitter_id,
                    is_final=match_doc.format == "elo_final_seven",
                ),
            )

    await run_helper_host_job(
        runtime,
        ctx,
        HelperHostJobRequest(
            create_request=create_request,
            requester_id=str(ctx.author.id),
            started_message=started_message,
            abort_started=abort_started,
            on_success=schedule_result_store,
        ),
    )


async def host_war_district_automatically(
        runtime: HelperHostRuntime,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        request: AutomaticWarDistrictHost,
) -> None:
    match_doc = await Match.find_one({"token": str(request.match.token)})
    if match_doc is None:
        await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
        return
    if not match_doc.is_war or match_commands.is_match_finished(match_doc, player=request.player):
        await ctx.send(f"{ERROR_EMOJI} This match is not available for district hosting.", ephemeral=True)
        return

    host_clan_tag = started_host_clan_tag(match_doc, request.player)
    if host_clan_tag is None:
        await ctx.send(
            f"{ERROR_EMOJI} You can only select a district after this side of the match has started.",
            ephemeral=True,
        )
        return
    if request.clan.tag != host_clan_tag:
        await ctx.send(
            f"{ERROR_EMOJI} This side of the match is hosted in **{host_clan_tag}**. "
            "Select that clan for a district rerun.",
            ephemeral=True,
        )
        return

    slot = await get_available_helper_slot(request.clan.tag, bot=runtime.bot)
    if slot is None:
        await ctx.send(
            f"{ERROR_EMOJI} There are no empty base slots in **{request.clan.name}**.",
            ephemeral=True,
        )
        return

    district_index = match_district_index(match_doc, request.district)
    if district_index is None:
        await ctx.send(
            f"{ERROR_EMOJI} **{request.district}** is not one of this match's districts.",
            ephemeral=True,
        )
        return
    opponent = 1 - request.player
    opponent_bases = match_doc.participants[opponent].bases
    base = opponent_bases[district_index] if district_index < len(opponent_bases) else None
    if base is None or not base.link:
        await ctx.send(
            f"{ERROR_EMOJI} No opponent base is available for **{request.district}**.",
            ephemeral=True,
        )
        return

    participant = match_doc.participants[request.player]
    player_doc = await Player.find_one(Player.user_id == participant.user_id)
    if player_doc is None:
        await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
        return

    opponent_player_tag = None
    if request.clan.tag == match_doc.host_clans[opponent]:
        opponent_player = await Player.find_one(Player.user_id == match_doc.participants[opponent].user_id)
        opponent_player_tag = opponent_player.tag if opponent_player else None

    create_request = HelperJobCreateRequest(
        job_type="host_war_match",
        requested_by=request.requested_by,
        payload=HelperHostWarMatchPayload(
            host_clan_tag=request.clan.tag,
            player_tag=player_doc.tag,
            base_links=[base.link],
            slot=slot,
            opponent_player_tag=opponent_player_tag,
            kick_opponent=True if opponent_player_tag else None,
        ),
    )
    if not await confirm_helper_availability(runtime, ctx, create_request.job_type):
        return
    await run_helper_host_job(
        runtime,
        ctx,
        HelperHostJobRequest(
            create_request=create_request,
            requester_id=request.requested_by,
            public_progress=request.public_progress,
        ),
    )

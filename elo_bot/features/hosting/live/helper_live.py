# noqa: SIZE_OK - fresh and district live hosting share one paired-base lifecycle

from dataclasses import dataclass
from typing import Protocol, assert_never

import coc
import interactions as ipy

from elo_bot.core.match_results import is_match_finished
import elo_bot.features.hosting.live.eligibility as live_eligibility
import elo_bot.features.hosting.live.live_reservations as live_reservations
import elo_bot.features.hosting.live.live_start as live_start
from elo_bot.integrations.helper_api import (
    HelperHostLiveMatchPayload, HelperHostLiveMatchOnStoredPayload, HelperJobCreateRequest,
)
from elo_bot.features.hosting.war.helper_host_job import (
    HelperHostJobRequest,
    confirm_helper_availability,
    run_helper_host_job,
)
from elo_bot.features.hosting.war.district_selection import match_district_index, started_host_clan_tag
from elo_bot.features.base_store.live import live_pair_ready, live_stores
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.db import Match
from elo_bot.features.matches import attack_bases
from elo_bot.features.matches import resources


@dataclass(frozen=True, slots=True)
class AutomaticLiveHost:
    match: Match
    clan: coc.Clan
    host: ipy.BaseUser
    player1: coc.Player
    player2: coc.Player
    player1_elo: int
    player2_elo: int
    scout: bool
    requested_by: str
    public_progress: bool = False
    allow_opponent_cancel: bool = False
    stored_reservation_id: str | None = None


@dataclass(frozen=True, slots=True)
class AutomaticLiveDistrictHost:
    match: Match
    clan: coc.Clan
    player1_tag: str
    player2_tag: str
    district: str
    requested_by: str
    public_progress: bool = False
    allow_opponent_cancel: bool = False


@dataclass(frozen=True, slots=True)
class PairedLiveBases:
    for_player1: list[str]
    for_player2: list[str]


class HelperLiveRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str


def opponent_cancel_user_ids(match: Match, requester_id: str) -> frozenset[str]:
    participant_ids = frozenset(participant.user_id for participant in match.participants[:2])
    if requester_id not in participant_ids:
        return frozenset()
    return participant_ids - {requester_id}


def parse_paired_live_bases(match: Match, district: str | None = None) -> PairedLiveBases | None:
    districts = match.final_districts
    if not 1 <= len(districts) <= 9 or len(set(districts)) != len(districts):
        return None
    if not all(isinstance(district, str) and district for district in districts):
        return None

    player1_bases = match.participants[0].bases
    player2_bases = match.participants[1].bases
    if len(player1_bases) != len(districts) or len(player2_bases) != len(districts):
        return None
    if district is None:
        district_indexes = range(len(districts))
    else:
        district_index = match_district_index(match, district)
        if district_index is None:
            return None
        district_indexes = (district_index,)
    player1_links = []
    player2_links = []
    for index in district_indexes:
        player1_base = player1_bases[index]
        player2_base = player2_bases[index]
        if not player1_base or not player1_base.link or not player2_base or not player2_base.link:
            return None
        player1_links.append(player2_base.link)
        player2_links.append(player1_base.link)
    if len(set(player1_links)) != len(player1_links) or len(set(player2_links)) != len(player2_links):
        return None
    return PairedLiveBases(for_player1=player1_links, for_player2=player2_links)


async def host_live_automatically(
        runtime: HelperLiveRuntime,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        request: AutomaticLiveHost,
) -> None:
    reservation_outcome = await live_start.reserve_live_start(str(request.match.token), request.clan.tag)
    match reservation_outcome:
        case live_reservations.LiveStartReservations() as reservations:
            pass
        case live_reservations.LiveReservationsBusy():
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        case live_reservations.LiveReservationsClanUnavailable():
            await ctx.send(
                f"{ERROR_EMOJI} **{request.clan.name}** is currently occupied.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        case unreachable:
            assert_never(unreachable)
    keep_stored_reservations = False
    try:
        if request.stored_reservation_id is None:
            if not await live_eligibility.clan_has_all_base_store_slots(request.clan.tag, bot=runtime.bot):
                await ctx.send(
                    f"{ERROR_EMOJI} Both base slots in **{request.clan.name}** must be available for a live match.",
                    ephemeral=True,
                )
                return
            player1_user = await runtime.bot.fetch_user(request.match.participants[0].user_id)
            player2_user = await runtime.bot.fetch_user(request.match.participants[1].user_id)
            await attack_bases.choose_bases(
                str(request.match.token),
                player1_user,
                player2_user,
                return_match_data=True,
            )
            await attack_bases.choose_bases(
                str(request.match.token),
                player2_user,
                player1_user,
                return_match_data=True,
            )
        match_doc = await Match.find_one({"token": str(request.match.token)})
        if match_doc is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
            return
        if is_match_finished(match_doc):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return
        if live_start.hosting_started(match_doc):
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        if request.stored_reservation_id and (
                not live_pair_ready(match_doc, request.stored_reservation_id)
                or live_stores(match_doc, request.stored_reservation_id)[0][1].clan_tag != request.clan.tag):
            await ctx.send(f"{ERROR_EMOJI} The stored live bases changed or are no longer ready.", ephemeral=True)
            return
        paired_bases = parse_paired_live_bases(match_doc)
        if paired_bases is None:
            await ctx.send(
                f"{ERROR_EMOJI} A complete, distinct pair of bases is required for Helper hosting.",
                ephemeral=True,
            )
            return

        create_request = HelperJobCreateRequest(
            job_type="host_live_match",
            requested_by=request.requested_by,
            payload=HelperHostLiveMatchPayload(
                host_clan_tag=request.clan.tag,
                player_1_tag=request.player1.tag,
                player_2_tag=request.player2.tag,
                base_links_for_player_1=paired_bases.for_player1,
                base_links_for_player_2=paired_bases.for_player2,
            ),
        )
        if request.stored_reservation_id:
            create_request = HelperJobCreateRequest(
                job_type="host_live_match_on_stored", requested_by=request.requested_by,
                payload=HelperHostLiveMatchOnStoredPayload(
                    host_clan_tag=request.clan.tag, player_1_tag=request.player1.tag, player_2_tag=request.player2.tag,
                    districts=list(match_doc.final_districts), remove_bases_after=False,
                ),
            )
        if not await confirm_helper_availability(
                runtime, ctx, create_request.job_type, public_progress=request.public_progress,
        ):
            return

        start_outcome = await live_start.start_live_match(
            runtime.bot,
            ctx,
            live_start.LiveStart(
                match=match_doc,
                host=request.host,
                clan=request.clan,
                player1=request.player1,
                player2=request.player2,
                player1_elo=request.player1_elo,
                player2_elo=request.player2_elo,
                scout=request.scout,
                reply=live_start.AutomaticLiveStartReply(),
                reservations=reservations,
                stored_reservation_id=request.stored_reservation_id,
            ),
        )
        keep_stored_reservations = bool(
            request.stored_reservation_id and isinstance(start_outcome, live_start.LiveStartSucceeded)
        )
    finally:
        if not keep_stored_reservations:
            reservations.release()

    match start_outcome:
        case live_start.LiveStartSucceeded(message=started_message):
            pass
        case live_start.LiveStartBusy():
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        case live_start.LiveStartClanUnavailable():
            await ctx.send(
                f"{ERROR_EMOJI} **{request.clan.name}** is currently occupied.\n\n"
                f"{await resources.available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        case unreachable:
            assert_never(unreachable)

    async def abort_started(message: ipy.Message) -> bool:
        return await live_start.abort_live_start(
            ctx,
            live_start.LiveAbort(match=match_doc, message=message),
        )

    try:
        await run_helper_host_job(
            runtime,
            ctx,
            HelperHostJobRequest(
                create_request=create_request,
                requester_id=request.requested_by,
                additional_cancel_user_ids=(
                    opponent_cancel_user_ids(match_doc, request.requested_by)
                    if request.allow_opponent_cancel
                    else frozenset()
                ),
                started_message=started_message,
                abort_started=abort_started,
                public_progress=request.public_progress,
            ),
        )
    finally:
        if keep_stored_reservations:
            reservations.release()


async def host_live_district_automatically(
        runtime: HelperLiveRuntime,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        request: AutomaticLiveDistrictHost,
) -> None:
    match_doc = await Match.find_one({"token": str(request.match.token)})
    if match_doc is None:
        await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
        return
    if match_doc.is_war or is_match_finished(match_doc):
        await ctx.send(f"{ERROR_EMOJI} This match is not available for district hosting.", ephemeral=True)
        return

    host_clan_tag = started_host_clan_tag(match_doc, 0)
    if host_clan_tag is None:
        await ctx.send(
            f"{ERROR_EMOJI} You can only select a district after the live match has started.",
            ephemeral=True,
        )
        return
    if request.clan.tag != host_clan_tag:
        await ctx.send(
            f"{ERROR_EMOJI} This match is hosted in **{host_clan_tag}**. "
            "Select that clan for a district rerun.",
            ephemeral=True,
        )
        return
    if match_district_index(match_doc, request.district) is None:
        await ctx.send(
            f"{ERROR_EMOJI} **{request.district}** is not one of this match's districts.",
            ephemeral=True,
        )
        return

    paired_bases = parse_paired_live_bases(match_doc, request.district)
    if paired_bases is None:
        await ctx.send(
            f"{ERROR_EMOJI} Both players need a base for **{request.district}** before Helper can host it.",
            ephemeral=True,
        )
        return
    create_request = HelperJobCreateRequest(
        job_type="host_live_match",
        requested_by=request.requested_by,
        payload=HelperHostLiveMatchPayload(
            host_clan_tag=request.clan.tag,
            player_1_tag=request.player1_tag,
            player_2_tag=request.player2_tag,
            base_links_for_player_1=paired_bases.for_player1,
            base_links_for_player_2=paired_bases.for_player2,
        ),
    )
    if not await confirm_helper_availability(
            runtime, ctx, create_request.job_type, public_progress=request.public_progress,
    ):
        return
    await run_helper_host_job(
        runtime,
        ctx,
        HelperHostJobRequest(
            create_request=create_request,
            requester_id=request.requested_by,
            additional_cancel_user_ids=(
                opponent_cancel_user_ids(match_doc, request.requested_by)
                if request.allow_opponent_cancel
                else frozenset()
            ),
            public_progress=request.public_progress,
        ),
    )

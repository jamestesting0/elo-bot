# noqa: SIZE_OK - start, watch, cancel, continue and recover share one run lifecycle
import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from typing import assert_never

import aiohttp
import anyio
import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

import elo_bot.features.hosting.live.eligibility as live_eligibility
import elo_bot.features.hosting.live.live_reservations as live_reservations
import elo_bot.features.hosting.live.live_start as live_start
import elo_bot.features.hosting.war.war_start as war_start
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES, LVL2_ROLES
from elo_bot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elo_bot.core.finals import FINAL_DISTRICTS, FinalError
from elo_bot.core.match_results import is_match_finished
from elo_bot.db import EloFinal, Match, Player
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun, SequentialRunEvent
from elo_bot.features.base_store.live import live_pair_ready, live_stores
from elo_bot.features.base_store.publication import BaseStoreMessageConflict
from elo_bot.features.hosting.live.helper_live import (
    AutomaticLiveHost,
    parse_paired_live_bases,
    unpaired_bases_message,
)
from elo_bot.features.hosting.sequential import ui
from elo_bot.features.hosting.sequential.replay import open_replay, replay_scores, validate_replay_retry
from elo_bot.features.hosting.sequential.policy import (
    LIST_PAGE_SIZE,
    MAX_POLL_BACKOFF_SECONDS,
    POLL_INTERVAL_SECONDS,
    PROMPT_TIMEOUT_SECONDS,
)
from elo_bot.features.hosting.sequential.runs import (
    close_run,
    event_push,
    is_tracked,
    latest_run,
    match_runs,
    open_regular_run,
    original_run,
    played_district_codes,
    record_event,
    remaining_districts,
    result_sides,
    run_result,
    transition,
)
from elo_bot.features.hosting.sequential.scores import store_replay_scores, store_verified_scores
from elo_bot.features.hosting.war.helper_host import AutomaticWarHost
from elo_bot.features.hosting.war.helper_slot import get_available_helper_slot
from elo_bot.features.hosting.war.result_store import complete_stored_results
from elo_bot.features.matches import attack_bases, resources
from elo_bot.features.matches.timing import hosting_started
from elo_bot.integrations.helper_api import (
    HELPER_SEQUENTIAL_RESERVATION_PREFIX,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateConflict,
    HelperJobCreated,
    HelperJobCreateOutcome,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperJobNotFound,
    HelperSequentialClient,
    HelperSequentialJob,
    HelperSequentialJobCreateRequest,
    HelperSequentialLiveOnStoredPayload,
    HelperSequentialLivePayload,
    HelperSequentialResult,
    HelperSequentialWarPayload,
    HelperTrackingFailure,
)

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
_WATCHERS: dict[PydanticObjectId, asyncio.Task[None]] = {}
_RUN_LOCKS: dict[PydanticObjectId, anyio.Lock] = {}


@dataclass(frozen=True, slots=True)
class LaunchRequest:
    match: Match
    side: int
    player_tags: list[str]
    request: HelperSequentialJobCreateRequest
    districts: list[str]
    start_message_id: int | None
    previous_run_id: PydanticObjectId | None = None
    replay_source: HelperSequentialRun | None = None
    replay_baseline: dict[str, list[str | None]] | None = None


def helper_client() -> HelperSequentialClient:
    return HelperSequentialClient(HELPER_API_BASE_URL, HELPER_API_KEY)


def helper_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))


def is_staff(user: ipy.Member) -> bool:
    return any(user.has_role(role) for role in LVL2_ROLES)


def match_side(match: Match, player: int | None) -> int | None:
    return player if match.is_war else LIVE_SIDE


async def clan_refusal(clan_tag: str) -> str | None:
    if await HelperSequentialRun.find_one({"host_clan_tag": clan_tag, "open": True}) is None:
        return None
    return (f"{ERROR_EMOJI} Helper is already hosting a sequential match in this clan. "
            "Choose another clan or wait for that run to finish.")


async def host_war_sequentially(bot: ipy.Client, ctx: ipy.SlashContext | ipy.ComponentContext,
                                host: AutomaticWarHost) -> None:
    if refusal := await clan_refusal(host.clan.tag):
        await ctx.send(refusal, ephemeral=True)
        return
    match_doc = host.match
    opponent = 1 - host.player
    player_doc = await Player.find_one(Player.user_id == match_doc.participants[host.player].user_id)
    if player_doc is None:
        await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
        return
    if host.stored_start is not None:
        store = host.stored_start.stores[0][1]
        if store.clan_tag != host.clan.tag:
            await ctx.send(f"{ERROR_EMOJI} The stored bases do not match this start request.", ephemeral=True)
            return
        payload = HelperSequentialWarPayload(
            host_clan_tag=store.clan_tag, base_source="stored", player_tag=player_doc.tag,
            districts=list(FINAL_DISTRICTS), slot=1 if store.slot == "Slot 1" else 2, remove_bases_after=False,
        )
    else:
        slot = await get_available_helper_slot(host.clan.tag, bot=bot)
        if slot is None:
            await ctx.send(f"{ERROR_EMOJI} There are no empty base slots in **{host.clan.name}**.", ephemeral=True)
            return
        player_user = await bot.fetch_user(match_doc.participants[host.player].user_id)
        opponent_user = await bot.fetch_user(match_doc.participants[opponent].user_id)
        match_doc = await attack_bases.choose_bases(match_doc.token, player_user, opponent_user,
                                                    return_match_data=True)
        if refusal := attack_bases.missing_bases_message(match_doc, (opponent,)):
            await ctx.send(refusal, ephemeral=True)
            return
        links = [base.link for base in match_doc.participants[opponent].bases]
        opponent_tag = None
        if host.clan.tag == match_doc.host_clans[opponent]:
            opponent_player = await Player.find_one(Player.user_id == match_doc.participants[opponent].user_id)
            opponent_tag = opponent_player.tag
        payload = HelperSequentialWarPayload(
            host_clan_tag=host.clan.tag, base_source="links", player_tag=player_doc.tag, base_links=links,
            slot=slot, opponent_player_tag=opponent_tag, kick_opponent=True if opponent_tag else None,
        )
    request = HelperSequentialJobCreateRequest(
        job_type="host_sequential_war_match", requested_by=str(ctx.author.id), payload=payload,
    )
    try:
        started_message = await war_start.start_war(bot, ctx, war_start.WarStart(
            match=match_doc, player=host.player, host=host.host, clan=host.clan, coc_client=bot.coc_client,
            reply=war_start.SelfHostedWarStartReply(), clan_reservation=host.clan_reservation,
            side_reservation=host.side_reservation, stored_start=host.stored_start,
        ))
    except war_start.WarStartRefused as refusal:
        await ctx.send(str(refusal), ephemeral=True)
        return
    if not started_message:
        await ctx.send(f"{ERROR_EMOJI} **{host.clan.name}** is currently occupied.\n\n"
                       f"{await resources.available_resource_clans_text()}", ephemeral=True)
        return
    await launch_run(bot, ctx, LaunchRequest(
        match=match_doc, side=host.player, player_tags=[player_doc.tag], request=request,
        districts=list(FINAL_DISTRICTS), start_message_id=int(started_message.id),
    ))


async def host_live_sequentially(bot: ipy.Client, ctx: ipy.SlashContext | ipy.ComponentContext,
                                 host: AutomaticLiveHost) -> None:
    if refusal := await clan_refusal(host.clan.tag):
        await ctx.send(refusal, ephemeral=True)
        return
    # Helper's attacks_first takes the tag of the player whose challenge goes first; omitted posts both together.
    first_attacker = None if host.attacks_first is None else (host.player1, host.player2)[host.attacks_first].tag
    reservation_outcome = host.reservations or await live_start.reserve_live_start(
        str(host.match.token), host.clan.tag,
    )
    match reservation_outcome:
        case live_reservations.LiveStartReservations() as reservations:
            pass
        case live_reservations.LiveReservationsBusy():
            await ctx.send(f"{ERROR_EMOJI} This match is already being started.", ephemeral=True)
            return
        case live_reservations.LiveReservationsClanUnavailable():
            await ctx.send(f"{ERROR_EMOJI} **{host.clan.name}** is currently occupied.\n\n"
                           f"{await resources.available_resource_clans_text()}", ephemeral=True)
            return
        case unreachable:
            assert_never(unreachable)
    try:
        if host.stored_reservation_id is None:
            if not await live_eligibility.clan_has_all_base_store_slots(host.clan.tag, bot=bot):
                await ctx.send(f"{ERROR_EMOJI} Both base slots in **{host.clan.name}** must be available "
                               "for a live match.", ephemeral=True)
                return
            player1_user = await bot.fetch_user(host.match.participants[0].user_id)
            player2_user = await bot.fetch_user(host.match.participants[1].user_id)
            await attack_bases.choose_bases(str(host.match.token), player1_user, player2_user, return_match_data=True)
            await attack_bases.choose_bases(str(host.match.token), player2_user, player1_user, return_match_data=True)
        match_doc = await Match.find_one({"token": str(host.match.token)})
        if match_doc is None or is_match_finished(match_doc) or hosting_started(match_doc):
            await ctx.send(f"{ERROR_EMOJI} This match is already being started or has finished.", ephemeral=True)
            return
        if host.stored_reservation_id:
            if (not live_pair_ready(match_doc, host.stored_reservation_id)
                    or live_stores(match_doc, host.stored_reservation_id)[0][1].clan_tag != host.clan.tag):
                await ctx.send(f"{ERROR_EMOJI} The stored live bases changed or are no longer ready.",
                               ephemeral=True)
                return
            job_type = "host_sequential_live_match_on_stored"
            payload = HelperSequentialLiveOnStoredPayload(
                host_clan_tag=host.clan.tag, player_1_tag=host.player1.tag,
                player_2_tag=host.player2.tag, districts=list(FINAL_DISTRICTS), remove_bases_after=False,
                attacks_first=first_attacker,
            )
        else:
            paired = parse_paired_live_bases(match_doc)
            if paired is None:
                await ctx.send(unpaired_bases_message(match_doc), ephemeral=True)
                return
            job_type = "host_sequential_live_match"
            payload = HelperSequentialLivePayload(
                host_clan_tag=host.clan.tag, player_1_tag=host.player1.tag,
                player_2_tag=host.player2.tag, base_links_for_player_1=paired.for_player1,
                base_links_for_player_2=paired.for_player2, attacks_first=first_attacker,
            )
        start_outcome = await live_start.start_live_match(bot, ctx, live_start.LiveStart(
            match=match_doc, host=host.host, clan=host.clan, player1=host.player1, player2=host.player2,
            player1_elo=host.player1_elo, player2_elo=host.player2_elo,
            reply=live_start.AutomaticLiveStartReply(), reservations=reservations,
            stored_reservation_id=host.stored_reservation_id,
        ))
        match start_outcome:
            case live_start.LiveStartSucceeded(message=started_message):
                request = HelperSequentialJobCreateRequest(
                    job_type=job_type, requested_by=str(ctx.author.id), payload=payload,
                )
                await launch_run(bot, ctx, LaunchRequest(
                    match=match_doc, side=LIVE_SIDE, player_tags=[host.player1.tag, host.player2.tag], request=request,
                    districts=list(FINAL_DISTRICTS), start_message_id=int(started_message.id),
                ))
            case live_start.LiveStartBusy():
                await ctx.send(f"{ERROR_EMOJI} The match changed before hosting could start.", ephemeral=True)
            case live_start.LiveStartRefused():
                return
            case live_start.LiveStartClanUnavailable():
                await ctx.send(f"{ERROR_EMOJI} **{host.clan.name}** is currently occupied.\n\n"
                               f"{await resources.available_resource_clans_text()}", ephemeral=True)
            case unreachable:
                assert_never(unreachable)
    finally:
        reservations.release()


async def launch_run(bot: ipy.Client, ctx: ipy.BaseContext, launch: LaunchRequest) -> None:
    match = launch.match
    actor_id = str(ctx.author.id)
    run = HelperSequentialRun(
        id=PydanticObjectId(),
        match_id=match.id, match_token=str(match.token), thread_id=str(match.thread), final_id=match.final_id,
        final_node_id=match.final_node_id, final_contestant_revision=match.final_contestant_revision,
        final_attempt=match.final_attempt, side=launch.side, host_clan_tag=launch.request.payload.host_clan_tag,
        player_tags=launch.player_tags, districts=launch.districts,
        create_request=launch.request.model_dump(by_alias=True, exclude_none=True),
        requested_by=actor_id, previous_run_id=launch.previous_run_id, start_message_id=launch.start_message_id,
        replay_baseline=launch.replay_baseline,
        state="CREATING", created_at=time.time(),
        events=[SequentialRunEvent(at=time.time(), kind="created", detail=", ".join(launch.districts),
                                   actor_id=actor_id)],
    )
    async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
        await _register_and_queue(bot, ctx, launch, run)


async def _register_and_queue(bot: ipy.Client, ctx: ipy.BaseContext, launch: LaunchRequest,
                              run: HelperSequentialRun) -> None:
    try:
        if launch.replay_source is None:
            if launch.request.job_type == "host_sequential_live_match_on_stored" or (
                    isinstance(launch.request.payload, HelperSequentialWarPayload)
                    and launch.request.payload.base_source == "stored"):
                run.layout_storage_ids = [launch.match.participants[side].base_store.reservation_id
                                          for side in result_sides(run.side)]
            await open_regular_run(run)
        else:
            await open_replay(run, launch.replay_source)
            run = await HelperSequentialRun.get(run.id)
    except FinalError as error:
        await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
        return
    except DuplicateKeyError:
        # The open run owns this match start, so it must not be rolled back here.
        await ctx.send(f"{ERROR_EMOJI} Another Helper run is already open for this side. "
                       "Use `/match helper status`.", ephemeral=True)
        return
    async with helper_session() as session:
        outcome = await helper_client().create(session, _saved_create_request(run))
    await apply_create_outcome(bot, ctx, run, outcome)


async def apply_create_outcome(
        bot: ipy.Client, ctx: ipy.BaseContext, run: HelperSequentialRun, outcome: HelperJobCreateOutcome,
) -> None:
    actor_id = str(ctx.author.id)
    match outcome:
        case HelperJobCreated(id=job_id):
            if not await transition(run, {"state": "ACTIVE", "job_id": job_id, "job_status": "queued"},
                                    "job_created", job_id, actor_id):
                current = await HelperSequentialRun.get(run.id)
                if current is None or not current.open:
                    try:
                        async with helper_session() as session:
                            await helper_client().cancel(session, job_id)
                    except (aiohttp.ClientError, TimeoutError):
                        logging.exception("Unable to cancel Helper job %s after its run closed", job_id)
                await ctx.send(f"{ERROR_EMOJI} The Helper run changed. Check `/match helper status`.", ephemeral=True)
                return
            start_watcher(bot, run.id)
            active = await HelperSequentialRun.get(run.id)
            try:
                if await progress_message(bot, active) is None:
                    await publish_progress(bot, active, ctx.channel)
                else:
                    await refresh_progress(bot, active)
            except ipy.errors.HTTPException:
                logging.exception("Unable to post Helper sequential progress for run %s", run.id)
                await record_event(run, "progress_unpublished")
                await ctx.send(f"{IMPORTANT_EMOJI} Helper sequential hosting started and is being tracked, but the "
                               "progress message could not be posted. Run `/match helper recover` to post it.",
                               ephemeral=True)
                return
            await ctx.send(f"{SUCCESS_EMOJI} Helper sequential hosting started. Progress is posted in this thread.",
                           ephemeral=True)
        case HelperJobCreateConflict(error=error) | HelperJobCreateRejected(error=error):
            reason = f"Helper rejected the job: {error or 'no reason given'}"
            await close_run(run, reason, actor_id)
            notice = await rollback_first_run(ctx, run)
            if run.replay_baseline is not None:
                closed = await HelperSequentialRun.get(run.id)
                if await progress_message(bot, closed) is None:
                    await publish_progress(bot, closed, ctx.channel)
                else:
                    await refresh_progress(bot, closed)
            await ctx.send(f"{ERROR_EMOJI} {reason}{notice}", ephemeral=True)
        case HelperJobCreateUnavailable() | HelperJobCreateAcceptedInvalid():
            await transition(run, {"state": "UNCERTAIN"}, "create_uncertain", actor_id=actor_id)
            await publish_progress(bot, await HelperSequentialRun.get(run.id), ctx.channel)
            next_step = "review your replay options" if run.replay_baseline is not None else "check and adopt it"
            await ctx.send(f"{IMPORTANT_EMOJI} Helper's response could not be confirmed, so the job may be running. "
                           f"Run `/match helper recover` to {next_step}.", ephemeral=True)
        case unreachable:
            assert_never(unreachable)


async def rollback_first_run(ctx: ipy.BaseContext, run: HelperSequentialRun) -> str:
    if run.previous_run_id is not None:
        return ""
    match = await Match.get(run.match_id)
    if match is None:
        return "\n\nThe match no longer exists."
    message = await ctx.channel.fetch_message(run.start_message_id) if run.start_message_id else None
    try:
        if run.side == LIVE_SIDE:
            aborted = await live_start.abort_live_start(ctx, live_start.LiveAbort(match=match, message=message))
        else:
            aborted = message is not None and await war_start.abort_war_start(
                ctx, war_start.WarAbort(match=match, player=run.side, message=message),
            )
    except BaseStoreMessageConflict as error:
        return f"\n\nMatch aborted. {error}"
    await record_event(run, "start_rolled_back" if aborted else "rollback_failed", actor_id=str(ctx.author.id))
    return ("\n\nMatch aborted." if aborted
            else "\n\nMatch could not be aborted safely, please contact a helper.")


def sides_stored(match: Match, side: int) -> bool:
    return all(all(match.participants[player].result) for player in result_sides(side))


def _start_side(run: HelperSequentialRun) -> int:
    return 0 if run.side == LIVE_SIDE else run.side


def progress_components(run: HelperSequentialRun, match: Match | None) -> list[ipy.ActionRow]:
    replay_active = match is not None and isinstance(match.final_replay_run_id, PydanticObjectId)
    eligible = (not run.open and (run.job_id is not None or run.replay_baseline is not None) and match is not None
                and match.host_clans[_start_side(run)] == run.host_clan_tag and not replay_active)
    can_continue = eligible and not is_match_finished(match) and not sides_stored(match, run.side)
    can_replay = eligible and not isinstance(match.final_result_id, str) and match.final_superseded is not True
    buttons = ui.progress_components(
        run, can_continue=can_continue, can_submit=match is not None and not replay_active
        and complete_stored_results(match), match_token=run.match_token, can_replay=can_replay,
    )
    return [ipy.ActionRow(*buttons)] if buttons else []


async def publish_progress(bot: ipy.Client, run: HelperSequentialRun, channel: ipy.TYPE_MESSAGEABLE_CHANNEL) -> None:
    match = await Match.get(run.match_id)
    message = await channel.send(embed=ui.progress_embed(run, match), components=progress_components(run, match))
    await HelperSequentialRun.get_pymongo_collection().update_one(
        {"_id": run.id}, {"$set": {"progress_channel_id": int(channel.id), "progress_message_id": int(message.id)}},
    )


async def progress_message(bot: ipy.Client, run: HelperSequentialRun) -> ipy.Message | None:
    if run.progress_channel_id is None or run.progress_message_id is None:
        return None
    try:
        channel = await bot.fetch_channel(run.progress_channel_id)
        return await channel.fetch_message(run.progress_message_id) if channel else None
    except ipy.errors.NotFound:
        return None


async def refresh_progress(bot: ipy.Client, run: HelperSequentialRun, *, retire: bool = False) -> None:
    message = await progress_message(bot, run)
    if message is None:
        return
    match = await Match.get(run.match_id)
    try:
        await edit_channel_message(message, embed=ui.progress_embed(run, match),
                                   components=[] if retire else progress_components(run, match))
    except ipy.errors.HTTPException:
        logging.exception("Unable to publish Helper sequential progress for run %s", run.id)


async def stale_reason(run: HelperSequentialRun, match: Match | None) -> str | None:
    if match is None:
        return "The match no longer exists."
    final = await EloFinal.get(PydanticObjectId(run.final_id))
    if final is not None and final.state in {"CANCELLING", "CANCELLED"}:
        return "The Final was cancelled."
    if match.final_superseded or (match.final_contestant_revision, match.final_attempt) != (
            run.final_contestant_revision, run.final_attempt):
        return "The Final pairing changed."
    if match.final_result_id is not None:
        return "The Final result was submitted."
    return None


def completed_codes(snapshot: dict | None) -> set[str]:
    return played_district_codes(HelperSequentialResult.model_validate(snapshot)) if snapshot else set()


async def sync_run(bot: ipy.Client, run_id: PydanticObjectId) -> HelperSequentialRun | None:
    async with _RUN_LOCKS.setdefault(run_id, anyio.Lock()):
        run = await HelperSequentialRun.get(run_id)
        if run is None or not run.open or run.state != "ACTIVE":
            return run
        run = await _poll_active_run(bot, run)
        if run is not None:
            await refresh_progress(bot, run)
        return run


async def _poll_active_run(bot: ipy.Client, run: HelperSequentialRun) -> HelperSequentialRun | None:
    match = await Match.get(run.match_id)
    stale = await stale_reason(run, match)
    client = helper_client()
    async with helper_session() as session:
        outcome = await client.read(session, run.job_id)
        if stale and isinstance(outcome, HelperSequentialJob) and outcome.status not in TERMINAL_JOB_STATUSES:
            try:
                await client.cancel(session, run.job_id)
            except (aiohttp.ClientError, TimeoutError) as error:
                await record_event(run, "cancel_failed", str(error))
            else:
                outcome = await client.read(session, run.job_id)
    await apply_read(bot, run, match, outcome, stale)
    return await HelperSequentialRun.get(run.id)


async def apply_read(
        bot: ipy.Client,
        run: HelperSequentialRun,
        match: Match | None,
        outcome: HelperSequentialJob | HelperJobNotFound | HelperTrackingFailure,
        stale: str | None,
) -> None:
    collection = HelperSequentialRun.get_pymongo_collection()
    now = time.time()
    match outcome:
        case HelperJobNotFound():
            await close_run(run, "Helper no longer has this job. A manager must check the match.",
                            updates={"last_polled_at": now})
        case HelperTrackingFailure(reason=reason):
            update = {"$set": {"last_polled_at": now, "last_poll_error": reason}, "$inc": {"poll_failures": 1}}
            if run.poll_failures == 0:
                update |= event_push("poll_failed", reason)
            await collection.update_one({"_id": run.id}, update)
        case HelperSequentialJob() as job:
            if run.replay_baseline is not None and (job.id != run.job_id or not players_match(run, job)):
                reason = "Helper returned a job for a different replay or player pairing."
                await collection.update_one({"_id": run.id}, {
                    "$set": {"last_polled_at": now, "last_poll_error": reason}, "$inc": {"poll_failures": 1},
                })
                return
            snapshot = job.result.model_dump(mode="json") if job.result else run.snapshot
            await collection.update_one({"_id": run.id}, {"$set": {
                "snapshot": snapshot, "job_status": job.status, "error": job.error, "last_polled_at": now,
                "poll_failures": 0, "last_poll_error": None,
            }})
            if job.status != run.job_status:
                await record_event(run, "status", job.status)
            for code in sorted(completed_codes(snapshot) - completed_codes(run.snapshot)):
                await record_event(run, "district_played", code)
            if match is not None and stale is None:
                if run.replay_baseline is not None:
                    if result := job.result or run_result(run):
                        await store_replay_scores(run, result)
                elif job.result is not None:
                    for stored in await store_verified_scores(run, job.result):
                        await record_event(run, "score_stored", stored)
            if job.status in TERMINAL_JOB_STATUSES:
                detail = f": {job.error}" if job.error else ""
                if await close_run(run, stale or f"Helper job {job.status}{detail}.") and stale is None:
                    await finish_stored_sides(bot, run)
        case unreachable:
            assert_never(unreachable)


async def finish_stored_sides(bot: ipy.Client, run: HelperSequentialRun) -> None:
    match = await Match.get(run.match_id)
    match_commands = bot.get_ext("MatchCommands")
    if match is None or match_commands is None:
        return
    preserve_live_clan = run.replay_baseline is not None and run.side == LIVE_SIDE and not sides_stored(match, LIVE_SIDE)
    for side in result_sides(run.side):
        if run.replay_baseline is not None:
            index = result_sides(run.side).index(side)
            if all(scores[index] for scores in run.replay_baseline.values()):
                continue
        if all(match.participants[side].result):
            await match_commands._finish_stored_result_side(
                match, side, **({"release_clan": False} if preserve_live_clan else {}),
            )


def is_watching(run_id: PydanticObjectId) -> bool:
    task = _WATCHERS.get(run_id)
    return task is not None and not task.done()


def start_watcher(bot: ipy.Client, run_id: PydanticObjectId) -> None:
    if is_watching(run_id):
        return
    task = asyncio.create_task(watch_run(bot, run_id), name=f"helper_sequential_run:{run_id}")
    _WATCHERS[run_id] = task
    task.add_done_callback(log_watcher_exit)


def log_watcher_exit(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        logging.error("Helper sequential watcher %s stopped", task.get_name(), exc_info=task.exception())


async def watch_run(bot: ipy.Client, run_id: PydanticObjectId) -> None:
    while True:
        run = await sync_run(bot, run_id)
        if run is None or not run.open or run.state != "ACTIVE":
            return
        await anyio.sleep(min(POLL_INTERVAL_SECONDS * 2 ** run.poll_failures, MAX_POLL_BACKOFF_SECONDS))


async def choose(bot: ipy.Client, ctx: ipy.BaseContext, prompt: str, *buttons: ipy.Button) -> str | None:
    row = ipy.ActionRow(*buttons)
    message = await ctx.send(prompt, components=row, ephemeral=True)
    try:
        used = (await bot.wait_for_component(components=row, messages=int(message.id),
                                             timeout=PROMPT_TIMEOUT_SECONDS)).ctx
    except TimeoutError:
        await ctx.edit(message, components=[])
        return None
    await used.edit_origin(components=[])
    return used.custom_id


async def cancel_run(bot: ipy.Client, ctx: ipy.ComponentContext, run_id: PydanticObjectId) -> None:
    run = await HelperSequentialRun.get(run_id)
    if run is None or not run.open:
        await ctx.send(f"{ERROR_EMOJI} This Helper run has already ended.", ephemeral=True)
        return
    if str(ctx.author.id) != run.requested_by and not is_staff(ctx.author):
        await ctx.send(f"{ERROR_EMOJI} Only the requester or staff can cancel this Helper run.", ephemeral=True)
        return
    if run.state != "ACTIVE":
        await ctx.send(f"{ERROR_EMOJI} Helper has not confirmed this job yet. Run `/match helper recover` first.",
                       ephemeral=True)
        return
    choice = await choose(
        bot, ctx,
        (f"{IMPORTANT_EMOJI} Cancel this replay? Verified score replacements are kept."
         if run.replay_baseline is not None else
         f"{IMPORTANT_EMOJI} Cancel this Helper run? If Helper has not posted a challenge yet, the match start is "
         "rolled back. Otherwise the match and its stored scores are kept, and you can continue the remaining "
         "districts later."),
        ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel", emoji=ui.CROSS_EMOJI,
                   custom_id="seq_cancel_confirm"),
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Back", custom_id="seq_cancel_back"),
    )
    if choice != "seq_cancel_confirm":
        return
    async with _RUN_LOCKS.setdefault(run_id, anyio.Lock()):
        run = await HelperSequentialRun.get(run_id)
        if run is None or not run.open:
            await ctx.send(f"{ERROR_EMOJI} This Helper run has already ended.", ephemeral=True)
            return
        try:
            async with helper_session() as session:
                await helper_client().cancel(session, run.job_id)
        except (aiohttp.ClientError, TimeoutError):
            await ctx.send(f"{ERROR_EMOJI} Helper could not cancel the job. Please try again.", ephemeral=True)
            return
        await record_event(run, "cancel_requested", actor_id=str(ctx.author.id))
        run = await _poll_active_run(bot, run)
        if run is None or run.open:
            if run is not None:
                await refresh_progress(bot, run)
            await ctx.send(f"{IMPORTANT_EMOJI} Helper accepted the cancellation, but the run is still closing. "
                           "Check it with `/match helper status`.", ephemeral=True)
            return
        result = run_result(run)
        nothing_posted = result is None or not any(
            player.posted for record in result.completion.rounds for player in record.players.values()
        )
        try:
            notice = await rollback_first_run(ctx, run) if nothing_posted else ""
        finally:
            await refresh_progress(bot, run)
    await ctx.send(f"{SUCCESS_EMOJI} The Helper run was cancelled.{notice}", ephemeral=True)


def _saved_create_request(run: HelperSequentialRun) -> HelperSequentialJobCreateRequest:
    payload = dict(run.create_request["payload"])
    job_type = run.create_request["type"]
    if job_type == "host_sequential_live_match" and payload.pop("base_source", None) == "stored":
        job_type = "host_sequential_live_match_on_stored"
    return HelperSequentialJobCreateRequest.model_validate({**run.create_request, "type": job_type, "payload": payload})


def continuation_request(run: HelperSequentialRun, remaining: list[str], actor_id: str) -> HelperSequentialJobCreateRequest:
    request = _saved_create_request(run)
    payload = request.payload.model_dump(exclude_none=True)
    indexes = [run.districts.index(district) for district in remaining]
    if "districts" in payload:
        payload["districts"] = remaining
    for key in ("base_links", "base_links_for_player_1", "base_links_for_player_2"):
        if key in payload:
            payload[key] = [payload[key][index] for index in indexes]
    return HelperSequentialJobCreateRequest.model_validate(
        {"type": request.job_type, "requested_by": actor_id, "payload": payload},
    )


async def _continuation_options(
        ctx: ipy.BaseContext, run: HelperSequentialRun,
) -> tuple[Match, HelperSequentialRun, list[str]] | None:
    latest = await latest_run(run.match_id, run.side)
    if latest is None or run.id != latest.id:
        await ctx.send(f"{ERROR_EMOJI} A newer Helper run exists for this match. "
                       "Use `/match helper recover`.", ephemeral=True)
        return None
    match = await Match.get(run.match_id)
    if stale := await stale_reason(run, match):
        await ctx.send(f"{ERROR_EMOJI} {stale}", ephemeral=True)
        return None
    if is_match_finished(match):
        await ctx.send(f"{ERROR_EMOJI} This match has already finished.", ephemeral=True)
        return None
    if isinstance(match.final_replay_run_id, PydanticObjectId):
        await ctx.send(f"{ERROR_EMOJI} Wait for the district replay to finish.", ephemeral=True)
        return None
    current_clan = match.host_clans[_start_side(run)]
    if not current_clan:
        subject, command = (("This match", "/match autostart") if run.side == LIVE_SIDE
                            else ("This side", "/match war autostart"))
        paused = any(side in match.final_scouting_paused_sides for side in result_sides(run.side))
        reason = "was aborted" if paused else "has no active host"
        await ctx.send(f"{ERROR_EMOJI} {subject} {reason}. Start it again with `{command}`.",
                       ephemeral=True)
        return None
    if current_clan != run.host_clan_tag:
        names = await resources.resource_clan_names({run.host_clan_tag, current_clan})
        await ctx.send(f"{ERROR_EMOJI} This Helper run was hosted in **{names[run.host_clan_tag]}**. "
                       f"The current host clan is **{names[current_clan]}**.", ephemeral=True)
        return None
    history = [earlier for earlier in await match_runs(match.id, run.side)
               if (earlier.final_contestant_revision, earlier.final_attempt)
               == (run.final_contestant_revision, run.final_attempt)]
    remaining = remaining_districts(match, history)
    if not remaining:
        await ctx.send(f"{SUCCESS_EMOJI} Every district was played. Enter any missing scores with **Submit Result**.",
                       ephemeral=True)
        return None
    original = original_run(run, history)
    if not set(remaining).issubset(original.districts):
        await ctx.send(f"{ERROR_EMOJI} The saved Helper layouts are missing for some districts.", ephemeral=True)
        return None
    return match, original, remaining


async def continue_run(
        bot: ipy.Client, ctx: ipy.BaseContext, run: HelperSequentialRun, start: str | None = None,
) -> None:
    async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
        options = await _continuation_options(ctx, run)
        if options is None:
            return
        match, original, remaining = options
        if start is not None:
            if start not in remaining:
                await ctx.send(f"{ERROR_EMOJI} That district is no longer pending. Open **Continue** again.",
                               ephemeral=True)
                return
            remaining = remaining[remaining.index(start):]
        actor_id = str(ctx.author.id)
        await launch_run(bot, ctx, LaunchRequest(
            match=match, side=run.side, player_tags=run.player_tags,
            request=continuation_request(original, remaining, actor_id), districts=remaining,
            start_message_id=run.start_message_id, previous_run_id=run.id,
        ))


async def _pick_start_district(bot: ipy.Client, ctx: ipy.ComponentContext, districts: list[str]) -> str | None:
    selected = await _pick_districts(bot, ctx, [districts[0]], lambda chosen: ui.continuation_picker(districts, chosen[0]))
    return selected[0] if selected else None


async def _pick_districts(
        bot: ipy.Client, ctx: ipy.ComponentContext, selected: list[str],
        picker: Callable[[list[str]], tuple[str, list[ipy.ActionRow]]],
) -> list[str] | None:
    text, rows = picker(selected)
    message = await ctx.send(text, components=rows, ephemeral=True)

    send_selection, selections = anyio.create_memory_object_stream[tuple[ipy.ComponentContext, anyio.Event]](float("inf"))

    async def acknowledge_selection(used: ipy.ComponentContext, acknowledged: anyio.Event) -> None:
        await used.defer(edit_origin=True)
        acknowledged.set()

    async def update_selection() -> None:
        async with selections:
            async for used, acknowledged in selections:
                await acknowledged.wait()
                content, components = picker(used.values)
                await used.edit_origin(content=content, components=components)

    async with anyio.create_task_group() as tasks:
        def is_confirmation(event: ipy.events.Component) -> bool:
            nonlocal selected
            used = event.ctx
            if used.author.id != ctx.author.id:
                return False
            if used.custom_id == ui.CONTINUE_CONFIRM:
                return bool(selected)
            selected = used.values
            acknowledged = anyio.Event()
            tasks.start_soon(acknowledge_selection, used, acknowledged)
            send_selection.send_nowait((used, acknowledged))
            return False

        tasks.start_soon(update_selection)
        try:
            confirmed = (await bot.wait_for_component(
                messages=int(message.id), components=rows, check=is_confirmation, timeout=PROMPT_TIMEOUT_SECONDS,
            )).ctx
        except TimeoutError:
            confirmed = None
        else:
            await confirmed.defer(edit_origin=True)
        finally:
            send_selection.close()
    if confirmed is None:
        await ctx.edit(message, components=[])
        return None
    await confirmed.edit_origin(components=[])
    return selected


async def continue_from_button(bot: ipy.Client, ctx: ipy.ComponentContext, run_id: PydanticObjectId) -> None:
    await ctx.defer(ephemeral=True)
    run = await HelperSequentialRun.get(run_id)
    if run is None or run.open:
        await ctx.send(f"{ERROR_EMOJI} This Helper run cannot be continued.", ephemeral=True)
        return
    if str(ctx.author.id) != run.requested_by and not is_staff(ctx.author):
        await ctx.send(f"{ERROR_EMOJI} Only the requester or staff can continue this Helper run.", ephemeral=True)
        return
    async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
        options = await _continuation_options(ctx, run)
    if options is not None and (start := await _pick_start_district(bot, ctx, options[2])) is not None:
        await continue_run(bot, ctx, run, start)


async def replay_from_button(bot: ipy.Client, ctx: ipy.ComponentContext, run_id: PydanticObjectId) -> None:
    await ctx.defer(ephemeral=True)
    if not any(ctx.author.has_role(role) for role in LVL1_ROLES):
        await ctx.send(f"{ERROR_EMOJI} Only managers can replay districts.", ephemeral=True)
        return
    run = await HelperSequentialRun.get(run_id)
    if (run is None or run.open or ctx.channel_id != int(run.thread_id)
            or ctx.message_id != run.progress_message_id):
        await ctx.send(f"{ERROR_EMOJI} Use **Replay** on the current Helper progress message.", ephemeral=True)
        return
    match = await Match.get(run.match_id)
    stale = await stale_reason(run, match)
    latest = await latest_run(run.match_id, run.side)
    if stale or latest is None or latest.id != run.id:
        await ctx.send(f"{ERROR_EMOJI} {stale or 'A newer Helper run exists. Use its progress message.'}", ephemeral=True)
        return
    if match.final_replay_run_id or match.host_clans[_start_side(run)] != run.host_clan_tag:
        await ctx.send(f"{ERROR_EMOJI} Replay is unavailable while hosting is active or the host clan has changed.",
                       ephemeral=True)
        return
    history = await match_runs(match.id, run.side)
    original = original_run(run, history)
    baseline = replay_scores(match, run.side)
    scores = {district: baseline[district] for district in FINAL_DISTRICTS if district in original.districts}
    if not scores:
        await ctx.send(f"{ERROR_EMOJI} The original layouts are unavailable for Replay.", ephemeral=True)
        return
    selected = await _pick_districts(bot, ctx, [], lambda chosen: ui.replay_picker(scores, chosen))
    if not selected:
        return
    districts = [district for district in scores if district in selected]
    if len(districts) != len(selected):
        await ctx.send(f"{ERROR_EMOJI} The selected districts are unavailable. Open **Replay** again.", ephemeral=True)
        return

    async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
        reservations = []
        try:
            for side in (0, 1):
                reservation = await resources.try_reserve_resource_match_side(str(match.token), side)
                if reservation is None:
                    await ctx.send(f"{ERROR_EMOJI} Another hosting action is in progress.", ephemeral=True)
                    return
                reservations.append(reservation)
            await launch_run(bot, ctx, LaunchRequest(
                match=match, side=run.side, player_tags=run.player_tags, districts=districts,
                request=continuation_request(original, districts, str(ctx.author.id)),
                start_message_id=run.start_message_id, previous_run_id=run.id,
                replay_source=original, replay_baseline=baseline,
            ))
            latest = await latest_run(run.match_id, run.side)
            if latest is not None and latest.id != run.id:
                await refresh_progress(bot, run, retire=True)
        finally:
            for reservation in reversed(reservations):
                reservation.release()


def players_match(run: HelperSequentialRun, job: HelperSequentialJob) -> bool:
    def normalized(tag: str) -> str:
        return tag.upper().lstrip("#")
    return job.result is None or [normalized(player.tag) for player in job.result.match.players] == [
        normalized(tag) for tag in run.player_tags
    ]


async def adopt_uncertain(bot: ipy.Client, ctx: ipy.SlashContext, run: HelperSequentialRun) -> None:
    replay = run.replay_baseline is not None
    if replay and not any(ctx.author.has_role(role) for role in LVL1_ROLES):
        await ctx.send(f"{ERROR_EMOJI} Only managers can retry a replay.", ephemeral=True)
        return
    prompt = (f"{IMPORTANT_EMOJI} Helper did not confirm this replay. A retry can start these districts again."
              if replay else f"{IMPORTANT_EMOJI} Helper never confirmed this job. **Check and adopt** asks Helper "
              "again; if Helper already has the job it is adopted, otherwise Helper starts it now. "
              "**Discard** closes this record only.")
    choice = await choose(
        bot, ctx, prompt,
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Try Again" if replay else "Check and adopt",
                   custom_id="seq_adopt"),
        (ipy.Button(style=ipy.ButtonStyle.GRAY, label="Back", custom_id="seq_adopt_back") if replay else
         ipy.Button(style=ipy.ButtonStyle.DANGER, label="Discard", custom_id="seq_discard")),
    )
    if choice is None or choice == "seq_adopt_back":
        return
    async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
        current = await HelperSequentialRun.get(run.id)
        if current is None or not current.open or current.state not in ("CREATING", "UNCERTAIN"):
            await ctx.send(f"{ERROR_EMOJI} The Helper run changed. Check `/match helper status`.", ephemeral=True)
            return
        if current.replay_baseline is not None:
            try:
                await validate_replay_retry(current)
            except FinalError as error:
                await close_run(current, str(error), str(ctx.author.id))
                await refresh_progress(bot, await HelperSequentialRun.get(run.id))
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
        await _apply_recovery_choice(bot, ctx, current, choice)


async def _apply_recovery_choice(bot: ipy.Client, ctx: ipy.SlashContext, run: HelperSequentialRun, choice: str) -> None:
    actor_id = str(ctx.author.id)
    if choice == "seq_discard":
        await close_run(run, "Discarded by staff. Helper may still be hosting; check in game.", actor_id)
        await refresh_progress(bot, await HelperSequentialRun.get(run.id))
        await ctx.send(f"{SUCCESS_EMOJI} The unconfirmed Helper run was discarded.", ephemeral=True)
        return
    request = _saved_create_request(run)
    async with helper_session() as session:
        outcome = await helper_client().create(session, request)
        if run.replay_baseline is not None and (
                isinstance(outcome, HelperJobCreateConflict) or isinstance(outcome, HelperJobCreateRejected)
                and (outcome.error or "").startswith((HELPER_SEQUENTIAL_RESERVATION_PREFIX, "clan_has_open_job:"))):
            await ctx.send(f"{IMPORTANT_EMOJI} Helper already has a hosting job. Wait for it to finish before retrying.",
                           ephemeral=True)
            return
        if (isinstance(outcome, HelperJobCreateRejected)
                and (outcome.error or "").startswith(HELPER_SEQUENTIAL_RESERVATION_PREFIX)):
            job_id = outcome.error.removeprefix(HELPER_SEQUENTIAL_RESERVATION_PREFIX)
            existing = await helper_client().read(session, job_id)
            if isinstance(existing, HelperSequentialJob) and players_match(run, existing):
                existing_type = existing.job_type
                if (existing_type == "host_sequential_live_match" and existing.result is not None
                        and existing.result.match.base_source == "stored"):
                    existing_type = "host_sequential_live_match_on_stored"
                if existing_type == request.job_type:
                    outcome = HelperJobCreated(id=job_id)
    await record_event(run, "adopt_checked", type(outcome).__name__, actor_id)
    await apply_create_outcome(bot, ctx, run, outcome)


async def recover(bot: ipy.Client, ctx: ipy.SlashContext, match: Match, side: int) -> None:
    run = await latest_run(match.id, side)
    if run is None:
        await ctx.send(f"{ERROR_EMOJI} There is no Helper run to recover here. Use autostart to start one.",
                       ephemeral=True)
        return
    actor_id = str(ctx.author.id)
    await record_event(run, "recover", actor_id=actor_id)
    if run.open and run.state in ("CREATING", "UNCERTAIN"):
        await adopt_uncertain(bot, ctx, run)
        return
    if run.open:
        if not is_watching(run.id):
            run = await sync_run(bot, run.id)
            if run.open:
                start_watcher(bot, run.id)
        if await progress_message(bot, run) is None:
            await publish_progress(bot, run, ctx.channel)
        state = "is being tracked" if run.open else f"has ended: {run.close_reason}"
        await ctx.send(f"{SUCCESS_EMOJI} The Helper run {state}", ephemeral=True)
        return
    if sides_stored(match, side):
        if not await stale_reason(run, match):
            if await progress_message(bot, run) is None:
                await publish_progress(bot, run, ctx.channel)
            else:
                await refresh_progress(bot, run)
        await ctx.send(f"{SUCCESS_EMOJI} All scores for this side are stored. Check the Helper progress message.",
                       ephemeral=True)
        return
    await continue_run(bot, ctx, run)


async def run_checks(
        bot: ipy.Client, run: HelperSequentialRun, match: Match, clan_names: dict[str, str],
) -> tuple[list[str], str | None]:
    stale = await stale_reason(run, match)
    checks = [ui.check_line(stale is None, stale or "The run matches the current Final pairing.")]
    tag = match.host_clans[_start_side(run)]
    name = clan_names[tag] if tag else "None assigned"
    checks.append(ui.check_line(tag == run.host_clan_tag, f"Match host clan: {name}"))
    message = await progress_message(bot, run)
    checks.append(ui.check_line(message is not None, "Progress message exists." if message
                                else "Progress message is missing; `/match helper recover` posts a new one."))
    if run.open and run.state == "ACTIVE":
        checks.append(ui.check_line(is_watching(run.id), "A watcher is running in this bot process."
                                    if is_watching(run.id) else "No watcher in this bot process."))
    return checks, message.jump_url if message else None


async def status(bot: ipy.Client, ctx: ipy.SlashContext, match: Match, side: int | None, *, export: bool,
                 hidden: bool) -> None:
    run = await latest_run(match.id, side)
    if run is None:
        await ctx.send(f"{ERROR_EMOJI} This match has no Helper sequential run.", ephemeral=hidden)
        return
    live: HelperSequentialJob | str = "No Helper job yet."
    raw_live = None
    if run.job_id:
        async with helper_session() as session:
            outcome = await helper_client().read(session, run.job_id)
            if export:
                try:
                    raw_live = await helper_client().read_raw(session, run.job_id)
                except (aiohttp.ClientError, TimeoutError) as error:
                    raw_live = f"Helper could not be reached: {error!r}"
        match outcome:
            case HelperSequentialJob():
                live = outcome
            case HelperJobNotFound():
                live = "⚠️ Helper no longer has this job."
            case HelperTrackingFailure(reason=reason):
                live = f"⚠️ Helper unreachable: {reason}"
    clan_tags = {run.host_clan_tag}
    if current_clan := match.host_clans[_start_side(run)]:
        clan_tags.add(current_clan)
    names = await resources.resource_clan_names(clan_tags)
    checks, link = await run_checks(bot, run, match, names)
    embed = ui.status_embed(run, match, checks, live, link, names[run.host_clan_tag])
    if not export:
        await ctx.send(embed=embed, ephemeral=hidden)
        return
    runs = await HelperSequentialRun.find({"match_id": match.id}).sort("created_at").to_list()
    dump = {"runs": [item.model_dump(mode="json") for item in runs], "live_helper_response": raw_live}
    file = ipy.File(BytesIO(json.dumps(dump, indent=2).encode()), file_name=f"helper-run-{run.id}.json")
    await ctx.send(embed=embed, file=file, ephemeral=hidden)


async def list_page(state: str, clan: str | None, page: int) -> tuple[ipy.Embed, list[ipy.ActionRow], int]:
    query: dict = {} if clan is None else {"host_clan_tag": clan}
    match state:
        case "open" | "attention":
            query["open"] = True
        case "uncertain":
            query |= {"open": True, "state": {"$in": ["CREATING", "UNCERTAIN"]}}
        case "failed":
            query |= {"open": False, "job_status": {"$in": ["failed", "cancelled"]}}
    runs = await HelperSequentialRun.find(query).sort("-created_at").to_list()
    if state == "attention":
        now = time.time()
        runs = [run for run in runs if run.state != "ACTIVE" or not is_tracked(run, now)]
    total_pages = max(1, -(-len(runs) // LIST_PAGE_SIZE))
    page = min(max(page, 0), total_pages - 1)
    shown = runs[page * LIST_PAGE_SIZE:(page + 1) * LIST_PAGE_SIZE]
    names = await resources.resource_clan_names({run.host_clan_tag for run in shown})
    return ui.list_embed(shown, names), [ui.list_components(page, total_pages, state, clan)], total_pages


async def close_runs_for_thread(thread_id: str) -> None:
    for run in await HelperSequentialRun.find({"thread_id": thread_id, "open": True}).to_list():
        async with _RUN_LOCKS.setdefault(run.id, anyio.Lock()):
            current = await HelperSequentialRun.get(run.id)
            if current is None or not current.open:
                continue
            if current.job_id:
                try:
                    async with helper_session() as session:
                        await helper_client().cancel(session, current.job_id)
                except (aiohttp.ClientError, TimeoutError):
                    logging.exception("Helper job %s could not be cancelled after its thread was deleted", current.job_id)
                    await record_event(current, "cancel_failed", "thread deleted")
            await close_run(current, "The match thread was deleted.")

# noqa: SIZE_OK - start, watch, cancel, continue and recover share one run lifecycle
import asyncio
import json
import logging
import time
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
from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL2_ROLES
from elo_bot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.core.match_results import is_match_finished
from elo_bot.db import Match, Player
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun, SequentialRunEvent
from elo_bot.features.base_store.live import live_pair_ready, live_stores
from elo_bot.features.base_store.publication import BaseStoreMessageConflict
from elo_bot.features.hosting.live.helper_live import AutomaticLiveHost, parse_paired_live_bases
from elo_bot.features.hosting.sequential import ui
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
    record_event,
    remaining_districts,
    result_sides,
    run_result,
    transition,
)
from elo_bot.features.hosting.sequential.scores import store_verified_scores
from elo_bot.features.hosting.war.helper_host import AutomaticWarHost
from elo_bot.features.hosting.war.helper_slot import get_available_helper_slot
from elo_bot.features.hosting.war.result_store import complete_stored_results
from elo_bot.features.matches import attack_bases, resources
from elo_bot.features.matches.timing import hosting_started
from elo_bot.integrations.helper_api import (
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
    HelperSequentialLivePayload,
    HelperSequentialResult,
    HelperSequentialWarPayload,
    HelperTrackingFailure,
)

TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
RESERVED_PREFIX = "reserved_by_sequential_job:"
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


async def host_war_sequentially(bot: ipy.Client, ctx: ipy.SlashContext, host: AutomaticWarHost) -> None:
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
        links = [base.link if base else None for base in match_doc.participants[opponent].bases]
        if len(links) != len(FINAL_DISTRICTS) or not all(links):
            await ctx.send(f"{ERROR_EMOJI} Helper needs an opponent base for all seven districts.", ephemeral=True)
            return
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
    started_message = await war_start.start_war(bot, ctx, war_start.WarStart(
        match=match_doc, player=host.player, host=host.host, clan=host.clan, coc_client=bot.coc_client,
        reply=war_start.SelfHostedWarStartReply(), scout=host.scout, clan_reservation=host.clan_reservation,
        side_reservation=host.side_reservation, stored_start=host.stored_start,
    ))
    if not started_message:
        await ctx.send(f"{ERROR_EMOJI} **{host.clan.name}** is currently occupied.\n\n"
                       f"{await resources.available_resource_clans_text()}", ephemeral=True)
        return
    await launch_run(bot, ctx, LaunchRequest(
        match=match_doc, side=host.player, player_tags=[player_doc.tag], request=request,
        districts=list(FINAL_DISTRICTS), start_message_id=int(started_message.id),
    ))


async def host_live_sequentially(bot: ipy.Client, ctx: ipy.SlashContext, host: AutomaticLiveHost) -> None:
    if refusal := await clan_refusal(host.clan.tag):
        await ctx.send(refusal, ephemeral=True)
        return
    reservation_outcome = await live_start.reserve_live_start(str(host.match.token), host.clan.tag)
    match reservation_outcome:
        case live_reservations.LiveStartReservations() as reservations:
            pass
        case live_reservations.LiveReservationsBusy():
            await ctx.send(f"{ERROR_EMOJI} This match is already being started.\n\n"
                           f"{await resources.available_resource_clans_text()}", ephemeral=True)
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
            payload = HelperSequentialLivePayload(
                host_clan_tag=host.clan.tag, base_source="stored", player_1_tag=host.player1.tag,
                player_2_tag=host.player2.tag, districts=list(FINAL_DISTRICTS), remove_bases_after=False,
            )
        else:
            paired = parse_paired_live_bases(match_doc)
            if paired is None:
                await ctx.send(f"{ERROR_EMOJI} A complete, distinct pair of bases is required for Helper hosting.",
                               ephemeral=True)
                return
            payload = HelperSequentialLivePayload(
                host_clan_tag=host.clan.tag, base_source="links", player_1_tag=host.player1.tag,
                player_2_tag=host.player2.tag, base_links_for_player_1=paired.for_player1,
                base_links_for_player_2=paired.for_player2,
            )
        start_outcome = await live_start.start_live_match(bot, ctx, live_start.LiveStart(
            match=match_doc, host=host.host, clan=host.clan, player1=host.player1, player2=host.player2,
            player1_elo=host.player1_elo, player2_elo=host.player2_elo, scout=host.scout,
            reply=live_start.AutomaticLiveStartReply(), reservations=reservations,
            stored_reservation_id=host.stored_reservation_id,
        ))
    finally:
        reservations.release()
    match start_outcome:
        case live_start.LiveStartSucceeded(message=started_message):
            pass
        case live_start.LiveStartBusy():
            await ctx.send(f"{ERROR_EMOJI} This match is already being started.\n\n"
                           f"{await resources.available_resource_clans_text()}", ephemeral=True)
            return
        case live_start.LiveStartClanUnavailable():
            await ctx.send(f"{ERROR_EMOJI} **{host.clan.name}** is currently occupied.\n\n"
                           f"{await resources.available_resource_clans_text()}", ephemeral=True)
            return
        case unreachable:
            assert_never(unreachable)
    request = HelperSequentialJobCreateRequest(
        job_type="host_sequential_live_match", requested_by=str(ctx.author.id), payload=payload,
    )
    await launch_run(bot, ctx, LaunchRequest(
        match=match_doc, side=LIVE_SIDE, player_tags=[host.player1.tag, host.player2.tag], request=request,
        districts=list(FINAL_DISTRICTS), start_message_id=int(started_message.id),
    ))


async def launch_run(bot: ipy.Client, ctx: ipy.BaseContext, launch: LaunchRequest) -> None:
    match = launch.match
    actor_id = str(ctx.author.id)
    run = HelperSequentialRun(
        match_id=match.id, match_token=str(match.token), thread_id=str(match.thread), final_id=match.final_id,
        final_node_id=match.final_node_id, final_contestant_revision=match.final_contestant_revision,
        final_attempt=match.final_attempt, side=launch.side, host_clan_tag=launch.request.payload.host_clan_tag,
        player_tags=launch.player_tags, districts=launch.districts,
        create_request=launch.request.model_dump(by_alias=True, exclude_none=True),
        requested_by=actor_id, previous_run_id=launch.previous_run_id, start_message_id=launch.start_message_id,
        state="CREATING", created_at=time.time(),
        events=[SequentialRunEvent(at=time.time(), kind="created", detail=", ".join(launch.districts),
                                   actor_id=actor_id)],
    )
    try:
        await run.insert()
    except DuplicateKeyError:
        # The open run owns this match start, so it must not be rolled back here.
        await ctx.send(f"{ERROR_EMOJI} Another Helper run is already open for this side. "
                       "Use `/match helper status`.", ephemeral=True)
        return
    async with helper_session() as session:
        outcome = await helper_client().create(session, launch.request)
    await apply_create_outcome(bot, ctx, run, outcome)


async def apply_create_outcome(
        bot: ipy.Client, ctx: ipy.BaseContext, run: HelperSequentialRun, outcome: HelperJobCreateOutcome,
) -> None:
    actor_id = str(ctx.author.id)
    match outcome:
        case HelperJobCreated(id=job_id):
            await transition(run, {"state": "ACTIVE", "job_id": job_id, "job_status": "queued"},
                             "job_created", job_id, actor_id)
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
            await ctx.send(f"{ERROR_EMOJI} {reason}{notice}", ephemeral=True)
        case HelperJobCreateUnavailable() | HelperJobCreateAcceptedInvalid():
            await transition(run, {"state": "UNCERTAIN"}, "create_uncertain", actor_id=actor_id)
            await publish_progress(bot, await HelperSequentialRun.get(run.id), ctx.channel)
            await ctx.send(f"{IMPORTANT_EMOJI} Helper's response could not be confirmed, so the job may be running. "
                           "Run `/match helper recover` to check and adopt it.", ephemeral=True)
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


def progress_components(run: HelperSequentialRun, match: Match | None) -> list[ipy.ActionRow]:
    can_continue = (not run.open and run.job_id is not None and match is not None
                    and not is_match_finished(match) and not sides_stored(match, run.side))
    buttons = ui.progress_components(
        run, can_continue=can_continue, can_submit=match is not None and complete_stored_results(match),
        match_token=run.match_token,
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


async def refresh_progress(bot: ipy.Client, run: HelperSequentialRun) -> None:
    message = await progress_message(bot, run)
    if message is None:
        return
    match = await Match.get(run.match_id)
    try:
        await message.edit(embed=ui.progress_embed(run, match), components=progress_components(run, match))
    except ipy.errors.HTTPException:
        logging.exception("Unable to publish Helper sequential progress for run %s", run.id)


def stale_reason(run: HelperSequentialRun, match: Match | None) -> str | None:
    if match is None:
        return "The match no longer exists."
    if match.final_superseded or (match.final_contestant_revision, match.final_attempt) != (
            run.final_contestant_revision, run.final_attempt):
        return "The Final pairing changed."
    if match.final_result_id is not None:
        return "The Final result was submitted."
    return None


def completed_codes(snapshot: dict | None) -> set[str]:
    if not snapshot:
        return set()
    return {
        code for record in HelperSequentialResult.model_validate(snapshot).completion.rounds
        if record.players and all(player.completed for player in record.players.values())
        for code in record.districts.values()
    }


async def sync_run(bot: ipy.Client, run_id: PydanticObjectId) -> HelperSequentialRun | None:
    async with _RUN_LOCKS.setdefault(run_id, anyio.Lock()):
        run = await HelperSequentialRun.get(run_id)
        if run is None or not run.open or run.state != "ACTIVE":
            return run
        match = await Match.get(run.match_id)
        stale = stale_reason(run, match)
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
        run = await HelperSequentialRun.get(run_id)
        await refresh_progress(bot, run)
        return run


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
            snapshot = job.result.model_dump(mode="json") if job.result else run.snapshot
            await collection.update_one({"_id": run.id}, {"$set": {
                "snapshot": snapshot, "job_status": job.status, "error": job.error, "last_polled_at": now,
                "poll_failures": 0, "last_poll_error": None,
            }})
            if job.status != run.job_status:
                await record_event(run, "status", job.status)
            for code in sorted(completed_codes(snapshot) - completed_codes(run.snapshot)):
                await record_event(run, "district_played", code)
            if job.result is not None and match is not None and stale is None:
                for stored in await store_verified_scores(run, job.result):
                    await record_event(run, "score_stored", stored)
            if job.status in TERMINAL_JOB_STATUSES:
                detail = f": {job.error}" if job.error else ""
                if await close_run(run, stale or f"Helper job {job.status}{detail}."):
                    await finish_stored_sides(bot, run)
        case unreachable:
            assert_never(unreachable)


async def finish_stored_sides(bot: ipy.Client, run: HelperSequentialRun) -> None:
    match = await Match.get(run.match_id)
    match_commands = bot.get_ext("MatchCommands")
    if match is None or match_commands is None:
        return
    for side in result_sides(run.side):
        if all(match.participants[side].result):
            await match_commands._finish_stored_result_side(match, side)


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
        f"{IMPORTANT_EMOJI} Cancel this Helper run? If Helper has not posted a challenge yet, the match start is "
        "rolled back. Otherwise the match and its stored scores are kept, and you can continue the remaining "
        "districts later.",
        ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel Run", emoji=ui.CROSS_EMOJI,
                   custom_id="seq_cancel_confirm"),
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Back", custom_id="seq_cancel_back"),
    )
    if choice != "seq_cancel_confirm":
        return
    try:
        async with helper_session() as session:
            await helper_client().cancel(session, run.job_id)
    except (aiohttp.ClientError, TimeoutError):
        await ctx.send(f"{ERROR_EMOJI} Helper could not cancel the job. Please try again.", ephemeral=True)
        return
    await record_event(run, "cancel_requested", actor_id=str(ctx.author.id))
    run = await sync_run(bot, run_id)
    if run is None or run.open:
        await ctx.send(f"{IMPORTANT_EMOJI} Helper accepted the cancellation, but the run is still closing. "
                       "Check it with `/match helper status`.", ephemeral=True)
        return
    result = run_result(run)
    nothing_posted = result is None or not any(
        player.posted for record in result.completion.rounds for player in record.players.values()
    )
    notice = await rollback_first_run(ctx, run) if nothing_posted else ""
    await ctx.send(f"{SUCCESS_EMOJI} The Helper run was cancelled.{notice}", ephemeral=True)


def continuation_request(run: HelperSequentialRun, remaining: list[str], actor_id: str) -> HelperSequentialJobCreateRequest:
    payload = dict(run.create_request["payload"])
    indexes = [run.districts.index(district) for district in remaining]
    if payload["base_source"] == "stored":
        payload["districts"] = remaining
    for key in ("base_links", "base_links_for_player_1", "base_links_for_player_2"):
        if key in payload:
            payload[key] = [payload[key][index] for index in indexes]
    return HelperSequentialJobCreateRequest.model_validate(
        {"type": run.create_request["type"], "requested_by": actor_id, "payload": payload},
    )


async def continue_run(bot: ipy.Client, ctx: ipy.BaseContext, run: HelperSequentialRun) -> None:
    match = await Match.get(run.match_id)
    if stale := stale_reason(run, match):
        await ctx.send(f"{ERROR_EMOJI} {stale}", ephemeral=True)
        return
    started_side = 0 if run.side == LIVE_SIDE else run.side
    if is_match_finished(match) or match.host_clans[started_side] != run.host_clan_tag:
        await ctx.send(f"{ERROR_EMOJI} This match is no longer hosted in `{run.host_clan_tag}`.", ephemeral=True)
        return
    remaining = remaining_districts(match, await match_runs(match.id, run.side))
    if not remaining:
        await ctx.send(f"{SUCCESS_EMOJI} Every district was played. Enter any missing scores with **Submit Result**.",
                       ephemeral=True)
        return
    actor_id = str(ctx.author.id)
    await launch_run(bot, ctx, LaunchRequest(
        match=match, side=run.side, player_tags=run.player_tags,
        request=continuation_request(run, remaining, actor_id), districts=remaining,
        start_message_id=run.start_message_id, previous_run_id=run.id,
    ))


async def continue_from_button(bot: ipy.Client, ctx: ipy.ComponentContext, run_id: PydanticObjectId) -> None:
    await ctx.defer(ephemeral=True)
    run = await HelperSequentialRun.get(run_id)
    if run is None or run.open:
        await ctx.send(f"{ERROR_EMOJI} This Helper run cannot be continued.", ephemeral=True)
        return
    if str(ctx.author.id) != run.requested_by and not is_staff(ctx.author):
        await ctx.send(f"{ERROR_EMOJI} Only the requester or staff can continue this Helper run.", ephemeral=True)
        return
    if run.id != (await latest_run(run.match_id, run.side)).id:
        await ctx.send(f"{ERROR_EMOJI} A newer Helper run exists for this match. "
                       "Use `/match helper recover`.", ephemeral=True)
        return
    await continue_run(bot, ctx, run)


def players_match(run: HelperSequentialRun, job: HelperSequentialJob) -> bool:
    def normalized(tag: str) -> str:
        return tag.upper().lstrip("#")
    return job.result is None or [normalized(player.tag) for player in job.result.match.players] == [
        normalized(tag) for tag in run.player_tags
    ]


async def adopt_uncertain(bot: ipy.Client, ctx: ipy.SlashContext, run: HelperSequentialRun) -> None:
    choice = await choose(
        bot, ctx,
        f"{IMPORTANT_EMOJI} Helper never confirmed this job. **Check and adopt** asks Helper again; if Helper "
        "already has the job it is adopted, otherwise Helper starts it now. **Discard** closes this record only.",
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Check and adopt", custom_id="seq_adopt"),
        ipy.Button(style=ipy.ButtonStyle.DANGER, label="Discard", custom_id="seq_discard"),
    )
    if choice is None:
        return
    actor_id = str(ctx.author.id)
    if choice == "seq_discard":
        await close_run(run, "Discarded by staff. Helper may still be hosting; check in game.", actor_id)
        await refresh_progress(bot, await HelperSequentialRun.get(run.id))
        await ctx.send(f"{SUCCESS_EMOJI} The unconfirmed Helper run was discarded.", ephemeral=True)
        return
    request = HelperSequentialJobCreateRequest.model_validate(run.create_request)
    async with helper_session() as session:
        outcome = await helper_client().create(session, request)
        if isinstance(outcome, HelperJobCreateRejected) and (outcome.error or "").startswith(RESERVED_PREFIX):
            job_id = outcome.error.removeprefix(RESERVED_PREFIX)
            existing = await helper_client().read(session, job_id)
            if (isinstance(existing, HelperSequentialJob) and existing.job_type == request.job_type
                    and players_match(run, existing)):
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
        await ctx.send(f"{SUCCESS_EMOJI} All scores for this side are stored. Use **Submit Result** to record the "
                       "match.", ephemeral=True)
        return
    await continue_run(bot, ctx, run)


async def run_checks(bot: ipy.Client, run: HelperSequentialRun, match: Match | None) -> tuple[list[str], str | None]:
    stale = stale_reason(run, match)
    checks = [ui.check_line(stale is None, stale or "The run matches the current Final pairing.")]
    if match is not None:
        started_side = 0 if run.side == LIVE_SIDE else run.side
        checks.append(ui.check_line(match.host_clans[started_side] in (run.host_clan_tag, None),
                                    f"Match host clan: `{match.host_clans[started_side] or '-'}`"))
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
    checks, link = await run_checks(bot, run, match)
    embed = ui.status_embed(run, match, checks, live, link)
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
    return ui.list_embed(shown), [ui.list_components(page, total_pages, state, clan)], total_pages


async def close_runs_for_thread(thread_id: str) -> None:
    for run in await HelperSequentialRun.find({"thread_id": thread_id, "open": True}).to_list():
        if run.job_id:
            try:
                async with helper_session() as session:
                    await helper_client().cancel(session, run.job_id)
            except (aiohttp.ClientError, TimeoutError):
                logging.exception("Helper job %s could not be cancelled after its thread was deleted", run.job_id)
                await record_event(run, "cancel_failed", "thread deleted")
        await close_run(run, "The match thread was deleted.")

from typing import Protocol, assert_never

import aiohttp
import coc
import interactions as ipy

from elobot.integrations.helper_api import (
    HelperInviteSpectatorPayload,
    HelperJob,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateConflict,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperTrackingFailure,
    helper_job_notification,
)
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import MATCH_CHANNEL
from elobot.core.finals import is_final_match
from elobot.core.match_results import is_match_finished
from elobot.db import Match, Player


class SpectateRuntime(Protocol):
    helper_api_base_url: str
    helper_api_key: str


async def spectate_match(
    runtime: SpectateRuntime, ctx: ipy.ComponentContext, war_side: int | None = None,
) -> None:
    await ctx.defer(ephemeral=True)
    caller_id = str(ctx.author.id)
    player = await Player.find_one({"user_id": caller_id})
    if player is None:
        await ctx.send(f"{ERROR_EMOJI} You must be registered to spectate a match.", ephemeral=True)
        return

    try:
        account = await ctx.bot.coc_client.get_player(player.tag)
    except (coc.errors.HTTPException, TimeoutError):
        await ctx.send(
            f"{ERROR_EMOJI} Could not verify your registered account's Town Hall level. Please try again later.",
            ephemeral=True,
        )
        return
    if account.town_hall > 5:
        await ctx.send(
            f"{ERROR_EMOJI} Your registered Elo account must be TH5 or below to spectate.", ephemeral=True,
        )
        return

    fields = ctx.custom_id.split("|")
    token, started_at = fields[1], int(fields[-1])
    side = 0 if war_side is None else war_side
    match_doc = await Match.find_one({"token": token})
    if (
        match_doc is None
        or match_doc.is_war != (war_side is not None)
        or (war_side is None and is_match_finished(match_doc))
        or match_doc.times[side] != started_at
        or not match_doc.host_clans[side]
        or (war_side is None and (
            int(ctx.channel_id) != (match_doc.routing_channel_id if is_final_match(match_doc) else MATCH_CHANNEL)
            or match_doc.msg != str(ctx.message.id)
        ))
        or (war_side is not None and (
            str(match_doc.thread) != str(ctx.channel_id) or match_doc.old_msgs[side] != int(ctx.message.id)
        ))
    ):
        await ctx.send(f"{ERROR_EMOJI} This match is no longer active.", ephemeral=True)
        return
    if war_side is not None:
        matches = await Match.find({
            "participants.user_id": caller_id, "host_clans": match_doc.host_clans[side],
        }).to_list()
        for active_match in matches:
            caller_side = next((
                index for index, participant in enumerate(active_match.participants[:2])
                if participant.user_id == caller_id
            ), None)
            if caller_side is None or is_match_finished(active_match, player=caller_side):
                continue
            opponent_side = 1 - caller_side
            host_side = opponent_side if active_match.is_war else 0
            if (
                active_match.times[host_side]
                and active_match.host_clans[host_side] == match_doc.host_clans[side]
                and not is_match_finished(active_match, player=opponent_side)
            ):
                opponent_id = active_match.participants[opponent_side].user_id
                await ctx.send(
                    f"{ERROR_EMOJI} You cannot spectate this clan while your opponent <@{opponent_id}> "
                    f"has attacks pending in <#{active_match.thread}>. "
                    "Finish your attacks and store your results in that match first.",
                    ephemeral=True,
                )
                return

    await invite_spectator(runtime, ctx, HelperJobCreateRequest(
        job_type="invite_spectator", requested_by=caller_id,
        payload=HelperInviteSpectatorPayload(host_clan_tag=match_doc.host_clans[side], player_tag=player.tag),
    ))


async def invite_spectator(
    runtime: SpectateRuntime, ctx: ipy.ComponentContext, request: HelperJobCreateRequest,
) -> None:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        created = await HelperJobCreator(runtime.helper_api_base_url, runtime.helper_api_key).create(session, request)
        match created:
            case HelperJobCreated(id=job_id):
                status = "queued"
            case HelperJobCreateConflict(existing_job_id=job_id, existing_job_status=status):
                pass
            case HelperJobCreateAcceptedInvalid():
                await ctx.send(
                    f"{ERROR_EMOJI} Helper accepted the request, but the outcome could not be confirmed.",
                    ephemeral=True,
                )
                return
            case HelperJobCreateRejected(error=error):
                detail = f": {error}" if error else "."
                await ctx.send(f"{ERROR_EMOJI} Helper rejected the request{detail}", ephemeral=True)
                return
            case HelperJobCreateUnavailable():
                await ctx.send(
                    f"{ERROR_EMOJI} The Helper service could not queue the spectator invite.", ephemeral=True,
                )
                return
            case unexpected:
                assert_never(unexpected)

        progress = await ctx.send(
            f"{LOADING_EMOJI} Helper spectator-invite job `{job_id}` is {status}.", ephemeral=True,
        )

        async def update_progress(job: HelperJob) -> None:
            match job.status:
                case "queued" | "running":
                    emoji = LOADING_EMOJI
                case "succeeded":
                    emoji = SUCCESS_EMOJI
                case "failed" | "cancelled":
                    emoji = ERROR_EMOJI
                case unexpected:
                    assert_never(unexpected)
            await ctx.edit(progress, content=f"{emoji} {helper_job_notification(job)}")

        outcome = await HelperJobPoller(runtime.helper_api_base_url, runtime.helper_api_key).wait(
            session, job_id, update_progress,
        )
    match outcome:
        case HelperJob():
            return
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            await ctx.edit(
                progress,
                content=(f"{ERROR_EMOJI} Could not monitor Helper job `{job_id}`: {reason} "
                         "The invitation outcome is unknown; contact a helper before retrying."),
            )
        case unexpected:
            assert_never(unexpected)

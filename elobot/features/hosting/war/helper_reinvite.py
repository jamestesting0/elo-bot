from dataclasses import dataclass
from typing import Protocol, assert_never

import aiohttp
import interactions as ipy

from elobot.integrations.helper_api import (
    HelperJob,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperKickAndReinvitePayload,
    HelperTrackingFailure,
    helper_job_notification,
)
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI


@dataclass(frozen=True, slots=True)
class HelperReinviteRequest:
    requested_by: str
    host_clan_tag: str
    player_tag: str


class HelperReinviteRuntime(Protocol):
    helper_api_base_url: str
    helper_api_key: str


async def kick_and_reinvite(
        runtime: HelperReinviteRuntime,
        ctx: ipy.SlashContext,
        request: HelperReinviteRequest,
) -> None:
    create_request = HelperJobCreateRequest(
        job_type="kick_and_reinvite",
        requested_by=request.requested_by,
        payload=HelperKickAndReinvitePayload(
            host_clan_tag=request.host_clan_tag,
            player_tag=request.player_tag,
        ),
    )
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        create_outcome = await HelperJobCreator(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).create(session, create_request)
        match create_outcome:
            case HelperJobCreated() as job:
                pass
            case HelperJobCreateAcceptedInvalid():
                await ctx.send(
                    f"{ERROR_EMOJI} Helper accepted the request, but the outcome could not be confirmed.",
                    ephemeral=True,
                )
                return
            case HelperJobCreateRejected(error=None):
                await ctx.send(f"{ERROR_EMOJI} Helper rejected the request.", ephemeral=True)
                return
            case HelperJobCreateRejected(error=error):
                await ctx.send(f"{ERROR_EMOJI} Helper rejected the request: {error}", ephemeral=True)
                return
            case HelperJobCreateUnavailable():
                await ctx.send(
                    f"{ERROR_EMOJI} The Helper service could not queue the re-invite request.",
                    ephemeral=True,
                )
                return
            case unexpected:
                assert_never(unexpected)

        progress_message = await ctx.send(
            f"{LOADING_EMOJI} Helper re-invite job `{job.id}` has been queued.",
            ephemeral=True,
        )

        async def update_progress(current_job: HelperJob) -> None:
            match current_job.status:
                case "queued" | "running":
                    emoji = LOADING_EMOJI
                case "succeeded":
                    emoji = SUCCESS_EMOJI
                case "failed" | "cancelled":
                    emoji = ERROR_EMOJI
                case unexpected:
                    assert_never(unexpected)
            await ctx.edit(
                progress_message,
                content=f"{emoji} {helper_job_notification(current_job)}",
            )

        outcome = await HelperJobPoller(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).wait(session, job.id, update_progress)

    match outcome:
        case HelperJob():
            return
        case HelperTrackingFailure():
            await ctx.edit(
                progress_message,
                content=f"{ERROR_EMOJI} {helper_job_notification(outcome)}",
            )
        case unexpected:
            assert_never(unexpected)

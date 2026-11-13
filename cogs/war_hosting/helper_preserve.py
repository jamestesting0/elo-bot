from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol, assert_never

import aiohttp
import interactions as ipy
from pydantic import Field, ValidationError

from cogs.war_hosting.helper_api import (
    HelperJob,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateConflict,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperPreserveBaseLinkPayload,
    HelperTrackingFailure,
    helper_job_notification,
)
from common.constants import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from database_models import BaseLayout


type HelperPreserveBaseLinkTerminalStatus = Literal["succeeded", "failed", "cancelled"]


class HelperPreserveBaseLinkRuntime(Protocol):
    helper_api_base_url: str
    helper_api_key: str

    async def send_base_renewal_log(self, base: BaseLayout) -> None: ...

    async def send_base_renewal_failure_log(self, base: BaseLayout, notification: str) -> None: ...


class HelperPreserveBaseLinkTerminalJob(HelperJob):
    job_type: Literal["preserve_base_link"] = Field(alias="type")
    status: HelperPreserveBaseLinkTerminalStatus
    result: None


type HelperPreserveBaseLinkOutcome = (
    HelperPreserveBaseLinkTerminalJob
    | HelperTrackingFailure
    | HelperJobCreateRejected
    | HelperJobCreateAcceptedInvalid
    | HelperJobCreateUnavailable
)
type HelperPreserveBaseLinkAccepted = HelperJobCreated | HelperJobCreateConflict


async def preserve_base_link(
        runtime: HelperPreserveBaseLinkRuntime,
        requested_by: str,
        base_link: str,
        notify_job_accepted: Callable[[HelperPreserveBaseLinkAccepted], Awaitable[None]],
) -> HelperPreserveBaseLinkOutcome:
    request = HelperJobCreateRequest(
        job_type="preserve_base_link",
        requested_by=requested_by,
        payload=HelperPreserveBaseLinkPayload(base_link=base_link),
    )

    async def discard_job_update(_job: HelperJob) -> None:
        pass

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        create_outcome = await HelperJobCreator(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).create(session, request)
        match create_outcome:
            case HelperJobCreated(id=job_id) as accepted:
                await notify_job_accepted(accepted)
                poll_outcome = await HelperJobPoller(
                    runtime.helper_api_base_url,
                    runtime.helper_api_key,
                ).wait(session, job_id, discard_job_update)
            case HelperJobCreateConflict(existing_job_id=job_id) as accepted:
                await notify_job_accepted(accepted)
                poll_outcome = await HelperJobPoller(
                    runtime.helper_api_base_url,
                    runtime.helper_api_key,
                ).wait(session, job_id, discard_job_update)
            case (
                HelperJobCreateRejected()
                | HelperJobCreateAcceptedInvalid()
                | HelperJobCreateUnavailable()
            ) as create_failure:
                return create_failure
            case unexpected:
                assert_never(unexpected)

    match poll_outcome:
        case HelperTrackingFailure() as tracking_failure:
            return tracking_failure
        case HelperJob(status="succeeded" | "failed" | "cancelled") as terminal_job:
            try:
                return HelperPreserveBaseLinkTerminalJob.model_validate(
                    terminal_job.model_dump(by_alias=True),
                )
            except ValidationError:
                return HelperTrackingFailure(
                    job_id=terminal_job.id,
                    reason="the status API returned a terminal job that did not match the preserve request.",
                )
        case HelperJob(id=job_id):
            return HelperTrackingFailure(
                job_id=job_id,
                reason="the status API returned before the preserve job became terminal.",
            )
        case unexpected:
            assert_never(unexpected)


async def preserve_and_renew_base(
        runtime: HelperPreserveBaseLinkRuntime,
        base: BaseLayout,
        requested_by: str,
        notify_job_accepted: Callable[[HelperPreserveBaseLinkAccepted], Awaitable[None]],
) -> HelperPreserveBaseLinkOutcome:
    outcome = await preserve_base_link(runtime, requested_by, base.link, notify_job_accepted)
    match outcome:
        case HelperPreserveBaseLinkTerminalJob(status="failed"):
            outcome = await preserve_base_link(runtime, requested_by, base.link, notify_job_accepted)
        case (
            HelperPreserveBaseLinkTerminalJob(status="succeeded" | "cancelled")
            | HelperTrackingFailure()
            | HelperJobCreateRejected()
            | HelperJobCreateAcceptedInvalid()
            | HelperJobCreateUnavailable()
        ):
            pass
        case unexpected:
            assert_never(unexpected)
    match outcome:
        case HelperPreserveBaseLinkTerminalJob(status="succeeded"):
            base.expiration_date = datetime.now().timestamp() + 15 * 86400
            await base.save_changes()
            await runtime.send_base_renewal_log(base)
        case HelperPreserveBaseLinkTerminalJob(status="failed" | "cancelled"):
            await runtime.send_base_renewal_failure_log(base, helper_job_notification(outcome))
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            await runtime.send_base_renewal_failure_log(
                base,
                f"Could not monitor Helper job `{job_id}`: {reason}",
            )
        case HelperJobCreateAcceptedInvalid():
            await runtime.send_base_renewal_failure_log(
                base,
                "Helper accepted the preserve request, but its job ID could not be confirmed.",
            )
        case HelperJobCreateRejected(error=None):
            await runtime.send_base_renewal_failure_log(base, "Helper rejected the preserve request.")
        case HelperJobCreateRejected(error=error):
            await runtime.send_base_renewal_failure_log(
                base,
                f"Helper rejected the preserve request: {error}",
            )
        case HelperJobCreateUnavailable():
            await runtime.send_base_renewal_failure_log(
                base,
                "The Helper service could not queue the preserve request.",
            )
        case unexpected:
            assert_never(unexpected)
    return outcome


async def automatically_renew_base(
        runtime: HelperPreserveBaseLinkRuntime,
        base: BaseLayout,
        requested_by: str,
) -> HelperPreserveBaseLinkOutcome:
    async def discard_job_accepted(_accepted: HelperPreserveBaseLinkAccepted) -> None:
        pass

    return await preserve_and_renew_base(runtime, base, requested_by, discard_job_accepted)


async def autorenew_base(
        runtime: HelperPreserveBaseLinkRuntime,
        ctx: ipy.SlashContext,
        base_link: str,
) -> None:
    await ctx.defer(ephemeral=True)

    base = await BaseLayout.find_one(BaseLayout.link == base_link)
    if base is None:
        await ctx.send(
            f"{ERROR_EMOJI} This base is not stored in the helper's base bank.",
            ephemeral=True,
        )
        return

    async def notify_job_accepted(accepted: HelperPreserveBaseLinkAccepted) -> None:
        match accepted:
            case HelperJobCreated(id=job_id):
                content = f"{LOADING_EMOJI} Helper base-renewal job `{job_id}` has been queued."
            case HelperJobCreateConflict(existing_job_id=job_id, existing_job_status=status):
                content = f"{LOADING_EMOJI} Helper base-renewal job `{job_id}` is {status}."
            case unexpected:
                assert_never(unexpected)
        await ctx.send(content, ephemeral=True)

    outcome = await preserve_and_renew_base(runtime, base, str(ctx.author.id), notify_job_accepted)
    match outcome:
        case HelperPreserveBaseLinkTerminalJob(status="succeeded"):
            await ctx.send(
                f"{SUCCESS_EMOJI} The `{base.district}` base link was preserved and renewed for 15 days.",
                ephemeral=True,
            )
        case HelperPreserveBaseLinkTerminalJob(status="failed"):
            notification = helper_job_notification(outcome)
            await ctx.send(
                f"{ERROR_EMOJI} {notification} The base expiration was not changed.",
                ephemeral=True,
            )
        case HelperPreserveBaseLinkTerminalJob(status="cancelled"):
            await ctx.send(
                f"{ERROR_EMOJI} {helper_job_notification(outcome)} The base expiration was not changed.",
                ephemeral=True,
            )
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            await ctx.send(
                f"{ERROR_EMOJI} Could not monitor Helper job `{job_id}`: {reason} "
                "The base expiration was not changed; contact a helper before retrying.",
                ephemeral=True,
            )
        case HelperJobCreateAcceptedInvalid():
            await ctx.send(
                f"{ERROR_EMOJI} Helper accepted the request, but its job ID could not be confirmed. "
                "The base expiration was not changed; contact a helper before retrying.",
                ephemeral=True,
            )
        case HelperJobCreateRejected(error=None):
            await ctx.send(
                f"{ERROR_EMOJI} Helper rejected the preserve request. The base expiration was not changed.",
                ephemeral=True,
            )
        case HelperJobCreateRejected(error=error):
            await ctx.send(
                f"{ERROR_EMOJI} Helper rejected the preserve request: {error} "
                "The base expiration was not changed.",
                ephemeral=True,
            )
        case HelperJobCreateUnavailable():
            await ctx.send(
                f"{ERROR_EMOJI} The Helper service could not queue the preserve request. "
                "The base expiration was not changed.",
                ephemeral=True,
            )
        case unexpected:
            assert_never(unexpected)

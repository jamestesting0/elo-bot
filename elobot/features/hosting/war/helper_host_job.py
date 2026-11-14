# noqa: SIZE_OK - shared Helper polling and cancellation form one state machine

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
import re
import time
from typing import Final, Protocol, assert_never

import aiohttp
import anyio
import interactions as ipy

from elobot.features.base_store.publication import BaseStoreMessageConflict
from elobot.features.hosting.war.confirmation import confirm_helper_queue
from elobot.integrations.helper_api import (
    HelperEta,
    HelperEtaFailure,
    HelperEtaReader,
    HelperJob,
    HelperJobCanceller,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateConflict,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperJobType,
    HelperTrackingFailure,
    helper_job_notification,
)
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI


HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS: Final = 30
FIRST_DISTRICT_SETUP: Final = re.compile(r"^Setting up .+? \(1 of [1-9]\d*\)")
type HelperHostOutcome = HelperJob | HelperTrackingFailure
type AbortStartedHost = Callable[[ipy.Message], Awaitable[bool]]
type HostSuccessHook = Callable[[], None]
type HelperHostContext = ipy.SlashContext | ipy.ComponentContext


class HelperHostJobRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str


@dataclass(frozen=True, slots=True)
class IncompleteHelperHostRollbackError(ValueError):
    started_message_supplied: bool
    abort_callback_supplied: bool

    def __str__(self) -> str:
        return "started_message and abort_started must be provided together"


@dataclass(frozen=True, slots=True)
class HelperHostJobRequest:
    create_request: HelperJobCreateRequest
    requester_id: str
    additional_cancel_user_ids: frozenset[str] = frozenset()
    started_message: ipy.Message | None = None
    abort_started: AbortStartedHost | None = None
    on_success: HostSuccessHook | None = None
    public_progress: bool = False

    def __post_init__(self) -> None:
        if (self.started_message is None) != (self.abort_started is None):
            raise IncompleteHelperHostRollbackError(
                started_message_supplied=self.started_message is not None,
                abort_callback_supplied=self.abort_started is not None,
            )


async def _failure_cleanup_notice(request: HelperHostJobRequest) -> str:
    if request.abort_started is None:
        return "\n\nThe existing match remains active."
    assert request.started_message is not None
    try:
        aborted = await request.abort_started(request.started_message)
    except BaseStoreMessageConflict as error:
        logging.exception(
            "Helper hosting aborted the match, but stored-base message %s could not be updated",
            request.started_message.id,
        )
        return f"\n\nMatch aborted. {error}"
    if aborted:
        return "\n\nMatch aborted."
    return "\n\nMatch could not be aborted safely, please contact a helper."


async def confirm_helper_availability(
        runtime: HelperHostJobRuntime,
        ctx: HelperHostContext,
        job_type: HelperJobType,
        *,
        public_progress: bool = False,
) -> bool:
    ephemeral = not public_progress
    eta_outcome = await HelperEtaReader(
        runtime.helper_api_base_url,
        runtime.helper_api_key,
        job_type,
    ).read()
    match eta_outcome:
        case HelperEtaFailure(reason=reason):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=ephemeral)
            return False
        case HelperEta(estimate_start_at=None):
            await ctx.send(
                f"{ERROR_EMOJI} Helper availability cannot be estimated because no devices are connected.",
                ephemeral=ephemeral,
            )
            return False
        case HelperEta(estimate_start_at=estimate_start_at):
            return (
                estimate_start_at <= time.time() + HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS
                or await confirm_helper_queue(
                    runtime.bot, ctx, estimate_start_at, ephemeral=ephemeral,
                )
            )
        case unreachable:
            assert_never(unreachable)


async def run_helper_host_job(
        runtime: HelperHostJobRuntime,
        ctx: HelperHostContext,
        request: HelperHostJobRequest,
) -> None:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        poller = HelperJobPoller(runtime.helper_api_base_url, runtime.helper_api_key)
        create_outcome = await HelperJobCreator(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).create(session, request.create_request)
        match create_outcome:
            case HelperJobCreated(id=job_id):
                progress_content = f"{LOADING_EMOJI} Helper-hosting job `{job_id}` has been queued."
            case HelperJobCreateConflict(existing_job_id=job_id, existing_job_status=status, error=error):
                existing = await poller.read(session, job_id)
                match existing:
                    case HelperJob(job_type=open_type) if open_type == request.create_request.job_type:
                        progress_content = f"{LOADING_EMOJI} Helper-hosting job `{job_id}` is {status}."
                    case HelperJob():
                        notification = f"{ERROR_EMOJI} {error}"
                        notification += await _failure_cleanup_notice(request)
                        await ctx.send(notification, ephemeral=True)
                        return
                    case HelperTrackingFailure():
                        await ctx.send(
                            f"{ERROR_EMOJI} Helper's response could not be confirmed."
                            " The match remains active because the job may still run."
                            " Please contact a helper before retrying.",
                            ephemeral=True,
                        )
                        return
                    case unexpected:
                        assert_never(unexpected)
            case HelperJobCreateAcceptedInvalid():
                await ctx.send(
                    f"{ERROR_EMOJI} Helper accepted the request, but its job ID could not be confirmed."
                    " The match remains active because the job may still run."
                    " Please contact a helper before retrying.",
                    ephemeral=True,
                )
                return
            case HelperJobCreateRejected(error=error):
                notification = f"{ERROR_EMOJI} Helper rejected the request"
                notification += f": {error}" if error else "."
                notification += await _failure_cleanup_notice(request)
                await ctx.send(notification, ephemeral=True)
                return
            case HelperJobCreateUnavailable():
                await ctx.send(
                    f"{ERROR_EMOJI} Helper's response could not be confirmed."
                    " The match remains active because the job may still run."
                    " Please contact a helper before retrying.",
                    ephemeral=True,
                )
                return
            case unexpected:
                assert_never(unexpected)

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id=f"helper_job_cancel|{job_id}",
            emoji=CROSS_EMOJI,
        )
        if request.public_progress:
            progress_message = await ctx.channel.send(progress_content, components=cancel_button)
        else:
            progress_message = await ctx.send(progress_content, components=cancel_button, ephemeral=True)
        cancel_available = True

        async def edit_progress_message(**kwargs) -> None:
            try:
                if request.public_progress:
                    await progress_message.edit(**kwargs)
                else:
                    await ctx.edit(progress_message, **kwargs)
            except ipy.errors.HTTPException:
                logging.exception("Unable to publish Helper hosting progress for message %s", progress_message.id)

        async def update_progress(current_job: HelperJob) -> None:
            nonlocal cancel_available
            if cancel_available and any(
                    FIRST_DISTRICT_SETUP.match(event.message) for event in current_job.events):
                cancel_available = False
            match current_job.status:
                case "queued" | "running":
                    emoji = LOADING_EMOJI
                case "succeeded" | "cancelled":
                    emoji = SUCCESS_EMOJI
                case "failed":
                    emoji = ERROR_EMOJI
                case unexpected:
                    assert_never(unexpected)
            await edit_progress_message(
                content=f"{emoji} {helper_job_notification(current_job)}",
                components=(
                    cancel_button
                    if cancel_available and current_job.status in {"queued", "running"}
                    else []
                ),
            )

        send_outcome, receive_outcome = anyio.create_memory_object_stream[HelperHostOutcome](0)

        async def monitor_job() -> None:
            async with send_outcome.clone() as outcome_stream:
                await outcome_stream.send(await poller.wait(session, job_id, update_progress))

        def can_cancel(event: ipy.events.Component) -> bool:
            user_id = str(event.ctx.author.id)
            return user_id == request.requester_id or user_id in request.additional_cancel_user_ids

        async def wait_for_cancellation() -> None:
            nonlocal cancel_available
            async with send_outcome.clone() as outcome_stream:
                while True:
                    component_ctx: ipy.ComponentContext = (
                        await runtime.bot.wait_for_component(
                            components=cancel_button,
                            messages=int(progress_message.id),
                            check=can_cancel,
                        )
                    ).ctx
                    if not cancel_available:
                        await component_ctx.edit_origin(components=[])
                        return
                    cancel_button.disabled = True
                    await component_ctx.edit_origin(components=cancel_button)
                    try:
                        await HelperJobCanceller(runtime.helper_api_base_url, runtime.helper_api_key).cancel(
                            session,
                            job_id,
                        )
                    except (aiohttp.ClientError, TimeoutError):
                        if cancel_available:
                            cancel_button.disabled = False
                            await edit_progress_message(components=cancel_button)
                        await component_ctx.send(
                            f"{ERROR_EMOJI} Helper could not cancel the hosting job. Please try again.",
                            ephemeral=True,
                        )
                        if not cancel_available:
                            await component_ctx.edit_origin(components=[])
                            return
                        continue
                    cancel_available = False
                    cancellation_outcome = await poller.read(session, job_id)
                    match cancellation_outcome:
                        case HelperJob() as current_job:
                            await update_progress(current_job)
                            if current_job.status in {"succeeded", "failed", "cancelled"}:
                                await outcome_stream.send(current_job)
                        case HelperTrackingFailure():
                            await edit_progress_message(components=[])
                        case unexpected:
                            assert_never(unexpected)
                    return

        async with send_outcome, receive_outcome:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(monitor_job)
                task_group.start_soon(wait_for_cancellation)
                outcome = await receive_outcome.receive()
                task_group.cancel_scope.cancel()

    match outcome:
        case HelperJob(status="succeeded"):
            if request.on_success is not None:
                request.on_success()
        case HelperJob(status="failed"):
            notification = f"{ERROR_EMOJI} {helper_job_notification(outcome)}"
            notification += await _failure_cleanup_notice(request)
            await edit_progress_message(content=notification, components=[])
        case HelperJob(status="cancelled"):
            notification = f"{SUCCESS_EMOJI} {helper_job_notification(outcome)}"
            if request.abort_started is None:
                notification += await _failure_cleanup_notice(request)
            else:
                cleanup_notice = await _failure_cleanup_notice(request)
                if cleanup_notice.endswith("Match aborted."):
                    notification += cleanup_notice
                else:
                    notification = f"{ERROR_EMOJI} {helper_job_notification(outcome)}{cleanup_notice}"
            await edit_progress_message(content=notification, components=[])
        case HelperTrackingFailure():
            await edit_progress_message(
                content=f"{ERROR_EMOJI} {helper_job_notification(outcome)}",
                components=[],
            )
        case unreachable:
            assert_never(unreachable)

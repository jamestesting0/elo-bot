from dataclasses import dataclass
import re
from typing import Final, assert_never

import aiohttp
import anyio
import interactions as ipy

import elobot.features.hosting.war.stored_bases as stored_bases
from elobot.integrations.helper_api import (
    HelperHostWarMatchOnStoredPayload,
    HelperJob,
    HelperJobCanceller,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperRemoveBasesPayload,
    HelperStoreBasesPayload,
    HelperTrackingFailure,
    helper_job_notification,
)
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, LOADING_EMOJI


FIRST_DISTRICT_SETUP: Final = re.compile(r"^Setting up .+? \(1 of [1-9]\d*\)")


type HelperStoredBaseContext = ipy.SlashContext | ipy.ComponentContext
type HelperStoredBasePayload = (
    HelperStoreBasesPayload | HelperHostWarMatchOnStoredPayload | HelperRemoveBasesPayload
)


@dataclass(frozen=True, slots=True)
class InteractiveStoredBaseJob:
    job_type: stored_bases.HelperStoredBaseJobType
    payload: HelperStoredBasePayload


@dataclass(frozen=True, slots=True)
class StoredBaseJobUI:
    name: str
    cancel_id_prefix: str
    hide_cancel_after_first_district: bool
    completion_message: str


def _job_ui(job_type: stored_bases.HelperStoredBaseJobType) -> StoredBaseJobUI:
    match job_type:
        case "store_bases":
            return StoredBaseJobUI(
                "base-storage",
                "helper_base_store_cancel",
                False,
                "Helper copied the bases in-game. The slot reservation still needs confirmation.",
            )
        case "host_war_match_on_stored":
            return StoredBaseJobUI(
                "stored-base match-start",
                "helper_stored_start_cancel",
                True,
                "Helper completed the in-game match start. The match state still needs confirmation.",
            )
        case "remove_bases":
            return StoredBaseJobUI(
                "stored-base removal",
                "helper_stored_undo_cancel",
                False,
                "Helper removed the bases in-game. The slot release still needs confirmation.",
            )
        case unreachable:
            assert_never(unreachable)


type HelperStoredBaseInteractiveOutcome = stored_bases.HelperStoredBaseJobOutcome
type HelperStoredBaseMonitorOutcome = HelperJob | HelperTrackingFailure


async def _run_stored_base_job_interactively(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
        interactive_job: InteractiveStoredBaseJob,
) -> HelperStoredBaseInteractiveOutcome:
    ui = _job_ui(interactive_job.job_type)
    request = HelperJobCreateRequest(
        job_type=interactive_job.job_type,
        requested_by=str(ctx.author.id),
        payload=interactive_job.payload,
    )
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        create_outcome = await HelperJobCreator(
            runtime.helper_api_base_url,
            runtime.helper_api_key,
        ).create(session, request)
        match create_outcome:
            case HelperJobCreated() as job:
                pass
            case (
                HelperJobCreateRejected()
                | HelperJobCreateAcceptedInvalid()
                | HelperJobCreateUnavailable()
            ) as create_failure:
                return create_failure
            case unexpected:
                assert_never(unexpected)

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id=f"{ui.cancel_id_prefix}|{job.id}",
            emoji=CROSS_EMOJI,
        )
        progress_message = await ctx.send(
            f"{LOADING_EMOJI} Helper {ui.name} job `{job.id}` has been queued.",
            components=cancel_button,
            ephemeral=True,
        )

        cancel_available = True
        cancellation_requested = False
        job_finished = False
        progress_lock = anyio.Lock()

        async def update_progress(current_job: HelperJob) -> None:
            nonlocal cancel_available, job_finished
            async with progress_lock:
                job_finished = current_job.status in {"succeeded", "failed", "cancelled"}
                if job_finished or (ui.hide_cancel_after_first_district and any(
                        FIRST_DISTRICT_SETUP.match(event.message) for event in current_job.events)):
                    cancel_available = False
                match current_job.status:
                    case "queued" | "running":
                        content = f"{LOADING_EMOJI} {helper_job_notification(current_job)}"
                    case "succeeded":
                        content = f"{ui.completion_message} Check the final response below."
                    case "failed" | "cancelled":
                        content = f"{ERROR_EMOJI} {helper_job_notification(current_job)}"
                    case unexpected:
                        assert_never(unexpected)
                if cancellation_requested and not job_finished:
                    content += " Cancellation requested; waiting for Helper to confirm the final result."
                await ctx.edit(
                    progress_message,
                    content=content,
                    components=cancel_button if cancel_available else [],
                )

        poller = HelperJobPoller(runtime.helper_api_base_url, runtime.helper_api_key)
        send_outcome, receive_outcome = anyio.create_memory_object_stream[HelperStoredBaseMonitorOutcome](0)

        async def monitor_job() -> None:
            async with send_outcome.clone() as outcome_stream:
                await outcome_stream.send(await poller.wait(session, job.id, update_progress))

        requester_id = str(ctx.author.id)

        def is_requester(event: ipy.events.Component) -> bool:
            return str(event.ctx.author.id) == requester_id

        async def wait_for_cancellation() -> None:
            nonlocal cancel_available, cancellation_requested
            while True:
                component_ctx: ipy.ComponentContext = (
                    await runtime.bot.wait_for_component(
                        components=cancel_button,
                        messages=int(progress_message.id),
                        check=is_requester,
                    )
                ).ctx
                async with progress_lock:
                    if not cancel_available:
                        await component_ctx.edit_origin(components=[])
                        return
                    cancel_button.disabled = True
                    await component_ctx.edit_origin(components=cancel_button)
                try:
                    await HelperJobCanceller(runtime.helper_api_base_url, runtime.helper_api_key).cancel(
                        session,
                        job.id,
                    )
                except (aiohttp.ClientError, TimeoutError):
                    async with progress_lock:
                        if cancel_available:
                            cancel_button.disabled = False
                            await ctx.edit(progress_message, components=cancel_button)
                    await component_ctx.send(
                        f"{ERROR_EMOJI} Helper could not confirm the cancellation request. "
                        "Tracking will continue; you can try again.",
                        ephemeral=True,
                    )
                    if not cancel_available:
                        await component_ctx.edit_origin(components=[])
                        return
                    continue
                async with progress_lock:
                    cancellation_requested = True
                    cancel_available = False
                    if not job_finished:
                        await ctx.edit(
                            progress_message,
                            content=(
                                f"{LOADING_EMOJI} Helper {ui.name} job `{job.id}` cancellation requested. "
                                "Waiting for Helper to confirm the final result."
                            ),
                            components=[],
                        )
                return

        async with send_outcome, receive_outcome:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(monitor_job)
                task_group.start_soon(wait_for_cancellation)
                monitor_outcome = await receive_outcome.receive()
                task_group.cancel_scope.cancel()

    outcome = stored_bases._stored_base_poll_outcome(monitor_outcome)

    match outcome:
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            await ctx.edit(
                progress_message,
                content=(
                    f"{ERROR_EMOJI} Could not monitor Helper base-storage job `{job_id}`: {reason} "
                    "The stored-base state remains unchanged; contact a helper before retrying."
                ),
                components=[],
            )
            return outcome
        case stored_bases.HelperStoredBaseTerminalJob():
            return outcome
        case unexpected:
            assert_never(unexpected)


async def store_bases_interactively(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
        payload: HelperStoreBasesPayload,
) -> HelperStoredBaseInteractiveOutcome:
    return await _run_stored_base_job_interactively(
        runtime,
        ctx,
        InteractiveStoredBaseJob("store_bases", payload),
    )


async def host_war_match_on_stored_interactively(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
        payload: HelperHostWarMatchOnStoredPayload,
) -> HelperStoredBaseInteractiveOutcome:
    return await _run_stored_base_job_interactively(
        runtime,
        ctx,
        InteractiveStoredBaseJob("host_war_match_on_stored", payload),
    )


async def remove_bases_interactively(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
        payload: HelperRemoveBasesPayload,
) -> HelperStoredBaseInteractiveOutcome:
    return await _run_stored_base_job_interactively(
        runtime,
        ctx,
        InteractiveStoredBaseJob("remove_bases", payload),
    )

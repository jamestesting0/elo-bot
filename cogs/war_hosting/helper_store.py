# noqa: SIZE_OK — one cohesive score-job lifecycle keeps cancellation and merge state together

from dataclasses import dataclass
import time
from typing import Final, Protocol, assert_never

import aiohttp
import anyio
import coc
import interactions as ipy

from cogs.war_hosting import result_store_ui
from cogs.war_hosting.helper_api import (
    HelperEta,
    HelperEtaFailure,
    HelperEtaReader,
    HelperFetchScorePayload,
    HelperFetchLiveScorePayload,
    HelperLiveScoreResult,
    HelperScoreResult,
    HelperJob,
    HelperJobCanceller,
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
    HelperJobCreateUnavailable,
    HelperJobCreated,
    HelperJobCreator,
    HelperJobPoller,
    HelperJobStatus,
    HelperScore,
    HelperScoreAttackLimit,
    HelperTrackingFailure,
    helper_job_notification,
)
from cogs.war_hosting.confirmation import confirm_helper_queue
from common.constants import CROSS_EMOJI, ERROR_EMOJI, IMPORTANT_EMOJI, LOADING_EMOJI, STAFF_ROLE
from common.results.validation import is_valid_match_result_entry
from database_models import Match, Player


HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS: Final = 30
MAX_MINERS_PER_ATTACK: Final = 3


@dataclass(frozen=True, slots=True)
class HelperScoreStoreRequest:
    match: Match
    player: int | None  # None reads both participants of a live match.
    interactive: bool = True
    allow_cancellation: bool = True
    max_allowed_attacks: HelperScoreAttackLimit = 2
    ephemeral: bool = True


@dataclass(frozen=True, slots=True)
class HelperScoreStoreSkip:
    district: str
    reason: str


@dataclass(frozen=True, slots=True)
class HelperScoreStoreOutcome:
    results: tuple[str | None, ...]
    changed: bool
    skipped: tuple[HelperScoreStoreSkip, ...]
    job_id: str | None
    status: HelperJobStatus | None
    summary: str
    trusted: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class HelperLiveScoreStoreOutcome:
    trusted: tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]


class HelperScoreStoreRuntime(Protocol):
    bot: ipy.Client
    helper_api_base_url: str
    helper_api_key: str


type HelperScoreStoreContext = (
    ipy.SlashContext | ipy.ComponentContext | result_store_ui.ScheduledResultStoreContext
)


def _current_results(match: Match, player: int) -> tuple[str | None, ...]:
    results = match.participants[player].result
    return tuple(results)


async def _safe_edit_progress(
        ctx: HelperScoreStoreContext,
        message: ipy.Message,
        *,
        content: str | None = None,
        components: list[ipy.ActionRow | ipy.Button] | ipy.Button | list | None = None,
) -> None:
    kwargs = {}
    if content is not None:
        kwargs["content"] = content
    if components is not None:
        kwargs["components"] = components
    try:
        await ctx.edit(message, **kwargs)
    except (ipy.errors.HTTPException, aiohttp.ClientError, TimeoutError):
        pass


async def _safe_edit_origin(
        ctx: ipy.ComponentContext,
        *,
        components: list[ipy.ActionRow | ipy.Button] | ipy.Button | list | None = None,
) -> None:
    kwargs = {}
    if components is not None:
        kwargs["components"] = components
    try:
        await ctx.edit_origin(**kwargs)
    except (ipy.errors.HTTPException, aiohttp.ClientError, TimeoutError):
        pass


async def _reject_store(
        ctx: HelperScoreStoreContext,
        request: HelperScoreStoreRequest,
        results: tuple[str | None, str | None, str | None],
        message: str,
) -> HelperScoreStoreOutcome:
    summary = f"{ERROR_EMOJI} {message}"
    await ctx.send(summary, ephemeral=request.ephemeral)
    return HelperScoreStoreOutcome(
        results=results,
        changed=False,
        skipped=(),
        job_id=None,
        status=None,
        summary=summary,
    )


async def _confirm_score_availability(
        runtime: HelperScoreStoreRuntime,
        ctx: HelperScoreStoreContext,
        request: HelperScoreStoreRequest,
) -> str | None:
    eta_outcome = await HelperEtaReader(
        runtime.helper_api_base_url,
        runtime.helper_api_key,
        "fetch_live_score" if request.player is None else "fetch_score",
    ).read()
    match eta_outcome:
        case HelperEtaFailure(reason=reason):
            summary = f"{ERROR_EMOJI} {reason}"
            await ctx.send(summary, ephemeral=request.ephemeral)
            return summary
        case HelperEta(estimate_start_at=None):
            summary = f"{ERROR_EMOJI} Helper availability cannot be estimated because no devices are connected."
            await ctx.send(summary, ephemeral=request.ephemeral)
            return summary
        case HelperEta(estimate_start_at=estimate_start_at):
            if not request.interactive or estimate_start_at <= time.time() + HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS:
                return None
            if await confirm_helper_queue(
                    runtime.bot,
                    ctx,
                    estimate_start_at,
                    f"{ERROR_EMOJI} Helper score storage was cancelled.",
                    ephemeral=request.ephemeral,
            ):
                return None
            return f"{ERROR_EMOJI} Helper score storage was cancelled."
        case unreachable:
            assert_never(unreachable)


def _result_entry(score: HelperScore) -> str | None:
    if not score.attacker_name_verified or not score.same_attacker_for_all_attacks:
        return None
    stars = score.stars_gained
    destruction = score.destruction_gained
    if stars is None or destruction is None:
        return None
    if not destruction.is_integer():
        return None
    entry = f"{stars}.{int(destruction)}"
    return entry if is_valid_match_result_entry(entry) else None


def _skipped_reason(score: HelperScore | None) -> str:
    if score is None:
        return "Helper did not find a usable score."
    if not score.attacker_name_verified or not score.same_attacker_for_all_attacks:
        return "Helper could not verify that every attack belongs to this player."
    if score.stars_gained is None:
        return "Helper could not determine the stars."
    return "Helper returned an invalid score."


def _merge_scores(
        match: Match,
        player: int,
        score_result: HelperScoreResult,
) -> tuple[
    tuple[str | None, str | None, str | None],
    tuple[HelperScoreStoreSkip, ...],
    tuple[tuple[str, str], ...],
]:
    results = list(_current_results(match, player))
    districts = match.final_districts
    skipped = []
    trusted = []
    for index, district in enumerate(districts):
        if district is None:
            continue
        score = score_result.scores.get(district)
        entry = _result_entry(score) if score is not None else None
        if entry is None:
            skipped.append(HelperScoreStoreSkip(district=district, reason=_skipped_reason(score)))
            continue
        results[index] = entry
        trusted.append((district, entry))
    for district in score_result.scores:
        if district not in districts:
            skipped.append(HelperScoreStoreSkip(district=district, reason="Helper returned an unrequested district."))
    return tuple(results), tuple(skipped), tuple(trusted)


def _districts_with_miner_limit_violations(match: Match, score_result: HelperScoreResult) -> tuple[str, ...]:
    violations = []
    for district in match.final_districts:
        if district is None:
            continue
        score = score_result.scores.get(district)
        if (
            score is not None
            and score.attacker_name_verified
            and score.same_attacker_for_all_attacks
            and any(
                attack.miner_count is not None and attack.miner_count > MAX_MINERS_PER_ATTACK
                for attack in score.attacks
            )
        ):
            violations.append(district)
    return tuple(violations)


def _stored_summary(job: HelperJob, changed: bool, skipped: tuple[HelperScoreStoreSkip, ...]) -> str:
    found = "Found trusted scores for verification." if changed else "No additional trusted scores were found."
    skipped_text = ""
    if skipped:
        skipped_text = "\n\nSkipped: " + "; ".join(f"{skip.district} ({skip.reason})" for skip in skipped)
    match job.status:
        case "succeeded":
            return f"{found} The result has not been saved yet.{skipped_text}"
        case "failed":
            return f"{ERROR_EMOJI} {helper_job_notification(job)} {found} The result has not been saved yet.{skipped_text}"
        case unreachable:
            assert_never(unreachable)


async def store_helper_score(
        runtime: HelperScoreStoreRuntime,
        ctx: HelperScoreStoreContext,
        request: HelperScoreStoreRequest,
) -> HelperScoreStoreOutcome | HelperLiveScoreStoreOutcome:
    match_doc = request.match
    if request.player is None:
        if match_doc.is_war or len(match_doc.participants) < 2:
            return await _reject_store(ctx, request, (None, None, None), "This match is not available for live result storage.")
    elif request.player not in {0, 1} or len(match_doc.participants) <= request.player:
        return await _reject_store(ctx, request, (None, None, None), "Unable to fetch a valid match player.")

    player_index = request.player if request.player is not None else 0
    results = _current_results(match_doc, player_index)
    host_clan_tag = match_doc.host_clans[player_index]
    started_at = match_doc.times[player_index]
    districts = [district for district in match_doc.final_districts if district]
    ends_at = time.time()
    if not host_clan_tag:
        return await _reject_store(ctx, request, results, "No host clan is recorded for this match.")
    if started_at is None:
        return await _reject_store(ctx, request, results, "The match has not started yet.")
    if ends_at <= started_at:
        return await _reject_store(ctx, request, results, "The result window must end after the match started.")
    if len(districts) not in {3, 7} or len(set(districts)) != len(districts):
        return await _reject_store(ctx, request, results, "The match districts are invalid.")

    participant = match_doc.participants[player_index]
    player = await Player.find_one(Player.user_id == participant.user_id)
    if player is None or not player.tag:
        return await _reject_store(ctx, request, results, "Unable to find this player's Clash account.")

    if request.player is None:
        opponent = await Player.find_one(Player.user_id == match_doc.participants[1].user_id)
        if opponent is None or not opponent.tag:
            return await _reject_store(ctx, request, results, "Unable to find the other player's Clash account.")
        create_request = HelperJobCreateRequest(
            job_type="fetch_live_score",
            requested_by=str(ctx.author.id),
            payload=HelperFetchLiveScorePayload(
                host_clan_tag=host_clan_tag,
                player_1_tag=player.tag,
                player_2_tag=opponent.tag,
                attacks_started_after=started_at - 300,
                attacks_ended_before=ends_at + 300,
                districts=districts,
                max_allowed_attacks={district: request.max_allowed_attacks for district in districts},
            ),
        )
    else:
        create_request = HelperJobCreateRequest(
            job_type="fetch_score",
            requested_by=str(ctx.author.id),
            payload=HelperFetchScorePayload(
                host_clan_tag=host_clan_tag,
                attacker_tag=player.tag,
                attacks_started_after=started_at - 300,
                attacks_ended_before=ends_at + 300,
                districts=districts,
                max_allowed_attacks={district: request.max_allowed_attacks for district in districts},
            ),
        )
    availability_error = await _confirm_score_availability(runtime, ctx, request)
    if availability_error:
        return HelperScoreStoreOutcome(
            results=results,
            changed=False,
            skipped=(),
            job_id=None,
            status=None,
            summary=availability_error,
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
                return await _reject_store(
                    ctx,
                    request,
                    results,
                    "Helper accepted the request, but the outcome could not be confirmed. Please contact the bot developers.",
                )
            case HelperJobCreateRejected(error=None):
                return await _reject_store(ctx, request, results, "Helper rejected the score request.")
            case HelperJobCreateRejected(error=error):
                return await _reject_store(ctx, request, results, f"Helper rejected the score request: {error}")
            case HelperJobCreateUnavailable():
                return await _reject_store(ctx, request, results, "The Helper score service could not queue the request.")
            case unexpected:
                assert_never(unexpected)

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id=f"helper_score_cancel|{job.id}",
            emoji=CROSS_EMOJI,
        )
        progress_message = await ctx.send(
            f"{LOADING_EMOJI} Helper score job `{job.id}` has been queued.",
            components=cancel_button if request.allow_cancellation else [],
            ephemeral=request.ephemeral,
        )

        cancellation_requested = False
        job_finished = False
        progress_lock = anyio.Lock()

        async def update_progress(current_job: HelperJob) -> None:
            nonlocal job_finished
            async with progress_lock:
                job_finished = current_job.status in {"succeeded", "failed", "cancelled"}
                match current_job.status:
                    case "queued" | "running":
                        content = f"{LOADING_EMOJI} {helper_job_notification(current_job)}"
                    case "succeeded":
                        content = "Helper finished reading scores. The result still needs verification before saving."
                    case "failed" | "cancelled":
                        content = f"{ERROR_EMOJI} {helper_job_notification(current_job)}"
                    case unexpected:
                        assert_never(unexpected)
                if cancellation_requested and not job_finished:
                    content += " Cancellation requested. Waiting for the final result."
                await _safe_edit_progress(
                    ctx,
                    progress_message,
                    content=content,
                    components=cancel_button if request.allow_cancellation and not job_finished
                    and not cancellation_requested else [],
                )

        poller = HelperJobPoller(runtime.helper_api_base_url, runtime.helper_api_key)
        send_outcome, receive_outcome = anyio.create_memory_object_stream[
            HelperJob | HelperTrackingFailure
        ](0)

        async def monitor_job() -> None:
            async with send_outcome.clone() as outcome_stream:
                await outcome_stream.send(await poller.wait(session, job.id, update_progress))

        requester_id = str(ctx.author.id)

        def is_requester(event: ipy.events.Component) -> bool:
            return str(event.ctx.author.id) == requester_id

        async def wait_for_cancellation() -> None:
            nonlocal cancellation_requested
            while True:
                component_ctx: ipy.ComponentContext = (
                    await runtime.bot.wait_for_component(
                        components=cancel_button,
                        messages=int(progress_message.id),
                        check=is_requester,
                    )
                ).ctx
                async with progress_lock:
                    if job_finished:
                        await _safe_edit_origin(component_ctx, components=[])
                        return
                    cancel_button.disabled = True
                    await _safe_edit_origin(component_ctx, components=cancel_button)
                try:
                    await HelperJobCanceller(runtime.helper_api_base_url, runtime.helper_api_key).cancel(
                        session,
                        job.id,
                    )
                except (aiohttp.ClientError, TimeoutError):
                    async with progress_lock:
                        if not job_finished:
                            cancel_button.disabled = False
                            await _safe_edit_progress(ctx, progress_message, components=cancel_button)
                    try:
                        await component_ctx.send(
                            f"{ERROR_EMOJI} Helper could not confirm the cancellation request. Tracking will continue.",
                            ephemeral=request.ephemeral,
                        )
                    except (ipy.errors.HTTPException, aiohttp.ClientError, TimeoutError):
                        pass
                    continue
                async with progress_lock:
                    cancellation_requested = True
                    if not job_finished:
                        await _safe_edit_progress(
                            ctx,
                            progress_message,
                            content=f"{LOADING_EMOJI} Cancellation requested. Waiting for Helper's final result.",
                            components=[],
                        )
                return

        async with send_outcome, receive_outcome:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(monitor_job)
                if request.allow_cancellation:
                    task_group.start_soon(wait_for_cancellation)
                outcome = await receive_outcome.receive()
                task_group.cancel_scope.cancel()

    if request.player is None and isinstance(outcome, HelperJob) and outcome.job_type == "fetch_live_score":
        assert isinstance(create_request.payload, HelperFetchLiveScorePayload)
        return await _live_score_outcome(ctx, request, outcome, create_request.payload, progress_message)

    match outcome:
        case HelperTrackingFailure(job_id=job_id, reason=reason):
            summary = f"{ERROR_EMOJI} Could not monitor Helper score job `{job_id}`: {reason} Results were not changed."
            await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
            return HelperScoreStoreOutcome(results, False, (), job_id, None, summary)
        case HelperJob(job_type="fetch_score", status="cancelled", id=job_id):
            summary = f"{ERROR_EMOJI} Helper score job `{job_id}` was cancelled."
            await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
            return HelperScoreStoreOutcome(results, False, (), job_id, "cancelled", summary)
        case HelperJob(job_type="fetch_score", status="succeeded" | "failed", result=None, id=job_id) as completed_job:
            summary = f"{ERROR_EMOJI} {helper_job_notification(outcome)} Results were not changed."
            await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
            return HelperScoreStoreOutcome(results, False, (), job_id, completed_job.status, summary)
        case HelperJob(job_type="fetch_score", status="succeeded" | "failed") as completed_job:
            assert isinstance(completed_job.result, HelperScoreResult)
            miner_limit_violations = _districts_with_miner_limit_violations(match_doc, completed_job.result)
            if miner_limit_violations:
                skipped = tuple(
                    HelperScoreStoreSkip(
                        district=district,
                        reason="Helper detected more than 3 miners in an attack.",
                    )
                    for district in miner_limit_violations
                )
                summary = f"{ERROR_EMOJI} Helper detected a miner-limit violation. Results were not changed."
                await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
                districts = ", ".join(f"**{district}**" for district in miner_limit_violations)
                _, _, found_results = _merge_scores(match_doc, player_index, completed_job.result)
                found_results_by_district = dict(found_results)
                result_summary = result_store_ui.format_result_summary(
                    match_doc.final_districts,
                    [
                        found_results_by_district.get(district) if district is not None else None
                        for district in match_doc.final_districts
                    ],
                )
                await ctx.channel.send(
                    f"<@&{STAFF_ROLE}>\n{IMPORTANT_EMOJI} <@{participant.user_id}> has used more than 3 miners "
                    f"in at least one attack for the following districts: {districts}. Please take action and "
                    f"apply penalties accordingly.\n\n**Results:**\n{result_summary}"
                )
                return HelperScoreStoreOutcome(
                    results,
                    False,
                    skipped,
                    completed_job.id,
                    completed_job.status,
                    summary,
                )
            merged_results, skipped, trusted = _merge_scores(match_doc, player_index, completed_job.result)
            changed = merged_results != results
            summary = _stored_summary(completed_job, changed, skipped)
            await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
            return HelperScoreStoreOutcome(
                merged_results,
                changed,
                skipped,
                completed_job.id,
                completed_job.status,
                summary,
                trusted,
            )
        case HelperJob(id=job_id):
            summary = f"{ERROR_EMOJI} Helper returned an unexpected job `{job_id}`. Results were not changed."
            await _safe_edit_progress(ctx, progress_message, content=summary, components=[])
            return HelperScoreStoreOutcome(results, False, (), job_id, None, summary)
        case unexpected:
            assert_never(unexpected)


async def _live_score_outcome(
        ctx: HelperScoreStoreContext,
        request: HelperScoreStoreRequest,
        job: HelperJob,
        payload: HelperFetchLiveScorePayload,
        progress_message: ipy.Message,
) -> HelperLiveScoreStoreOutcome:
    empty = HelperLiveScoreStoreOutcome(((), ()))
    if job.status not in {"succeeded", "failed"} or not isinstance(job.result, HelperLiveScoreResult):
        await _safe_edit_progress(
            ctx,
            progress_message,
            content=f"{ERROR_EMOJI} {helper_job_notification(job)} Results were not changed.",
            components=[],
        )
        return empty
    players = (job.result.scores.player_1, job.result.scores.player_2)
    expected_tags = (payload.player_1_tag, payload.player_2_tag)
    if any(coc.utils.correct_tag(score.player_tag) != coc.utils.correct_tag(tag)
           for score, tag in zip(players, expected_tags)):
        await _safe_edit_progress(
            ctx,
            progress_message,
            content=f"{ERROR_EMOJI} Helper returned scores for different players. "
            "Results were not changed.",
            components=[],
        )
        return empty

    trusted_results = []
    summaries = []
    violation_found = False
    for index, score in enumerate(players):
        merged, skipped, trusted = _merge_scores(request.match, index, score)
        violations = _districts_with_miner_limit_violations(request.match, score)
        if violations:
            violation_found = True
            districts = ", ".join(f"**{district}**" for district in violations)
            summary = result_store_ui.format_result_summary(request.match.final_districts, merged)
            await ctx.channel.send(
                f"<@&{STAFF_ROLE}>\n{IMPORTANT_EMOJI} <@{request.match.participants[index].user_id}> has used more than "
                f"3 miners in at least one attack for the following districts: {districts}. Please take action and "
                f"apply penalties accordingly.\n\n**Results:**\n{summary}"
            )
        trusted_results.append(trusted)
        summaries.append(f"<@{request.match.participants[index].user_id}>: {_stored_summary(job, bool(trusted), skipped)}")
    if violation_found:
        await _safe_edit_progress(
            ctx,
            progress_message,
            content=f"{ERROR_EMOJI} Helper detected a miner-limit violation. "
            "Results were not changed.",
            components=[],
        )
        return empty
    await _safe_edit_progress(ctx, progress_message, content="\n\n".join(summaries), components=[])
    return HelperLiveScoreStoreOutcome((trusted_results[0], trusted_results[1]))

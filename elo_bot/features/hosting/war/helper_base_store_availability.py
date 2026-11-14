import time
from typing import Final, assert_never

import interactions as ipy

import elo_bot.features.hosting.war.stored_bases as stored_bases
from elo_bot.features.hosting.war.confirmation import confirm_helper_queue
from elo_bot.integrations.helper_api import HelperEta, HelperEtaFailure, HelperEtaReader
from elo_bot.config.emojis import ERROR_EMOJI


HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS: Final = 30
BASE_STORE_CANCELLED_MESSAGE: Final = f"{ERROR_EMOJI} Helper base storage was cancelled."
type HelperStoredBaseContext = ipy.SlashContext | ipy.ComponentContext


def _cancelled_message(job_type: stored_bases.HelperStoredBaseJobType) -> str:
    match job_type:
        case "store_bases":
            return BASE_STORE_CANCELLED_MESSAGE
        case "host_war_match_on_stored":
            return f"{ERROR_EMOJI} Helper stored-base match start was cancelled."
        case "remove_bases":
            return f"{ERROR_EMOJI} Helper stored-base removal was cancelled."
        case unreachable:
            assert_never(unreachable)


async def _confirm_stored_base_job_availability(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
        job_type: stored_bases.HelperStoredBaseJobType,
) -> bool:
    eta_outcome = await HelperEtaReader(
        runtime.helper_api_base_url,
        runtime.helper_api_key,
        job_type,
    ).read()
    match eta_outcome:
        case HelperEtaFailure(reason=reason):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return False
        case HelperEta(estimate_start_at=None):
            await ctx.send(
                f"{ERROR_EMOJI} Helper availability cannot be estimated because no devices are connected.",
                ephemeral=True,
            )
            return False
        case HelperEta(estimate_start_at=estimate_start_at):
            return (
                estimate_start_at <= time.time() + HELPER_QUEUE_PROMPT_THRESHOLD_SECONDS
                or await confirm_helper_queue(
                    runtime.bot,
                    ctx,
                    estimate_start_at,
                    _cancelled_message(job_type),
                )
            )
        case unreachable:
            assert_never(unreachable)


async def confirm_store_bases_availability(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
) -> bool:
    return await _confirm_stored_base_job_availability(runtime, ctx, "store_bases")


async def confirm_host_war_match_on_stored_availability(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
) -> bool:
    return await _confirm_stored_base_job_availability(runtime, ctx, "host_war_match_on_stored")


async def confirm_remove_bases_availability(
        runtime: stored_bases.HelperStoredBaseRuntime,
        ctx: HelperStoredBaseContext,
) -> bool:
    return await _confirm_stored_base_job_availability(runtime, ctx, "remove_bases")

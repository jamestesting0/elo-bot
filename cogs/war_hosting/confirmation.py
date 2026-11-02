from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import assert_never

import interactions as ipy

from common.constants import CROSS_EMOJI, ERROR_EMOJI
from database_models import Match


class HelperQueueDecision(StrEnum):
    CONTINUE = "helper_eta_continue"
    CANCEL = "helper_eta_cancel"


@dataclass(frozen=True, slots=True)
class PendingBaseConfirmation:
    match: Match
    player: int


async def confirm_pending_opponent_bases(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        confirmation: PendingBaseConfirmation,
) -> bool:
    opponent = 1 - confirmation.player
    opponent_id = confirmation.match.participants[opponent].user_id
    pinned_messages = await ctx.channel.fetch_pinned_messages()
    if pinned_messages:
        match_embed = pinned_messages[-1].embeds[0]
    else:
        match_message = (await ctx.channel.fetch_messages())[-1]
        match_embed = match_message.embeds[0]
        await match_message.pin()

    fields = match_embed.fields
    submission_pending = (
        len(fields) > 4
        and any(f"{mention} wish to submit bases." in fields[4].value
                for mention in (f"<@{opponent_id}>", f"users/{opponent_id}>)", f"users/{opponent_id}>)**"))
        and not any(f"{mention} has submitted bases." in fields[4].value
                    for mention in (f"<@{opponent_id}>", f"users/{opponent_id}>)", f"users/{opponent_id}>)**"))
    )
    if not submission_pending:
        return True

    thread_is_recent = datetime.now().timestamp() - ctx.channel.created_at.timestamp() < 43200
    if not thread_is_recent:
        return True

    continue_button = ipy.Button(
        style=ipy.ButtonStyle.GREY,
        label="Continue",
        custom_id="continue",
        emoji="⏩",
    )
    message = await ctx.send(
        f"Are you sure that you would like to start the match, while <@{opponent_id}> "
        "still need to submit bases. You should **not** start unless the player has specifically said something.",
        components=continue_button,
        ephemeral=True,
    )
    try:
        component_ctx: ipy.ComponentContext = (
            await bot.wait_for_component(
                components=continue_button,
                messages=int(message.id),
                timeout=180,
            )
        ).ctx
    except TimeoutError:
        await ctx.delete(message)
        return False

    continue_button.disabled = True
    await component_ctx.edit_origin(components=continue_button)
    return True


async def confirm_helper_queue(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        estimate_start_at: float,
        cancelled_message: str = "Helper hosting cancelled.",
        *,
        ephemeral: bool = True,
) -> bool:
    continue_button = ipy.Button(
        style=ipy.ButtonStyle.SUCCESS,
        label="Continue",
        custom_id=HelperQueueDecision.CONTINUE,
        emoji="⏩",
    )
    cancel_button = ipy.Button(
        style=ipy.ButtonStyle.DANGER,
        label="Cancel",
        custom_id=HelperQueueDecision.CANCEL,
        emoji=CROSS_EMOJI,
    )
    action_row = ipy.ActionRow(continue_button, cancel_button)
    estimate_timestamp = int(estimate_start_at)
    message = await ctx.send(
        "Helper is currently busy. Estimated availability: "
        f"<t:{estimate_timestamp}:R>. Would you like to continue?",
        components=action_row,
        ephemeral=ephemeral,
    )
    try:
        component_ctx: ipy.ComponentContext = (
            await bot.wait_for_component(
                components=action_row,
                messages=int(message.id),
                timeout=180,
                check=lambda event: str(event.ctx.author.id) == str(ctx.author.id),
            )
        ).ctx
    except TimeoutError:
        timeout_message = (
            f"{ERROR_EMOJI} Helper work was not started because the queue confirmation timed out."
        )
        try:
            await ctx.edit(message, content=timeout_message, components=[])
        except ipy.errors.HTTPException:
            await ctx.send(timeout_message, ephemeral=ephemeral)
        return False

    disabled_components = ipy.utils.misc_utils.disable_components(action_row)
    match HelperQueueDecision(component_ctx.custom_id):
        case HelperQueueDecision.CONTINUE:
            await component_ctx.edit_origin(components=disabled_components)
            return True
        case HelperQueueDecision.CANCEL:
            await component_ctx.edit_origin(
                content=cancelled_message,
                components=disabled_components,
            )
            return False
        case unreachable:
            assert_never(unreachable)

import logging
import re
from datetime import datetime
from typing import assert_never

import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.config.emojis import (
    ERROR_EMOJI,
    SUCCESS_EMOJI,
)
from elobot.config.ids import (
    INFO_CHANNEL,
)
from elobot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
)
from elobot.db import Player, Queue, ServerState
from elobot.db.player_models import QueueDistrictSelection
from elobot.extensions.match.groups import base
from elobot.features.matches.lifecycle import get_district_options
from elobot.features.matches.queued_districts import (
    find_attempt_match,
    find_attempt_queue,
    queue_district_components,
)
from elobot.features.queue.matching import queue_player, run_queue_matching_trigger
from elobot.features.queue.policy import QUEUE_OPERATION_LOCK, QUEUE_TIERS
from elobot.features.queue.state import QueueOutcomeStatus, autoqueue_expiry, remove_player_from_queue


class QueueCommands:
    @base.subcommand(sub_cmd_name="dequeue", sub_cmd_description="Stop queuing for a match based on your league")
    async def match_dequeue(self, ctx: ipy.SlashContext):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        player_removed = False
        async with QUEUE_OPERATION_LOCK:
            for tier in QUEUE_TIERS:
                queue = await Queue.find_one(Queue.tier == tier, fetch_links=True)
                if queue and remove_player_from_queue(queue, str(ctx.author.id)):
                    await queue.save_changes()
                    player_removed = True

        if player_removed:
            await ctx.send(f"{SUCCESS_EMOJI} You have been removed from the match-making queue.", ephemeral=True)
        else:
            await ctx.send(f"{ERROR_EMOJI} You are not currently in the match-making queue.", ephemeral=True)

    @base.subcommand(sub_cmd_name="autoqueue", sub_cmd_description="Automatically queue after completed matches")
    @ipy.slash_option(
        name="enabled",
        description="Enable automatic queueing after completed matches",
        opt_type=ipy.OptionType.BOOLEAN,
        required=True,
    )
    @ipy.slash_option(
        name="duration",
        description="How long autoqueue should stay enabled",
        opt_type=ipy.OptionType.STRING,
        required=False,
        choices=[
            ipy.SlashCommandChoice(name="Forever", value="Forever"),
            ipy.SlashCommandChoice(name="7 Days", value="7 Days"),
            ipy.SlashCommandChoice(name="3 Days", value="3 Days"),
            ipy.SlashCommandChoice(name="1 Day", value="1 Day"),
        ],
    )
    async def match_autoqueue(self, ctx: ipy.SlashContext, enabled: bool, duration: str | None = None):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)
            return

        expiry = autoqueue_expiry(duration) if enabled and duration else None
        if enabled and expiry is None:
            await ctx.send(f"{ERROR_EMOJI} Please select how long autoqueue should stay enabled.", ephemeral=True)
            return

        player.auto_queue = expiry if enabled else "0"
        await player.save_changes()
        await ctx.send(f"{SUCCESS_EMOJI} Autoqueue preference updated.", ephemeral=True)

    @base.subcommand(sub_cmd_name="queue", sub_cmd_description="Start queuing for a match based on your league")
    @ipy.slash_option(
        name="priority",
        description="Try priority matching if the closest Elo player's Elo is more than 100 away",
        opt_type=ipy.OptionType.BOOLEAN,
        required=False
    )
    async def match_queue(self, ctx: ipy.SlashContext | ipy.ComponentContext, priority: bool = False):
        await ctx.defer(ephemeral=True)
        outcome = await queue_player(self.bot, ctx.author, priority, district_picker=True)
        match outcome.status:
            case QueueOutcomeStatus.BLACKLISTED:
                await ctx.send(f"{ERROR_EMOJI} You are currently banned from playing Elo. If you are unsure of why, "
                               f"please open a support ticket in <#{INFO_CHANNEL}>.", ephemeral=True)
            case QueueOutcomeStatus.DISABLED:
                await ctx.send(f"{ERROR_EMOJI} Match-making is currently disabled due to season reset.",
                               ephemeral=True)
            case QueueOutcomeStatus.UNREGISTERED:
                await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                               f"please register using the command `/player register`.", ephemeral=True)
            case QueueOutcomeStatus.ALREADY_QUEUED:
                await ctx.send(f"{ERROR_EMOJI} You are already in the match-making queue.", ephemeral=True)
            case QueueOutcomeStatus.OCCUPIED:
                if outcome.occupied:
                    await ctx.send(
                        f"{ERROR_EMOJI} {outcome.occupied.message}",
                        components=outcome.occupied.component,
                        ephemeral=True,
                    )
            case QueueOutcomeStatus.MATCHED:
                await ctx.send(f"{SUCCESS_EMOJI} A match has been found, please check your DM for "
                               f"further information!", ephemeral=True)
            case QueueOutcomeStatus.BETA_QUEUED:
                await self._send_queue_district_picker(
                    ctx, outcome.selection,
                    f"{SUCCESS_EMOJI} For beta testing, you are added to the mixed queue for instant matches.",
                )
            case QueueOutcomeStatus.QUEUED:
                await self._send_queue_district_picker(
                    ctx, outcome.selection,
                    f"{SUCCESS_EMOJI} You are now added to the match-making queue. "
                    f"You will be informed via DM about your match in around 3 hours. "
                    f"If you would like to stop matching for an opponent, please run the command `/match dequeue`.",
                )
            case unreachable:
                assert_never(unreachable)

    async def _send_queue_district_picker(
            self, ctx: ipy.SlashContext | ipy.ComponentContext,
            selection: QueueDistrictSelection, content: str,
    ) -> None:
        user_id = str(ctx.author.id)
        async with QUEUE_OPERATION_LOCK:
            match = await find_attempt_match(user_id, selection.attempt_id)
            if match is not None:
                await ctx.send(f"{SUCCESS_EMOJI} A match has been found, please check your DM for "
                               f"further information!", ephemeral=True)
                return
            queue = await find_attempt_queue(user_id, selection.attempt_id, QUEUE_TIERS)
            if queue is None:
                await ctx.send(f"{ERROR_EMOJI} This queue entry is no longer active.", ephemeral=True)
                return
            selection = queue.district_selections[user_id]
            player = next(player for player in queue.players if str(player.user_id) == user_id)
            options = get_district_options(player.elo < 1200, player.capital_peak_enabled)
            description = "Optionally choose your districts before matching your opponent, but you will not get " \
                          "time to cancel once matched."
            if any(selection.districts):
                description += " Your district preset is loaded, feel free to update them if needed."
            embed = ipy.Embed(title="**Match-making Queue**", description=description, color=COLOR,
                              footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE))
            await ctx.send(content, embed=embed, components=queue_district_components(user_id, selection, options),
                           ephemeral=True)

    @ipy.component_callback(re.compile(r"^queued_(?:district|confirm|cancel)\|\d+\|\w+(?:\|[0-2])?$"))
    async def queued_district_action(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        action, user_id, attempt_id, *indexes = ctx.custom_id.split("|")
        if user_id != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return
        if action == "queued_district" and (not indexes or len(ctx.values) != 1):
            await ctx.send(f"{ERROR_EMOJI} That district is not available for this queue entry.", ephemeral=True)
            return
        cancel_token = None
        saved_state = "Any saved queue or match changes remain saved."
        try:
            async with QUEUE_OPERATION_LOCK:
                match = await find_attempt_match(user_id, attempt_id)
                if match is not None:
                    participant = next(p for p in match.participants if p.user_id == user_id)
                    if match.thread:
                        await ctx.message.edit(components=[], context=ctx)
                        await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
                        return
                    match action:
                        case "queued_cancel":
                            cancel_token = match.token
                        case "queued_district":
                            if participant.confirmed:
                                await ctx.message.edit(components=[], context=ctx)
                                await ctx.send(f"{ERROR_EMOJI} You have already confirmed this match.", ephemeral=True)
                                return
                            await self._select_match_district(ctx, match, indexes[0])
                        case "queued_confirm":
                            if not all(participant.districts):
                                await ctx.send(f"{ERROR_EMOJI} Please select all three districts.", ephemeral=True)
                                return
                            participant.queue_districts_locked = True
                            opponent = next(p for p in match.participants if p.user_id != user_id)
                            await self._confirm_match_participant(ctx, match, participant, opponent, match.token)
                            saved_state = "Your match confirmation is saved."
                            await ctx.message.edit(components=[], context=ctx)
                else:
                    queue = await find_attempt_queue(user_id, attempt_id, QUEUE_TIERS)
                    if queue is None:
                        await ctx.message.edit(components=[], context=ctx)
                        await ctx.send(f"{ERROR_EMOJI} This queue entry is no longer active.", ephemeral=True)
                        return
                    selection = queue.district_selections[user_id]
                    player = next(player for player in queue.players if str(player.user_id) == user_id)
                    options = get_district_options(player.elo < 1200, player.capital_peak_enabled)
                    match action:
                        case "queued_cancel":
                            for tier in QUEUE_TIERS:
                                current = await find_attempt_queue(user_id, attempt_id, [tier])
                                if current is not None:
                                    remove_player_from_queue(current, user_id)
                                    await current.save_changes()
                            saved_state = "You have been removed from the match-making queue."
                            await ctx.message.edit(components=[], context=ctx)
                            await ctx.send(f"{SUCCESS_EMOJI} You have been removed from the match-making queue.",
                                           ephemeral=True)
                        case "queued_district" | "queued_confirm":
                            if action == "queued_district":
                                index = int(indexes[0])
                                if len(ctx.values) != 1 or ctx.values[0] not in options[index]:
                                    await ctx.send(f"{ERROR_EMOJI} That district is not available for this queue entry.",
                                                   ephemeral=True)
                                    return
                                selection.districts[index] = ctx.values[0]
                                selection.confirmed = False
                                await queue.save_changes()
                                saved_state = "Your updated district choices are saved and need confirmation."
                                await ctx.send(f"{SUCCESS_EMOJI} You have selected **{ctx.values[0].lower()}**. "
                                               "Press Confirm again.",
                                               ephemeral=True)
                            else:
                                if any(district not in group for district, group in zip(selection.districts, options)):
                                    await ctx.send(f"{ERROR_EMOJI} Please select all three available districts.",
                                                   ephemeral=True)
                                    return
                                selection.confirmed = True
                                await queue.save_changes()
                                saved_state = "Your districts are saved."
                                await ctx.message.edit(
                                    components=queue_district_components(user_id, selection, options), context=ctx,
                                )
                                await ctx.send(f"{SUCCESS_EMOJI} Districts saved. "
                                               "You can change them until an opponent is found.",
                                               ephemeral=True)
        except PyMongoError:
            logging.exception("Queue district action failed for user %s, attempt %s", user_id, attempt_id)
            await ctx.send(f"{ERROR_EMOJI} The action could not be saved. Check your queue or match before retrying.",
                           ephemeral=True)
            return
        except ipy.errors.HTTPException:
            logging.exception("Queue district publication failed for user %s, attempt %s", user_id, attempt_id)
            await ctx.send(f"{ERROR_EMOJI} {saved_state} The display could not be updated.",
                           ephemeral=True)
            return
        if cancel_token is not None:
            await self._cancel_match(ctx, cancel_token)

    @ipy.Task.create(ipy.IntervalTrigger(hours=3))
    async def create_matches(self):
        async with QUEUE_OPERATION_LOCK:
            await run_queue_matching_trigger(self.bot)

        await ServerState.update_fields(last_matching=datetime.now().timestamp())

    @ipy.component_callback("match_queue")
    async def match_queue_button(self, ctx: ipy.ComponentContext):
        await self.match_queue(ctx)

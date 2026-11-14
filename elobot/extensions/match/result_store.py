import logging
import re
from typing import assert_never

import interactions as ipy
from interactions import utils
from pymongo.errors import PyMongoError

import elobot.features.hosting.live.result_store as live_result_store
import elobot.features.hosting.war.result_store as result_store
import elobot.features.hosting.war.result_store_ui as result_store_ui
from elobot.config.emojis import (
    ERROR_EMOJI,
    IMPORTANT_EMOJI,
    SUCCESS_EMOJI,
)
from elobot.config.game import (
    DISTRICT_CHOICES,
    DISTRICT_INDEX,
)
from elobot.config.ids import (
    ELO_BLACKLIST_ROLE,
    LVL2_ROLES,
)
from elobot.config.presentation import (
    ORDINAL_REPRESENTATIONS,
)
from elobot.core.finals import is_final_match
from elobot.core.match_results import ResultDetectionType, is_valid_match_result_entry
from elobot.db import Match
from elobot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from elobot.extensions.match.groups import result_group
from elobot.features.hosting.self_hosted.invitations import invite_eligible_players
from elobot.features.matches.controls import check_match_control, reject_outdated_match_control
from elobot.features.matches.message_lock import coordinated_match_message, match_message_lock
from elobot.features.matches.messages import (
    MatchMessageState,
    mark_match_message_ended,
)
from elobot.features.matches.resources import free_clan, remove_helper_task
from elobot.features.queue.matching import autoqueue_completed_match_participants
from elobot.features.results.operations import (
    result_input_error,
)
from elobot.integrations.helper_api import HelperScoreAttackLimit


class MatchResultStoreCommands:
    @ipy.component_callback(re.compile(r"^match_result_store\|\w+\|\w+$"))
    async def result_store_button(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if not await check_match_control(ctx, match, index):
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return

        # identify whether the clicker is one of the two players
        user_ids = [p.user_id for p in (match.participants or [])[:2]]
        is_player = str(ctx.author.id) in {str(uid) for uid in user_ids if uid}

        # if the clicker is a player, they can only submit if they are also the host for this side.
        current_host = match.hosts[index]
        if is_player and str(ctx.author.id) != str(current_host):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)
            return

        participant = match.participants[index]
        player = await self.bot.fetch_user(participant.user_id)

        # prepare labels and default values
        districts = match.final_districts
        results = participant.result

        if is_final_match(match):
            await ctx.send_modal(ipy.Modal(ipy.ParagraphText(
                label="Seven district results", custom_id="scores", max_length=100,
                value="\n".join(results) if all(results) else None,
                placeholder="Seven lines in district order: stars.destruction",
            ), title="Store Final Result", custom_id=f"store_modal|{token}|{index}"))
            return

        # short text expects strings; use empty string for none
        val1 = results[0] or ""
        val2 = results[1] or ""
        val3 = results[2] or ""

        label1 = districts[0] or "District 1"
        label2 = districts[1] or "District 2"
        label3 = districts[2] or "District 3"

        title = f"{player} Result Submission" if player else "Result Submission"
        if len(title) > 45:
            prefix = str(player)[:45 - len(" Result Submission")].rstrip()
            title = f"{prefix} Result Submission" if prefix else "Result Submission"

        modal = ipy.Modal(
            ipy.ShortText(
                label=label1,
                placeholder="Format: [stars].[destruction]",
                value=val1,
                max_length=100,
                custom_id="result1",
                required=False,
            ),
            ipy.ShortText(
                label=label2,
                placeholder="Format: [stars].[destruction]",
                value=val2,
                max_length=100,
                custom_id="result2",
                required=False,
            ),
            ipy.ShortText(
                label=label3,
                placeholder="Format: [stars].[destruction]",
                value=val3,
                max_length=100,
                custom_id="result3",
                required=False,
            ),
            title=title,
            custom_id=f"store_modal|{token}|{index}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^store_modal\|\w+\|\w+$"))
    @coordinated_match_message
    async def result_store_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)
        opponent_index = 0 if index else 1

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
            return

        if not await check_match_control(ctx, match, index):
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return
        expected = MatchMessageState.capture(match, index)

        if is_final_match(match):
            if (ctx.guild_id != match.routing_guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE)
                    or not any(ctx.author.has_role(role) for role in LVL2_ROLES)):
                await ctx.send(f"{ERROR_EMOJI} A staff member in the Final's server must store these results.",
                               ephemeral=True)
                return
            scores = response.get("scores", "").splitlines()
            if len(scores) != 7 or not all(is_valid_match_result_entry(score) for score in scores):
                await ctx.send(f"{ERROR_EMOJI} Enter seven valid stars.destruction lines.", ephemeral=True)
                return
            response = {f"result{position + 1}": score for position, score in enumerate(scores)}

        # resolve players from the participant list
        if (str(ctx.author.id) in {participant.user_id for participant in match.participants}
                and str(ctx.author.id) != str(match.hosts[index])):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)
            return
        player_id = match.participants[index].user_id
        player = await self.bot.fetch_user(int(player_id))

        # validate and stage updates to participant results
        updates = {}
        count = 0
        for result in response.values():
            if result and not is_valid_match_result_entry(result):
                await ctx.send(
                    f"{ERROR_EMOJI} The formatting of the {ORDINAL_REPRESENTATIONS[count]} "
                    f"result is incorrect. E.g., 2 stars 88% should be submitted as `2.88`.",
                    ephemeral=True,
                )
                return

            value = "" if not result else result
            updates[f"participants.{index}.result.{count}"] = value
            count += 1

        updated_results = list(match.participants[index].result)
        for position, value in enumerate(response.values()):
            updated_results[position] = value or ""
        if all(updated_results):
            updates[f"submitters.{index}"] = str(ctx.author.id)
        if updates:
            write = await match.get_pymongo_collection().update_one(
                {**expected.binding_filter(), "result_operation": None,
                 "final_result_id": None, "final_result_operation": None, "final_superseded": {"$ne": True},
                 "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
                 f"participants.{index}.result": list(match.participants[index].result)},
                {"$set": updates},
            )
            if write.matched_count != 1:
                fresh = await Match.find_one(Match.token == token)
                if fresh and (reason := result_input_error(fresh)):
                    await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
                else:
                    await reject_outdated_match_control(ctx)
                return
        match.participants[index].result = updated_results
        if all(updated_results):
            match.submitters[index] = str(ctx.author.id)
            await invite_eligible_players(self.bot, (player_id,))

        # reuse in-memory for checks/DM composition
        player_results = match.participants[index].result
        opponent_results = match.participants[opponent_index].result

        if all(player_results) and all(opponent_results):
            submit_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Submit Result",
                custom_id=f"submit_button|{token}",
                emoji=ipy.PartialEmoji(name="📥"),
            )

            await ctx.send(
                f"{SUCCESS_EMOJI} You have stored the match result for {player.mention}.\n\n"
                f"{IMPORTANT_EMOJI} *Both players' matches are recorded, you may submit them now!*",
                components=submit_button,
                ephemeral=True,
            )
        else:
            await ctx.send(
                f"{SUCCESS_EMOJI} You have stored the match result for {player.mention}.\n\n"
                f"{IMPORTANT_EMOJI} *Please kick the player out of the clan once the attacks are done!*",
                ephemeral=True,
            )

        if all(player_results):
            already_ended = ctx.message.embeds[0].title == "**Match Ended**"
            await mark_match_message_ended(ctx.message)
            if already_ended:
                return

            # ensure submitters exists and set the submitter for this side
            await result_store_ui.notify_stored_result(
                result_store_ui.StoredResultDmRequest(
                    match=match,
                    player=index,
                    user=player,
                )
            )

            await self._finish_stored_result_side(match, index)

    async def _finish_stored_result_side(self, match: Match, player: int) -> None:
        old_message_id = match.old_msgs[player]
        if old_message_id:
            try:
                async with match_message_lock(match.thread):
                    current = await Match.find_one({"token": match.token})
                    if (current and current.id == match.id and current.thread == match.thread
                            and current.old_msgs[player] == old_message_id
                            and current.participants[player].user_id == match.participants[player].user_id
                            and current.participants[player].result == match.participants[player].result):
                        channel = await self.bot.fetch_channel(int(match.thread))
                        message = await channel.fetch_message(old_message_id)
                        await mark_match_message_ended(message)
            except ipy.errors.NotFound:
                pass
            except (ipy.errors.HTTPException, PyMongoError):
                logging.exception(
                    "Stored result message %s remains pending recovery for match %s", old_message_id, match.token,
                )
        await free_clan(match.host_clans[player], expected_thread=str(match.thread))
        await remove_helper_task(match.thread, match.participants[player].user_id)
        await autoqueue_completed_match_participants(self.bot, match.token, (match.participants[player].user_id,))

    async def store_match_result_automatically(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
            *,
            max_allowed_attacks: HelperScoreAttackLimit = 2,
    ) -> None:
        await result_store.store_match_result_automatically(
            self,
            ctx,
            result_store.AutomaticResultStoreRequest(
                match=match,
                player=player,
                max_allowed_attacks=max_allowed_attacks,
            ),
        )

    async def store_self_result(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
    ) -> None:
        await result_store.store_match_result_automatically(
            self,
            ctx,
            result_store.AutomaticResultStoreRequest(
                match=match,
                player=player,
                expected_started_at=match.times[player],
                include_response_result=True,
                submitter_id=str(self.bot.user.id),
            ),
        )

    async def store_self_live_result(self, ctx: ipy.SlashContext, match: Match) -> None:
        await live_result_store.store_live_match_results(self, ctx, match)

    @result_group.subcommand(sub_cmd_name="clear", sub_cmd_description="Clear a player's stored result")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player whose result should be cleared",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1),
        ],
        required=True,
    )
    @coordinated_match_message
    async def match_result_clear(self, ctx: ipy.SlashContext, player: int) -> None:
        await ctx.defer(ephemeral=True)
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           "this command in a 1v1 thread!", ephemeral=True)
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return
        if match.recorded_match_id:
            await ctx.send(f"{ERROR_EMOJI} This match result is already recorded. "
                           "Use `/match resubmit` to correct it.", ephemeral=True)
            return

        is_player = str(ctx.author.id) in {participant.user_id for participant in match.participants}
        if is_player and str(ctx.author.id) != str(match.hosts[player]):
            await ctx.send(f"{ERROR_EMOJI} You cannot clear result as one of the players in the match.",
                           ephemeral=True)
            return

        field = f"participants.{player}.result"
        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, player).binding_filter(), "result_operation": None,
             "recorded_match_id": None, "final_result_id": None, "final_result_operation": None,
             "final_superseded": {"$ne": True},
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             field: list(match.participants[player].result)},
            {"$set": {field: [None] * len(match.final_districts), f"submitters.{player}": None}},
        )
        if write.matched_count != 1:
            fresh = await Match.find_one(Match.token == match.token)
            reason = result_input_error(fresh) if fresh else None
            await ctx.send(f"{ERROR_EMOJI} {reason or 'The match changed. Check the current result before retrying.'}",
                           ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Cleared <@{match.participants[player].user_id}>'s result.", ephemeral=True)

    @result_group.subcommand(sub_cmd_name="autostore", sub_cmd_description="Store a player's result with Helper")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to store result for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1),
        ],
        required=True,
    )
    @ipy.slash_option(
        name="detection_type",
        description="How Helper should detect the result",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Strict", value="Strict"),
            ipy.SlashCommandChoice(name="Relaxed", value="Relaxed"),
        ],
        required=False,
    )
    async def match_result_autostore(
            self,
            ctx: ipy.SlashContext,
            player: int,
            detection_type: ResultDetectionType = "Strict",
    ) -> None:
        await ctx.defer(ephemeral=True)
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True,
            )
            return
        if str(ctx.author.id) == str(match.participants[player].user_id):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot store your own result with this command. "
                "Please use `/self store result` instead.",
                ephemeral=True,
            )
            return
        if not match.is_war:
            await ctx.send(f"{ERROR_EMOJI} This match is not available for automatic result storage.", ephemeral=True)
            return
        if not match.times[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match has not started yet.", ephemeral=True)
            return
        if not match.host_clans[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match does not have a helper clan yet.", ephemeral=True)
            return
        if len(match.final_districts) not in {3, 7} or not all(match.final_districts):
            await ctx.send(f"{ERROR_EMOJI} This match does not have three final districts yet.", ephemeral=True)
            return
        match detection_type:
            case "Strict":
                max_allowed_attacks = 2
            case "Relaxed":
                max_allowed_attacks = -2
            case unexpected:
                assert_never(unexpected)
        await self.store_match_result_automatically(
            ctx,
            match,
            player,
            max_allowed_attacks=max_allowed_attacks,
        )

    @result_group.subcommand(sub_cmd_name="store", sub_cmd_description="Store the result on a single district")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to store result for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The district that got attacked?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="stars",
        description="The number of stars the player scored",
        opt_type=ipy.OptionType.INTEGER,
        max_value=3,
        min_value=0,
        required=True
    )
    @ipy.slash_option(
        name="destruction",
        description="The destruction the player scored",
        opt_type=ipy.OptionType.INTEGER,
        max_value=100,
        min_value=0,
        required=True
    )
    async def match_result_store(self, ctx: ipy.SlashContext, player: int, district: str, stars: int, destruction: int):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return

        # the district must be one of the chosen final districts
        if district not in (match.final_districts or []):
            await ctx.send(f"{ERROR_EMOJI} **{district}** is not chosen as part of the match!", ephemeral=True)
            return

        # if the clicker is a player, they can only store if they are the host for that side
        player_ids = [p.user_id for p in match.participants]
        is_player = str(ctx.author.id) in {str(pid) for pid in player_ids if pid}
        current_host = match.hosts[player]
        if is_player and str(ctx.author.id) != str(current_host):
            await ctx.send(f"{ERROR_EMOJI} You cannot store result as one of the players in the match.",
                           ephemeral=True)
            return

        # update the participant's result for the specified district
        district_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]
        field = f"participants.{player}.result.{district_idx}"
        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, player).binding_filter(), "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             field: match.participants[player].result[district_idx]},
            {"$set": {field: f"{stars}.{destruction}"}},
        )
        if write.matched_count != 1:
            fresh = await Match.find_one(Match.token == match.token)
            reason = result_input_error(fresh) if fresh else None
            await ctx.send(f"{ERROR_EMOJI} {reason or 'The match changed. Check the current result before retrying.'}",
                           ephemeral=True)
            return

        player_id = match.participants[player].user_id
        await ctx.send(f"{SUCCESS_EMOJI} You have recorded <@{player_id}>'s score on **{district}**.",
                       ephemeral=True)

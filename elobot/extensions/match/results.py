import asyncio
import copy
import logging
import re
from typing import assert_never

import anyio
import interactions as ipy
from interactions import utils
from pymongo.errors import PyMongoError

import elobot.features.base_store.publication as base_store_publication
import elobot.features.hosting.live.result_store as live_result_store
import elobot.features.hosting.war.result_store as result_store
import elobot.features.hosting.war.result_store_ui as result_store_ui
from elobot.config.emojis import (
    CROSS_EMOJI,
    DISTRICT_EMOJI,
    ERROR_EMOJI,
    IMPORTANT_EMOJI,
    LOSS_EMOJI,
    STAR_EMOJI,
    SUCCESS_EMOJI,
    WIN_EMOJI,
)
from elobot.config.game import (
    DISTRICT_CHOICES,
    DISTRICT_INDEX,
)
from elobot.config.ids import (
    ELO_BLACKLIST_ROLE,
    FINAL_CHANNEL,
    LVL1_ROLES,
    LVL2_ROLES,
    MAIN_GUILD,
    MATCH_CHANNEL,
    MODERATOR_ROLE,
    SEN_ROLE,
    STAFF_ROLE,
)
from elobot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    ORDINAL_REPRESENTATIONS,
    RED_COLOR,
)
from elobot.core.parsing import get_destruction, get_stars
from elobot.db import HelperHistory, Match, Participant, Player, RecordedMatch
from elobot.discord.channel_edits import schedule_channel_name_edit
from elobot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from elobot.discord.members import member_guild
from elobot.discord.mentions import mention_user
from elobot.discord.message_edits import edit_channel_message
from elobot.extensions.match.adapters import ResultDetectionType, clear_match_base_stores
from elobot.extensions.match.groups import base, result_group
from elobot.extensions.player import fetch_user_match_id_choices, forget_replaced_match_choice
from elobot.features.finals.types import is_final_match
from elobot.features.hosting.self_hosted.invitations import invite_eligible_players
from elobot.features.matches.cleanup import has_reserved_match_slots
from elobot.features.matches.controls import check_match_control, reject_outdated_match_control
from elobot.features.matches.lifecycle import get_district_options
from elobot.features.matches.message_lock import coordinated_match_message, match_message_lock
from elobot.features.matches.messages import (
    MatchMessageState,
    mark_match_message_ended,
)
from elobot.features.matches.resources import free_clan, free_match_clans, remove_helper_task
from elobot.features.matches.tips import choose_tip
from elobot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from elobot.features.queue.matching import autoqueue_completed_match_participants
from elobot.features.results.autowin import apply_autowin_player, claim_autowin, mark_autowin
from elobot.features.results.helper_credits import award_helper_once
from elobot.features.results.manual import (
    ManualResultConfirmationUnknown,
    manual_result_key,
)
from elobot.features.results.operations import (
    ResultOperationConflict,
    begin_result_operation,
    bind_result_message,
    complete_result_operation,
    completed_result_changes,
    publish_result_operation,
    result_input_error,
    rollback_result_operation,
)
from elobot.features.results.recording import _mark_result_publication_incomplete, record_base_stats, record_match
from elobot.features.results.validation import is_valid_complete_match_result, is_valid_match_result_entry
from elobot.integrations.helper_api import HelperScoreAttackLimit


class MatchResultCommands:
    @ipy.component_callback(re.compile(r"^submit_button\|\w+$"))
    async def result_submit_button(self, ctx: ipy.ComponentContext):
        if (not ctx.author.has_role(STAFF_ROLE) and not ctx.author.has_role(SEN_ROLE) and
                not ctx.author.has_role(MODERATOR_ROLE)):
            await ctx.send(f"{ERROR_EMOJI} Only staffs are allowed to submit results.", ephemeral=True)
            return

        _, token = ctx.custom_id.split("|")

        match: Match | None = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread.",
                           ephemeral=True)
            return

        if is_final_match(match):
            from elobot.features.finals.result_ui import show_source_result_form
            from elobot.features.finals.types import FinalError
            try:
                await show_source_result_form(ctx, match)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        # prevent staff who are also players (unless they're the bot owner)
        participant_ids = [p.user_id for p in match.participants]
        if str(ctx.author.id) in participant_ids and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot submit results as one of the players.", ephemeral=True)
            return

        if "🔴" in ctx.channel.name:
            await ctx.send(f"{ERROR_EMOJI} The match has already ended, you cannot submit again.", ephemeral=True)
            try:
                for actionrow in ctx.message.components:
                    for component in actionrow.components:
                        if component.custom_id.startswith("submit_button"):
                            component.disabled = True
                            break
                await ctx.message.edit(components=ctx.message.components)
            except ipy.errors.HTTPException:
                pass
            return

        p1, p2 = match.participants[0], match.participants[1]
        player1 = await self.bot.fetch_user(p1.user_id)
        player2 = await self.bot.fetch_user(p2.user_id)

        # pre-fill results if all three entries exist (same behavior as before)
        value1 = "\n".join(p1.result) if p1.result and all(p1.result) else None
        value2 = "\n".join(p2.result) if p2.result and all(p2.result) else None

        modal = ipy.Modal(
            ipy.ParagraphText(
                label=f"{player1}'s Result",
                placeholder="Format: [stars].[destruction]\n"
                            "E.g, 2 stars 83% -> 2.83\n"
                            "Separate results with line breaks",
                max_length=100,
                value=value1,
                custom_id="player1"
            ),
            ipy.ParagraphText(
                label=f"{player2}'s Result",
                placeholder="Format: [stars].[destruction]\n"
                            "E.g, 2 stars 83% -> 2.83\n"
                            "Separate results with line breaks",
                max_length=100,
                value=value2,
                custom_id="player2"
            ),
            title="Result Submission",
            custom_id=f"result_modal|{token}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^result_modal\|\w+$"))
    async def result_submit_modal(self, ctx: ipy.ModalContext, **response):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of the first result is invalid.", ephemeral=True)

            return

        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of the second result is invalid.", ephemeral=True)

            return

        await self._process_match_results(ctx, response, data_generation=data_generation)

    async def _process_match_results(
            self, ctx: ipy.ModalContext, response: dict, *, data_generation: int | None = None,
    ):
        _, token = ctx.custom_id.split("|")

        return await self._finalize_match_results(token, response=response, ctx=ctx, data_generation=data_generation)

    async def _finalize_match_results(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
            *,
            data_generation: int | None = None,
    ) -> bool:
        return await result_store.finalize_match_results(
            self,
            result_store.FinalizeMatchResultsRequest(token=token, response=response, ctx=ctx,
                                                    data_generation=data_generation),
        )

    async def _finalize_match_results_locked(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
    ) -> bool:
        match = await Match.find_one(Match.token == token)
        if match is None or not match.thread:
            return await self._publish_match_results_locked(token, response, ctx)
        async with match_message_lock(match.thread):
            return await self._publish_match_results_locked(token, response, ctx)

    async def _publish_match_results_locked(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
    ) -> bool:
        async def send_interaction_message(message: str) -> None:
            if ctx is not None:
                await ctx.send(message, ephemeral=True)

        mongo_match: Match | None = await Match.find_one(Match.token == token)
        if mongo_match is not None and is_final_match(mongo_match):
            from beanie import PydanticObjectId
            from pydantic import ValidationError

            from elobot.db.season_models import EloFinal
            from elobot.features.finals.results import FinalResultSubmission, automatic_operation_key, submit_result
            from elobot.features.finals.types import FinalError
            try:
                final = await EloFinal.get(PydanticObjectId(mongo_match.final_id))
                if final is None:
                    raise FinalError("The Final is missing. A manager must recover this match.")
                node = final.nodes[mongo_match.final_node_id]
                scores = {participant.user_id: (response[f"player{side + 1}"].splitlines() if response
                                               else list(participant.result))
                          for side, participant in enumerate(mongo_match.participants)}
                result = await submit_result(FinalResultSubmission(
                    final_id=final.id, node_id=node.id, actor_id=str(self.bot.user.id),
                    operation_key=automatic_operation_key(mongo_match, scores), expected_version=node.result_version,
                    contestant_revision=mongo_match.final_contestant_revision, attempt=mongo_match.final_attempt,
                    scores=scores,
                ))
                from elobot.features.finals.recovery import recover_final
                async def release_final(source):
                    await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="finished")
                    await free_match_clans(source)
            except (FinalError, ValidationError) as error:
                await send_interaction_message(f"{ERROR_EMOJI} " + (str(error) if isinstance(error, FinalError)
                    else "Both players need seven valid stars.destruction results."))
                return False
            try:
                pending = await recover_final(final, bot=self.bot, release_completed=release_final)
            except (FinalError, PyMongoError) as error:
                pending = [f"Recovery is pending: {error}. Use `/final recover`."]
            await send_interaction_message("Final result saved." + (" Updates remain pending: " + " ".join(pending) if pending else ""))
            return True
        if not mongo_match or not mongo_match.thread or len(mongo_match.participants) < 2:
            await send_interaction_message(
                f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread."
            )
            return False

        if mongo_match.result_operation and mongo_match.result_operation.cancelled:
            await send_interaction_message(f"{ERROR_EMOJI} {result_input_error(mongo_match)}")
            return False
        if token in self._pending_match_thread_renames:
            await send_interaction_message(
                f"{ERROR_EMOJI} The match has already ended, you cannot submit again."
            )
            return False

        match_thread = await self.bot.fetch_channel(int(mongo_match.thread))
        if (mongo_match.result_operation and mongo_match.result_operation.published) or (
                "🔴" in match_thread.name and not mongo_match.result_operation
        ):
            await send_interaction_message(
                f"{ERROR_EMOJI} The match has already ended, you cannot submit again."
            )
            return False

        p1 = mongo_match.participants[0]
        p2 = mongo_match.participants[1]
        player1_id = p1.user_id
        player2_id = p2.user_id
        player_ids = [player1_id, player2_id]

        if response is None:
            if any(
                    len(participant.result) != 3
                    or not all(isinstance(result, str) for result in participant.result)
                    for participant in (p1, p2)
            ):
                await send_interaction_message(
                    f"{ERROR_EMOJI} Both players must have complete valid results before the match can be submitted."
                )
                return False
            response = {
                "player1": "\n".join(p1.result),
                "player2": "\n".join(p2.result),
            }

        if not all(is_valid_complete_match_result(response[key]) for key in ("player1", "player2")):
            await send_interaction_message(
                f"{ERROR_EMOJI} Both players must have complete valid results before the match can be submitted."
            )
            return False

        try:
            await begin_result_operation(mongo_match, response)
        except ResultOperationConflict as error:
            await send_interaction_message(f"{ERROR_EMOJI} {error.message}")
            return False
        guild = await self.bot.fetch_guild(MAIN_GUILD)
        if mongo_match.result_operation.completed:
            player1_elo_change_str, player2_elo_change_str = await completed_result_changes(mongo_match)
            await invite_eligible_players(self.bot, (player1_id, player2_id), pending_only=True)
        else:
            # prepare commonly used fields
            final_districts = mongo_match.final_districts

            # record who hosted - using Helper model
            is_war = mongo_match.is_war
            hosts = (mongo_match.hosts or [None, None])
            times = (mongo_match.times or [None, None])
            single_host = hosts[0] if hosts else None
            single_time = int(mongo_match.matched_time or times[0] or 0)

            if not is_war and single_time:
                if single_host:
                    history_entry = HelperHistory(
                        time=single_time, player1=player1_id, player2=player2_id
                    )
                    await award_helper_once(single_host, f"result:{mongo_match.id}:host:live", 3, history_entry)
            else:
                for i in range(2):
                    host_id = hosts[i] if i < len(hosts) else None
                    host_time = times[i] if i < len(times) else None
                    if host_id and host_time:
                        history_entry = HelperHistory(
                            time=int(host_time), player1=player_ids[i], player2=None
                        )
                        await award_helper_once(host_id, f"result:{mongo_match.id}:host:{i}", 0.5, history_entry)

                    submitters = (mongo_match.submitters or [0, 0])
                    submitter_id = submitters[i] if i < len(submitters) else None
                    if submitter_id:
                        await award_helper_once(submitter_id, f"result:{mongo_match.id}:submitter:{i}", 0.5)

            # record the performance of the bases
            player_results = {
                player1_id: response["player1"].split("\n"),
                player2_id: response["player2"].split("\n"),
            }

            # record the base stats
            await record_base_stats(player_results, mongo_match)

            # record the result (unchanged)
            player1_elo_change_str, player2_elo_change_str = await record_match(
                guild, self.bot, player1_id, player2_id, response, final_districts, token
            )
            mongo_match = await Match.find_one(Match.token == token)
            await complete_result_operation(mongo_match)
            await invite_eligible_players(
                self.bot, (player1_id, player2_id),
                pending_only=mongo_match.result_operation.previous_record is not None,
            )

        await forget_replaced_match_choice(self.bot, mongo_match)
        await clear_match_base_stores(mongo_match, reset_stored_flags=True, bot=self.bot, reason="finished")
        await free_match_clans(mongo_match)

        interaction_message = getattr(ctx, "message", None) if ctx is not None else None
        if interaction_message is not None:
            try:
                for actionrow in interaction_message.components:
                    for component in actionrow.components:
                        if component.custom_id.startswith("submit_button"):
                            component.disabled = True
                            break
                await edit_channel_message(interaction_message, components=interaction_message.components)
            except ipy.errors.HTTPException:
                pass

        recorded_match = mongo_match.result_operation.record

        # build summary text from recorded_match
        match_info = ""
        p1_stats = recorded_match.players[player1_id]
        p2_stats = recorded_match.players[player2_id]
        
        district_keys = list(p1_stats.districts.keys())
        for district in district_keys:
            match_info += (
                f"{DISTRICT_EMOJI} **{district}**\n"
                f"{p1_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p1_stats.districts[district].destruction}% - "
                f"{p2_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p2_stats.districts[district].destruction}%\n\n"
            )

        match_info += (
            f"**Final Score**\n"
            f"{p1_stats.stars}{STAR_EMOJI} {p1_stats.destruction}% - "
            f"{p2_stats.stars}{STAR_EMOJI} {p2_stats.destruction}%"
        )

        members = await asyncio.gather(guild.fetch_member(player1_id), guild.fetch_member(player2_id))
        finished_embed = ipy.Embed(
            title="**Match has Ended**",
            description=(
                f"{mention_user(members[0], player1_id)} ({player1_elo_change_str}) vs. "
                f"{mention_user(members[1], player2_id)} ({player2_elo_change_str})\n\n{match_info}"
            ),
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        try:
            await self._publish_result_embeds_locked(mongo_match, match_thread, finished_embed)

            for old_msg_id in dict.fromkeys(mongo_match.old_msgs):
                if old_msg_id:
                    try:
                        old_msg = await match_thread.fetch_message(old_msg_id)
                        if old_msg:
                            await mark_match_message_ended(old_msg)
                    except ipy.errors.NotFound:
                        pass
        except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
            logging.exception("Result publication incomplete for match %s", token)
            await send_interaction_message(
                f"{IMPORTANT_EMOJI} The results are recorded, but the match messages could not all be updated. "
                "Publication is incomplete and will be retried."
            )
            return False

        await remove_helper_task(mongo_match.thread, mongo_match.participants[0].user_id)
        await remove_helper_task(mongo_match.thread, mongo_match.participants[1].user_id)

        await fetch_user_match_id_choices(self.bot, guild, player1_id)
        await fetch_user_match_id_choices(self.bot, guild, player2_id)
        await autoqueue_completed_match_participants(self.bot, token, (player1_id, player2_id))
        await publish_result_operation(mongo_match)
        rename_task = schedule_channel_name_edit(match_thread, "🔴")
        self._pending_match_thread_renames[token] = rename_task
        rename_task.add_done_callback(lambda _: self._pending_match_thread_renames.pop(token, None))
        await send_interaction_message(
            f"{SUCCESS_EMOJI} The match results are submitted and recorded.\n\n"
            f"{IMPORTANT_EMOJI} *Please kick the players out of the clan!*"
        )
        return True

    @base.subcommand(sub_cmd_name="submit", sub_cmd_description="Submit a match result manually")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="player1",
        description="Player 1 of the match",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player2",
        description="Player 2 of the match",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player1_result",
        description="Player 1's result",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="player2_result",
        description="Player 2's result",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="district1",
        description="First district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
            ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
            ipy.SlashCommandChoice(name="Wizard Valley", value="Wizard Valley"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="district2",
        description="Second district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Balloon Lagoon", value="Balloon Lagoon"),
            ipy.SlashCommandChoice(name="Builder's Workshop", value="Builder's Workshop"),
            ipy.SlashCommandChoice(name="Golem Quarry", value="Golem Quarry")
        ],
        required=True
    )
    @ipy.slash_option(
        name="district3",
        description="Third district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines"),
            ipy.SlashCommandChoice(name="Capital Peak", value="Capital Peak"),
        ],
        required=True
    )
    async def match_submit(self, ctx: ipy.SlashContext, player1: ipy.User, player2: ipy.User, player1_result: str,
                           player2_result: str, district1: str, district2: str, district3: str):
        if getattr(getattr(ctx.channel, "parent_channel", None), "id", None) == FINAL_CHANNEL:
            await ctx.send(f"{ERROR_EMOJI} Use Submit Result or `/final match result` for all seven Final districts.",
                           ephemeral=True)
            return
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        player_docs: list[Player] = []
        for player in [player1, player2]:
            player_doc = await Player.find_one(Player.user_id == str(player.id))
            if not player_doc:
                await ctx.send(f"{ERROR_EMOJI} {player.mention} is not registered in the elo system.",
                               ephemeral=True)

                return
            player_docs.append(player_doc)

        player1 = str(player1.id)
        player2 = str(player2.id)
        response = {
            "player1": player1_result.replace("/", "\n"),
            "player2": player2_result.replace("/", "\n"),
        }
        districts = [district1, district2, district3]
        district_options = get_district_options(
            all(player.elo < 1200 for player in player_docs),
            all(player.capital_peak_enabled for player in player_docs),
        )
        if any(district not in options for district, options in zip(districts, district_options)):
            await ctx.send(f"{ERROR_EMOJI} One or more districts are not available for these players.", ephemeral=True)
            return

        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player1's result is invalid.", ephemeral=True)

            return

        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player2's result is invalid.", ephemeral=True)

            return

        key = manual_result_key(str(ctx.author.id), [player1, player2], response, districts)
        lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(key, anyio.Lock())
        async with lock:
            try:
                async with PLAYER_DATA_BOUNDARY.write(data_generation):
                    try:
                        await record_match(ctx.guild, self.bot, player1, player2, response, districts, manual_key=key)
                    except ManualResultConfirmationUnknown as error:
                        logging.exception("Unable to confirm a completed manual result")
                        await ctx.send(
                            f"{ERROR_EMOJI} The match data was recorded as ID `{error.record_id}`, but I could not "
                            "confirm its final status. Check that record before submitting another match.",
                            ephemeral=True,
                        )
                        return
                    except ResultOperationConflict as error:
                        await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                        return
                    except (PyMongoError, ipy.errors.HTTPException) as error:
                        logging.exception("Manual result submission did not finish: %s", type(error).__name__)
                        await ctx.send(
                            f"{ERROR_EMOJI} The submission did not finish. Retry with the same players, scores, "
                            "and districts to continue it safely.",
                            ephemeral=True,
                        )
                        return
                    await ctx.send(
                        f"{SUCCESS_EMOJI} The match between <@{player1}> and <@{player2}> is submitted.",
                        ephemeral=True,
                    )
            except PlayerDataReset as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @base.subcommand(sub_cmd_name="resubmit", sub_cmd_description="Resubmit a match result")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    async def match_resubmit(self, ctx: ipy.SlashContext):
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True,
            )
            return

        if is_final_match(match):
            from elobot.features.finals.result_ui import show_source_result_form
            from elobot.features.finals.types import FinalError
            try:
                await show_source_result_form(ctx, match, correction=True)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        if "🔴" not in ctx.channel.name:
            await ctx.send(f"{ERROR_EMOJI} The match is not yet finished, so why are you using this command?",
                           ephemeral=True)

            return

        if match.is_autowin:
            await ctx.send(f"{ERROR_EMOJI} An auto-win has already been issued to this match!",
                           ephemeral=True)

            return

        if str(ctx.author.id) in match and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)

            return

        p1_id = match.participants[0].user_id
        p2_id = match.participants[1].user_id
        player1 = await self.bot.fetch_user(p1_id)
        player2 = await self.bot.fetch_user(p2_id)

        value1 = "\n".join(match.participants[0].result) if all(match.participants[0].result) else None
        value2 = "\n".join(match.participants[1].result) if all(match.participants[1].result) else None

        modal = ipy.Modal(
            ipy.ParagraphText(
                label=f"{player1}'s Result",
                max_length=100,
                value=value1,
                custom_id="player1"
            ),
            ipy.ParagraphText(
                label=f"{player2}'s Result",
                max_length=100,
                value=value2,
                custom_id="player2"
            ),
            title="Result Submission",
            custom_id=f"resubmit_modal|{match.token}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^resubmit_modal\|\w+$"))
    async def resubmit_modal(self, ctx: ipy.ModalContext, **response):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        _, token = ctx.custom_id.split("|")
        lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(token, anyio.Lock())
        try:
            async with lock, PLAYER_DATA_BOUNDARY.write(data_generation):
                await self._resubmit_modal_locked(ctx, token, response)
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _resubmit_modal_locked(self, ctx: ipy.ModalContext, token: str, response: dict[str, str]) -> None:
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread.",
                           ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send(f"{ERROR_EMOJI} Open `/match resubmit` again to use the current Final correction form.",
                           ephemeral=True)
            return
        if match.result_operation and match.result_operation.cancelled:
            await ctx.send(f"{ERROR_EMOJI} {result_input_error(match)}", ephemeral=True)
            return

        # validate formats
        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player1's result is invalid.", ephemeral=True)
            return
        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player2's result is invalid.", ephemeral=True)
            return

        # DB/JSON fetches
        p1, p2 = match.participants[0], match.participants[1]
        player1_id, player2_id = p1.user_id, p2.user_id
        
        player1_doc = await Player.find_one(Player.user_id == player1_id, fetch_links=True)
        player2_doc = await Player.find_one(Player.user_id == player2_id, fetch_links=True)
        
        # Find previously recorded match
        previous_match = None
        if match.result_operation and not match.result_operation.completed and match.result_operation.previous_record:
            previous_match = RecordedMatch.model_validate(match.result_operation.previous_record.model_dump())
        elif match.recorded_match_id:
            previous_match = await RecordedMatch.find_one(RecordedMatch.id == match.recorded_match_id)
        if not previous_match and player1_doc and player1_doc.matches:
            # Check recent matches (last 5)
            sorted_matches = sorted(player1_doc.matches, key=lambda m: int(m.id))
            for m in reversed(sorted_matches[-5:]):
                # Check if opponent matches
                if player2_id not in m.players:
                    continue
                
                # Check districts
                p1_stats = m.players[player1_id]
                if list(p1_stats.districts.keys()) != match.final_districts:
                    continue
                
                # Check results (stars/destruction) against the OLD results in match.participants
                # p1.result is list of strings "stars.destruction"
                if not p1.result or not p2.result:
                    continue
                    
                p1_res_stars = sum(map(get_stars, p1.result))
                p1_res_dest = sum(map(get_destruction, p1.result))
                
                if p1_stats.stars != p1_res_stars or p1_stats.destruction != p1_res_dest:
                    continue
                    
                # If we are here, it's likely the match
                previous_match = m
                break
        
        if not previous_match:
            await ctx.send(f"{ERROR_EMOJI} Could not find the previous match record to revert.", ephemeral=True)
            return

        try:
            await begin_result_operation(match, response, previous_match)
        except ResultOperationConflict as error:
            await ctx.send(f"{ERROR_EMOJI} {error.message}", ephemeral=True)
            return
        if match.result_operation.completed:
            player1_elo_change_str, player2_elo_change_str = await completed_result_changes(match)
        else:
            await rollback_result_operation(match)
            player1_elo_change_str, player2_elo_change_str = await record_match(
                ctx.guild, self.bot, player1_id, player2_id, response, match.final_districts, token
            )
            match = await Match.find_one(Match.token == token)
            p1, p2 = match.participants[:2]
            player_results = {
                player1_id: p1.result,
                player2_id: p2.result,
            }
            await record_base_stats(player_results, match)
            await complete_result_operation(match)

        await invite_eligible_players(self.bot, (player1_id, player2_id), pending_only=True)
        await forget_replaced_match_choice(self.bot, match)
        await clear_match_base_stores(match, reset_stored_flags=True, bot=self.bot, reason="finished")
        await free_match_clans(match)

        # update match result embed
        new_match = match.result_operation.record
        
        match_info = ""
        p1_new_stats = new_match.players[player1_id]
        p2_new_stats = new_match.players[player2_id]
        
        district_keys = list(p1_new_stats.districts.keys())
        for district in district_keys:
            match_info += (
                f"{DISTRICT_EMOJI} **{district}**\n"
                f"{p1_new_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p1_new_stats.districts[district].destruction}% - "
                f"{p2_new_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p2_new_stats.districts[district].destruction}%\n\n"
            )

        match_info += (
            f"**Final Score**\n"
            f"{p1_new_stats.stars}{STAR_EMOJI} {p1_new_stats.destruction}% - "
            f"{p2_new_stats.stars}{STAR_EMOJI} {p2_new_stats.destruction}%"
        )

        guild = await member_guild(ctx)
        members = await asyncio.gather(guild.fetch_member(player1_id), guild.fetch_member(player2_id))
        finished_embed = ipy.Embed(
            title="**Match has Ended**",
            description=f"{mention_user(members[0], player1_id)} ({player1_elo_change_str}) vs. "
                        f"{mention_user(members[1], player2_id)} ({player2_elo_change_str})\n\n"
                        f"{match_info}",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=RED_COLOR,
        )

        try:
            async with match_message_lock(match.thread):
                match_thread = await self.bot.fetch_channel(int(match.thread))
                await self._publish_result_embeds_locked(match, match_thread, finished_embed)
                await publish_result_operation(match)
        except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
            logging.exception("Corrected result publication incomplete for match %s", token)
            await ctx.send(
                f"{IMPORTANT_EMOJI} The corrected results are recorded, but the match messages could not all "
                "be updated. Publication is incomplete and will be retried.", ephemeral=True,
            )
            return
        await ctx.send(
            f"{SUCCESS_EMOJI} The match results are resubmitted, please use `/player matches ...` "
            f"and `/player profile` commands to double check if everything was done correctly.",
            ephemeral=True,
        )

    async def _publish_result_embeds_locked(
            self, match: Match, match_thread: ipy.GuildPublicThread, finished_embed: ipy.Embed,
    ) -> None:
        operation = match.result_operation
        if not operation or not operation.completed:
            raise ResultOperationConflict(match.token)
        for target in ("summary", "public"):
            channel = match_thread if target == "summary" else await self.bot.fetch_channel(MATCH_CHANNEL)
            message_id = operation.summary_message_id if target == "summary" else operation.public_message_id
            embed = copy.deepcopy(finished_embed)
            embed.color = COLOR if target == "summary" else RED_COLOR
            message = None
            if target == "public" and not message_id and not match.is_war and match.msg:
                try:
                    message = await channel.fetch_message(int(match.msg))
                except ipy.errors.NotFound:
                    pass
                else:
                    await bind_result_message(match, int(message.id), target="public")
                    message_id = int(message.id)
            if message_id:
                try:
                    if message is None:
                        message = await channel.fetch_message(int(message_id))
                    if target == "public" and not match.is_war:
                        await edit_channel_message(message, embed=embed, components=[])
                    else:
                        await edit_channel_message(message, embed=embed)
                except ipy.errors.HTTPException:
                    if message is not None:
                        await _mark_result_publication_incomplete(message)
                    raise
                continue
            content = (
                f"<@{match.participants[0].user_id}> and <@{match.participants[1].user_id}>, "
                "please double check if the match results are correct."
            ) if target == "summary" else await choose_tip()
            components: list[ipy.ActionRow] = []
            if target == "summary":
                delete_button = ipy.Button(
                    style=ipy.ButtonStyle.DANGER, label="Delete Thread",
                    custom_id=f"delete_thread|{match.token}", emoji=CROSS_EMOJI,
                )
                components = [ipy.ActionRow(delete_button)]
            with anyio.CancelScope(shield=True):
                message = await channel.send(content, embed=embed, components=components)
                try:
                    await bind_result_message(match, int(message.id), target=target)
                except (PyMongoError, ResultOperationConflict):
                    await _mark_result_publication_incomplete(message)
                    raise

    @base.subcommand(sub_cmd_name="autowin", sub_cmd_description="Auto-win for a selected player")
    @ipy.max_concurrency(ipy.Buckets.CHANNEL, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="winning_user",
        description="The user that will get an auto-win",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="reason",
        description="The reason for the auto-win",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def match_autowin(self, ctx: ipy.SlashContext, winning_user: ipy.User, reason: str):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        try:
            async with PLAYER_DATA_BOUNDARY.write(data_generation):
                # fetch the match by current thread channel
                match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
                if not match:
                    await ctx.send(
                        f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                        ephemeral=True
                    )
                    return

                if match.autowin_completed or (match.is_autowin and match.autowin_winner_id is None):
                    await ctx.send(f"{ERROR_EMOJI} An auto-win has already been given to this match already.", ephemeral=True)
                    return

                if is_final_match(match):
                    await ctx.send(f"{ERROR_EMOJI} Use `/final match forfeit` to enter zero scores and the advancing player.",
                                   ephemeral=True)
                    return

                p1: Participant = match.participants[0]
                p2: Participant = match.participants[1]
                player1_id, player2_id = p1.user_id, p2.user_id
                match_token = match.token
                participant_ids = {player1_id, player2_id}

                if str(winning_user.id) not in participant_ids:
                    await ctx.send(f"{ERROR_EMOJI} The winning player is not part of this match.", ephemeral=True)
                    return

                match = await claim_autowin(match, str(winning_user.id), str(ctx.author.id), reason)
                if match.result_operation and not match.result_operation.published:
                    await ctx.send(
                        f"{ERROR_EMOJI} Results are already being recorded for this match. The auto-win was not applied.",
                        ephemeral=True,
                    )
                    return
                if match.autowin_winner_id is None:
                    await ctx.send(
                        f"{ERROR_EMOJI} The match changed before the auto-win was recorded. Please retry.", ephemeral=True,
                    )
                    return
                if match.autowin_winner_id != str(winning_user.id):
                    await ctx.send(
                        f"{ERROR_EMOJI} This match already has an auto-win decision for <@{match.autowin_winner_id}>. "
                        "Retry with that winner to finish the interrupted operation.",
                        ephemeral=True,
                    )
                    return
                guild = await member_guild(ctx)
                actor = ctx.author if match.autowin_actor_id == str(ctx.author.id) else (
                    await guild.fetch_member(match.autowin_actor_id)
                )
                decision_reason = f"{mention_user(actor, match.autowin_actor_id)}: {match.autowin_reason}"

                # determine losing user
                losing_id = player2_id if str(winning_user.id) == player1_id else player1_id
                losing_user = await self.bot.fetch_user(int(losing_id)) if str(
                    losing_id).isdigit() else await self.bot.fetch_user(losing_id)

                winning_elo = await apply_autowin_player(str(winning_user.id), match, 5)
                losing_elo = await apply_autowin_player(losing_id, match, -35)
                await mark_autowin(match)
                for player, participant in enumerate(match.participants):
                    if participant.base_store is not None or participant.opponent_bases_stored:
                        if not await base_store_publication.mark_store_reserved(
                                self.bot, match, player,
                        ):
                            await ctx.send(
                                f"{ERROR_EMOJI} The auto-win rating changes are saved, but the stored-base message "
                                "could not be updated. Its slot remains reserved. Please retry, or use **Undo** "
                                "before retrying.",
                                ephemeral=True,
                            )
                            return
                reservation_notice = (
                    "\n\nStored-base slots remain reserved until you use **Undo**, the raid weekend ends, "
                    "or this thread is deleted."
                    if has_reserved_match_slots(match) else ""
                )
                await free_match_clans(match)

                hosts = match.hosts
                times = match.times
                single_host = hosts[0] if hosts else None
                single_time = int(match.matched_time or times[0] or 0)
                order_ids = [player1_id, player2_id]
                if not match.is_war:
                    if single_host and single_time:
                        await award_helper_once(
                            single_host, f"autowin:{match.id}:host:0", 3,
                            HelperHistory(time=single_time, player1=player1_id, player2=player2_id),
                        )
                else:
                    for i in range(2):
                        host_id = hosts[i] if i < len(hosts) else None
                        host_time = times[i] if i < len(times) else None
                        if host_id and host_time:
                            await award_helper_once(
                                host_id, f"autowin:{match.id}:host:{i}", 0.5,
                                HelperHistory(time=int(host_time), player1=order_ids[i]),
                            )
                        submitters = match.submitters or [None, None]
                        submitter_id = submitters[i] if i < len(submitters) else None
                        if submitter_id:
                            await award_helper_once(submitter_id, f"autowin:{match.id}:submitter:{i}", 0.5)

                await remove_helper_task(match.thread, player1_id)
                await remove_helper_task(match.thread, player2_id)

                # DMs to players
                winning_embed = ipy.Embed(
                    title=f"**Auto-win Against {losing_user}**",
                    description=f"**Elo Change:** {WIN_EMOJI} {winning_elo - 5} → "
                                f"{winning_elo}\n\n"
                                f"*Note: auto-wins do not affect your winrate.*",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                try:
                    await winning_user.send(embed=winning_embed)
                except ipy.errors.Forbidden:
                    pass

                losing_embed = ipy.Embed(
                    title=f"**Penalty in Match Against {losing_user}**",
                    description=f"**Elo Change:** {LOSS_EMOJI} {losing_elo + 35} → "
                                f"{losing_elo}\n\n"
                                f"*Note: penalties do not affect your winrate.*",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                try:
                    await losing_user.send(embed=losing_embed)
                except ipy.errors.Forbidden:
                    pass

                match_embed = ipy.Embed(
                    title="**An Auto-Win Has Been Issued**",
                    description=f"{mention_user(winning_user)} is given an auto-win, "
                                f"and {mention_user(losing_user)} is penalized after "
                                f"careful considerations.{reservation_notice}",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                delete_button = ipy.Button(
                    style=ipy.ButtonStyle.DANGER,
                    label="Delete Thread",
                    custom_id=f"delete_thread|{match_token}",
                    emoji=CROSS_EMOJI,
                )
                await ctx.channel.send(
                    embed=match_embed,
                    components=[ipy.ActionRow(delete_button)],
                )

                await autoqueue_completed_match_participants(self.bot, match_token, (player1_id, player2_id))
                await mark_autowin(match, completed=True)
                await ctx.send(
                    f"{SUCCESS_EMOJI} An auto-win has been given to {winning_user.mention}, "
                    f"and {losing_user.mention} is penalized.{reservation_notice}",
                    ephemeral=True,
                )
                schedule_channel_name_edit(ctx.channel, "🔴")
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

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

import asyncio
import copy
import logging
import re

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

import elo_bot.features.base_store.publication as base_store_publication
import elo_bot.features.hosting.war.result_store as result_store
from elo_bot.config.emojis import (
    CROSS_EMOJI,
    DISTRICT_EMOJI,
    ERROR_EMOJI,
    IMPORTANT_EMOJI,
    LOSS_EMOJI,
    STAR_EMOJI,
    SUCCESS_EMOJI,
    WIN_EMOJI,
)
from elo_bot.config.ids import (
    FINAL_CHANNEL,
    LVL1_ROLES,
    LVL2_ROLES,
    MAIN_GUILD,
    MATCH_CHANNEL,
    MODERATOR_ROLE,
    SEN_ROLE,
    STAFF_ROLE,
)
from elo_bot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    RED_COLOR,
)
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.db import HelperHistory, Match, Participant, Player, RecordedMatch
from elo_bot.discord.channel_edits import schedule_channel_name_edit
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.features.base_store.match_adapters import clear_match_base_stores
from elo_bot.extensions.match.groups import base
from elo_bot.features.players.match_choices import fetch_user_match_id_choices, forget_replaced_match_choice
from elo_bot.core.finals import is_final_match
from elo_bot.features.hosting.self_hosted.invitations import invite_eligible_players
from elo_bot.features.matches.cleanup import has_reserved_match_slots
from elo_bot.features.matches.lifecycle import get_district_options
from elo_bot.features.matches.message_lock import match_message_lock
from elo_bot.features.matches.messages import (
    mark_match_message_ended,
)
from elo_bot.features.matches.resources import free_match_clans, remove_helper_task
from elo_bot.features.matches.tips import choose_tip
from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from elo_bot.features.queue.matching import autoqueue_completed_match_participants
from elo_bot.features.results.autowin import apply_autowin_player, claim_autowin, mark_autowin
from elo_bot.features.results.helper_credits import award_helper_once
from elo_bot.features.results.manual import (
    ManualResultConfirmationUnknown,
    manual_result_key,
)
from elo_bot.features.results.operations import (
    ResultOperationConflict,
    begin_result_operation,
    bind_result_message,
    complete_result_operation,
    completed_result_changes,
    publish_result_operation,
    result_input_error,
    rollback_result_operation,
)
from elo_bot.features.results.recording import _mark_result_publication_incomplete, record_base_stats, record_match
from elo_bot.core.match_results import is_valid_complete_match_result
from elo_bot.features.finals.result_ui import show_source_result_form
from elo_bot.core.finals import FinalError
from elo_bot.db.season_models import EloFinal
from elo_bot.features.finals.results import FinalResultSubmission, automatic_operation_key, submit_result
from elo_bot.features.finals.recovery import recover_final
from beanie import PydanticObjectId
from pydantic import ValidationError


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


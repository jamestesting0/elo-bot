import asyncio
import logging
import re
from datetime import datetime

import anyio
import interactions as ipy
from interactions import utils
from pymongo.errors import PyMongoError

import elobot.features.hosting.war.result_store as result_store
from elobot.config.emojis import (
    CROSS_EMOJI,
    ERROR_EMOJI,
    IMPORTANT_EMOJI,
    LOADING_EMOJI,
    SUCCESS_EMOJI,
    TICK_EMOJI,
)
from elobot.config.ids import (
    COMMAND_CHANNEL,
    LVL2_ROLES,
    MATCH_CHANNEL,
    STAFF_ROLE,
)
from elobot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
)
from elobot.db import Match, Participant, Player
from elobot.discord.members import member_guild
from elobot.discord.mentions import mention_user
from elobot.features.base_store.match_adapters import clear_match_base_stores
from elobot.extensions.match.groups import base
from elobot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elobot.core.finals import is_final_match
from elobot.features.matches.cleanup import (
    delete_unreserved_match,
    has_pending_match_operation,
    has_reserved_match_slots,
)
from elobot.features.matches.lifecycle import (
    MatchChannelUnavailable,
    build_district_confirmation_modal,
    cancel_match,
    confirm_match,
    get_district_options,
)
from elobot.features.matches.message_lock import match_message_lock
from elobot.features.matches.messages import (
    match_message_embeds,
)
from elobot.features.matches.recovery import repair_match_messages
from elobot.features.matches.resources import append_helper_task, free_match_clans, remove_helper_task
from elobot.features.matches.thread_messages import (
    ThreadMessageBindingError,
)
from elobot.features.queue.policy import QUEUE_OPERATION_LOCK
from elobot.features.queue.state import remove_matched_queue_attempts
from elobot.features.results.operations import (
    ResultOperationConflict,
)


class MatchLifecycleCommands:
    @ipy.component_callback(re.compile(r"^district_select\|\w+\|\d+$"))
    async def district_select(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if match is not None and any(p.queue_attempt_id for p in match.participants):
            async with QUEUE_OPERATION_LOCK:
                match = await Match.find_one(Match.token == token)
                await self._select_match_district(ctx, match, index)
        else:
            await self._select_match_district(ctx, match, index)

    async def _select_match_district(self, ctx: ipy.ComponentContext, match: Match | None, index: str) -> None:
        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)

            return

        if match.thread:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} The match is already in process, is too late to select districts now.",
                           ephemeral=True)

            return

        district_index = int(index)
        district_options = get_district_options(
            match.wizard_valley_available,
            match.capital_peak_available,
        )
        selected_district = ctx.values[0]
        if district_index >= len(district_options) or selected_district not in district_options[district_index]:
            await ctx.send(f"{ERROR_EMOJI} That district is not available for this match.", ephemeral=True)
            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        if participant.queue_districts_locked:
            await ctx.send(f"{ERROR_EMOJI} Your districts are already locked for this match.", ephemeral=True)
            return
        participant.districts[district_index] = selected_district
        await match.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have selected **{selected_district.lower()}**", ephemeral=True)

    @ipy.component_callback(re.compile(r"^confirm_occupied_match\|\w+$"))
    async def confirm_occupied_match_button(self, ctx: ipy.ComponentContext):
        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send("Final districts are fixed. A manager can resume its thread with `/final round start`.", ephemeral=True)
            return

        await ctx.send_modal(build_district_confirmation_modal(
            token,
            match.wizard_valley_available,
            match.capital_peak_available,
        ))

    @ipy.modal_callback(re.compile(r"^confirm_occupied_match_modal\|\w+$"))
    async def confirm_occupied_match_modal(self, ctx: ipy.ModalContext, **response):
        async with QUEUE_OPERATION_LOCK:
            await self._confirm_occupied_match_modal(ctx, response)

    async def _confirm_occupied_match_modal(self, ctx: ipy.ModalContext, response) -> None:
        await ctx.defer(ephemeral=True)

        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send("Final districts are fixed. A manager can resume its thread with `/final round start`.", ephemeral=True)
            return

        selected_districts = [
            response[f"inline_district|{token}|{index}"][0]
            for index in range(3)
        ]
        district_options = get_district_options(
            match.wizard_valley_available,
            match.capital_peak_available,
        )
        if any(district not in options for district, options in zip(selected_districts, district_options)):
            await ctx.send(f"{ERROR_EMOJI} One or more districts are not available for this match.", ephemeral=True)
            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))
        participant.districts = selected_districts
        await self._confirm_match_participant(ctx, match, participant, opponent, token)

    @ipy.component_callback(re.compile(r"^match_confirm\|\w+$"))
    async def match_confirm_button(self, ctx: ipy.ComponentContext):
        async with QUEUE_OPERATION_LOCK:
            await self._match_confirm_button(ctx)

    async def _match_confirm_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)

            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))
        if not participant.districts[0]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 1st select menu.")

            return

        if not participant.districts[1]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 2nd select menu.")

            return

        if not participant.districts[2]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 3rd select menu.")

            return

        await self._confirm_match_participant(ctx, match, participant, opponent, token, update_message=True)

    async def _confirm_match_participant(
            self, ctx, match: Match, participant: Participant, opponent: Participant,
            token: str, update_message: bool = False):
        if participant.confirmed:
            await ctx.send(f"{ERROR_EMOJI} You have already confirmed this match.", ephemeral=True)
            return

        # confirm this participant and persist
        participant.confirmed = True
        if match.confirmation_tracking:
            participant.confirmed_at = datetime.now().timestamp()
            participant.confirmation_method = "manual"
        await match.save_changes()

        if update_message:
            submit_bases_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Submit Bases",
                emoji="📥",
                custom_id=f"submit_bases|{token}",
            )

            ctx.message.embeds[0].title = "**Match Confirmed**"
            ctx.message.embeds[0].description = (
                "If you wish to submit your own bases instead of using helpers' bases, "
                "please click on the \"Submit Bases\" button!"
            )
            guild = await member_guild(ctx)
            member = await guild.fetch_member(opponent.user_id)
            ctx.message.embeds[0].add_field(name="**Opponent**", value=mention_user(member, opponent.user_id))
            await ctx.message.edit(
                content="", components=submit_bases_button, embeds=match_message_embeds(ctx.message.embeds),
            )

        # if opponent not confirmed yet → wait
        if not all(p.confirmed for p in match.participants):
            await ctx.send(f"{SUCCESS_EMOJI} The match is **confirmed**, now waiting for your opponent to confirm!")
            return

        # both confirmed → create thread
        try:
            thread_id = await confirm_match(self.bot, token)
        except (ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError, MatchChannelUnavailable):
            logging.exception("Match %s confirmed but its thread publication is incomplete", token)
            await ctx.send(
                f"{IMPORTANT_EMOJI} The match is confirmed. Its thread publication is incomplete and will be retried.",
                ephemeral=True,
            )
            return
        await ctx.send(
            f"{SUCCESS_EMOJI} The match is **confirmed**, please determine the match "
            f"timing with your opponent in <#{thread_id}>",
            ephemeral=True,
        )

    @ipy.component_callback(re.compile(r"^delete_thread\|\w+$"))
    async def delete_thread_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        user_roles = {int(role.id) for role in ctx.author.roles}
        if not set(LVL2_ROLES).intersection(user_roles):
            await ctx.send(f"{ERROR_EMOJI} You are not allowed to delete the thread.", ephemeral=True)

            return

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id="cancel",
            emoji=CROSS_EMOJI
        )
        confirm_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Confirm",
            custom_id="confirm",
            emoji=TICK_EMOJI
        )

        button_actionrow = ipy.ActionRow(cancel_button, confirm_button)

        msg = await ctx.send("Are you sure that you would like to **delete** the thread? If the match is not "
                             "finished, deleting it will invalidate the match.\n\n"
                             "*Note: you cannot undo this action.*",
                             components=button_actionrow, ephemeral=True)

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(components=button_actionrow, messages=int(msg.id), timeout=180)).ctx
        except asyncio.TimeoutError:
            await ctx.delete(msg)

            return

        if res.custom_id == "confirm":
            match = await Match.find_one(Match.thread == str(ctx.channel.id))
            lock_key = match.token if match else f"thread:{ctx.channel.id}"
            lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(lock_key, anyio.Lock())
            async with lock:
                match = await Match.find_one(Match.thread == str(ctx.channel.id))
                if match and has_pending_match_operation(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} A result or auto-win submission is unfinished. "
                        "Complete it before deleting this thread.", ephemeral=True,
                    )
                    return
                if match and has_reserved_match_slots(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} This match still has reserved bases. "
                        "Finish the match or use Undo before deleting this thread.", ephemeral=True,
                    )
                    return
                await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

                await ctx.send(f"{LOADING_EMOJI} The thread will be deleted in 5 seconds.", ephemeral=True)
                await asyncio.sleep(5)

                async with BASE_STORE_RESERVATION_LOCK:
                    match = await Match.find_one(Match.thread == str(ctx.channel.id))
                    if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                        await ctx.send(
                            f"{ERROR_EMOJI} This match now has reserved bases or an unfinished submission. "
                            "Finish the match, or use Undo for its reservation, before deleting this thread.",
                            ephemeral=True,
                        )
                        return
                    await ctx.channel.delete(reason=f"Deleted by: {ctx.author} ({ctx.author.id})")
                    if match and not await delete_unreserved_match(match):
                        logging.warning("Match %s changed before deleted-thread cleanup could finish", match.token)
                        return
                if match:
                    await free_match_clans(match)
                    for participant in match.participants:
                        await remove_helper_task(match.thread, participant.user_id)

        if res.custom_id == "cancel":
            await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

            await ctx.send(f"{SUCCESS_EMOJI} The action has been canceled.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^reveal_opponent\|\w+$"))
    async def reveal_opponent_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, token = ctx.custom_id.split("|", 1)
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match has been canceled.", ephemeral=True)

            return

        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))

        submit_bases_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Bases",
            emoji="📥",
            custom_id=f"submit_bases|{token}",
        )

        guild = await member_guild(ctx)
        member = await guild.fetch_member(opponent.user_id)
        ctx.message.embeds[0].add_field(name="**Opponent**", value=mention_user(member, opponent.user_id))
        await ctx.message.edit(components=submit_bases_button, embeds=match_message_embeds(ctx.message.embeds))

        await ctx.send(f"{SUCCESS_EMOJI} Your opponent is now revealed, and you cannot cancel the match anymore.",
                       ephemeral=True)

    @ipy.component_callback(re.compile(r"^match_cancel\|\w+$"))
    async def match_cancel_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        _, token = ctx.custom_id.split("|")

        await self._cancel_match(ctx, token)

    async def _cancel_match(self, ctx: ipy.ComponentContext, token: str) -> None:
        # DB/JSON fetches
        match = await Match.find_one(Match.token == token)
        
        author_id = str(ctx.author.id)
        author_player = await Player.find_one(Player.user_id == author_id)

        # cooldown: once per 7 days
        last_cancel_ts = author_player.last_cancel if author_player else None
        if last_cancel_ts:
            last_cancel = datetime.fromtimestamp(last_cancel_ts)
            if (datetime.now() - last_cancel).days <= 7:
                await ctx.send(f"{ERROR_EMOJI} You can only cancel a match once every week.", ephemeral=True)
                return

        # match must exist and not have started
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components), context=ctx)
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is invalidated.", ephemeral=True)
            return

        if match.thread:
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return

        # confirm/cancel UI
        cancel_btn = ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel", custom_id="cancel", emoji=CROSS_EMOJI)
        confirm_btn = ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Confirm", custom_id="confirm", emoji=TICK_EMOJI)
        row = ipy.ActionRow(cancel_btn, confirm_btn)

        msg = await ctx.send(
            "Are you sure that you would like to **cancel** the match? "
            "You may only perform this action once in a week.\n\n"
            "*Note: you cannot undo this action.*",
            components=row, ephemeral=True
        )

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(components=row, messages=int(msg.id), timeout=180)
            ).ctx
        except asyncio.TimeoutError:
            await ctx.delete(ctx.message)
            return

        if res.custom_id == "confirm":
            await res.defer(ephemeral=True)
            async with QUEUE_OPERATION_LOCK:
                current_match = await Match.find_one(Match.token == token)
                deleted = False
                if current_match is not None and current_match.id == match.id:
                    await remove_matched_queue_attempts(match)
                    deleted = await delete_unreserved_match(match, pre_thread=True)
            if not deleted:
                await res.send(
                    f"{ERROR_EMOJI} The match changed while cancellation was being confirmed. It was not canceled.",
                    ephemeral=True,
                )
                return

            # figure out players (assumes exactly two participants)
            p1_id = match.participants[0].user_id if match.participants else None
            p2_id = match.participants[1].user_id if len(match.participants) > 1 else None

            forfeited_id = author_id
            innocent_id = p2_id if p1_id == forfeited_id else p1_id

            try:
                if innocent_id:
                    innocent_player = await Player.find_one(Player.user_id == innocent_id)
                    if innocent_player:
                        innocent_player.priority = True
                        await innocent_player.save_changes()

                if author_player:
                    author_player.last_cancel = datetime.now().timestamp()
                    await author_player.save_changes()
            except PyMongoError:
                logging.exception("Canceled match %s has incomplete player bookkeeping", token)
                await res.send(
                    f"{ERROR_EMOJI} The match was removed, but saving cancellation cooldown or opponent priority failed. "
                    "Please contact staff.", ephemeral=True,
                )
                return

            # DM notifications (best-effort)
            forfeited_user = await self.bot.fetch_user(forfeited_id)
            innocent_user = await self.bot.fetch_user(innocent_id)

            if innocent_user:
                try:
                    await innocent_user.send(
                        f"{IMPORTANT_EMOJI} Unfortunately, "
                        f"{forfeited_user.mention if forfeited_user else 'the opponent'} "
                        f"has decided to **cancel** the match against you. Please find a new match. "
                        f"If this happens repeatedly, please report it to staff!"
                    )
                except ipy.errors.Forbidden:
                    pass

            if forfeited_user:
                try:
                    await forfeited_user.send(
                        f"{IMPORTANT_EMOJI} The match is **canceled**. "
                        f"Next time please make sure your schedule is free when finding a match!"
                    )
                except ipy.errors.Forbidden:
                    pass

            # disable buttons in both places
            for actionrow in ctx.message.components:
                for component in actionrow.components:
                    component.disabled = True
            await ctx.message.edit(components=ctx.message.components, context=ctx)
            await ctx.edit(msg.id, components=utils.misc_utils.disable_components(row))

            await res.send(
                f"{SUCCESS_EMOJI} Match successfully canceled. "
                f"You may only cancel once per week—please ensure your schedule is clear next time."
            )

        if res.custom_id == "cancel":
            await ctx.edit(msg.id, components=utils.misc_utils.disable_components(row))
            await res.send(f"{SUCCESS_EMOJI} The action has been canceled.", ephemeral=True)

    @base.subcommand(sub_cmd_name="list", sub_cmd_description="List the upcoming matches")
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_list(self, ctx: ipy.SlashContext, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        guild = await member_guild(ctx)
        matches: list[Match] = await Match.find_all().to_list()
        # sort by scheduled time (fall back to 0 if missing)
        matches_sorted = sorted(matches, key=lambda m: m.matched_time or 0)

        now = int(datetime.now().timestamp())
        description = ""

        for match in matches_sorted:
            # skip if scheduled time already passed
            if not match.matched_time or match.matched_time < now:
                continue

            if len(match.participants) < 2:
                continue
            player1 = match.participants[0].user_id
            player2 = match.participants[1].user_id

            timestamp = match.matched_time
            long_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.LongDateTime)
            relative_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.RelativeTime)

            members = await asyncio.gather(guild.fetch_member(player1), guild.fetch_member(player2))
            description += (f"{mention_user(members[0], player1)} vs. {mention_user(members[1], player2)}\n"
                            f"{long_ts}\n{relative_ts}\n")

            # host_clans field replaces old "hostClan"
            if match.host_clans and match.host_clans[0]:
                clan = await self.coc_client.get_clan(match.host_clans[0])
                description += f"[{clan.name} ({clan.tag})]({clan.share_link})\n\n"
            else:
                description += "\n"

        embed = ipy.Embed(
            title="**Upcoming Elo Matches**",
            description=description if description else "No upcoming matches.",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, hidden=hidden)

    @ipy.Task.create(ipy.IntervalTrigger(hours=3))
    async def inactive_threads_delete(self):
        channel = await self.bot.fetch_channel(MATCH_CHANNEL)
        thread_list: ipy.ThreadList = await channel.fetch_all_threads()
        for thread in thread_list.threads:
            if not thread.name.startswith("🔴"):
                continue

            last_messages = await thread.fetch_messages(limit=1)
            if not last_messages:
                continue

            last_message = last_messages[0]
            if datetime.now().timestamp() - last_message.created_at.timestamp() > 24 * 3600:
                match = await Match.find_one(Match.thread == str(thread.id))
                lock_key = match.token if match else f"thread:{thread.id}"
                lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(lock_key, anyio.Lock())
                async with lock:
                    match = await Match.find_one(Match.thread == str(thread.id))
                    if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                        continue
                    current_messages = await thread.fetch_messages(limit=1)
                    if not current_messages or (
                            datetime.now().timestamp() - current_messages[0].created_at.timestamp() <= 24 * 3600
                    ):
                        continue
                    async with BASE_STORE_RESERVATION_LOCK:
                        match = await Match.find_one(Match.thread == str(thread.id))
                        if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                            continue
                        await thread.delete()
                        if match and not await delete_unreserved_match(match):
                            logging.warning("Match %s changed before expired-thread cleanup could finish", match.token)
                            continue
                    if match:
                        await free_match_clans(match)
                        for participant in match.participants:
                            await remove_helper_task(match.thread, participant.user_id)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def auto_match_confirm(self):
        matches = await Match.find({
            "$or": [
                {"thread": None, "is_challenge": False},
                {"thread": {"$ne": None}, "times": [None, None], "old_msgs": [None, None],
                 "is_autowin": {"$ne": True}},
            ],
        }).to_list()

        for match in matches:
            if is_final_match(match):
                continue
            p0, p1 = match.participants[0], match.participants[1]

            if not (p0.confirmed and p1.confirmed):
                async with QUEUE_OPERATION_LOCK:
                    match = await Match.find_one(Match.token == match.token)
                    if not match or match.thread or match.is_challenge or is_final_match(match):
                        continue
                    p0, p1 = match.participants[0], match.participants[1]
                    if p0.confirmed == p1.confirmed:
                        continue
                    now = datetime.now().timestamp()
                    if now - match.matched_time < 43200:
                        continue

                    confirmed, other = (p0, p1) if p0.confirmed else (p1, p0)
                    other.confirmed = True
                    if match.confirmation_tracking:
                        other.confirmed_at = now
                        other.confirmation_method = "automatic"
                    for i in range(3):
                        if not other.districts[i]:
                            other.districts[i] = confirmed.districts[i]
                    try:
                        await match.save_changes()
                    except PyMongoError:
                        logging.exception("Unable to save match confirmation for %s; retrying on the next sweep",
                                          match.token)
                        continue

            try:
                async with match_message_lock(match.thread or match.token):
                    await confirm_match(self.bot, match.token)
            except (ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError, MatchChannelUnavailable):
                logging.exception("Unable to finish match confirmation for %s; retrying on the next sweep", match.token)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def auto_match_cancel(self):
        matches = await Match.find().to_list()
        for match in matches:
            if match.is_challenge or is_final_match(match):
                continue

            p0, p1 = match.participants[0], match.participants[1]

            # skip if someone already confirmed
            if p0.confirmed or p1.confirmed:
                continue

            now = datetime.now().timestamp()
            if now - match.matched_time < 43200:
                continue

            await cancel_match(self.bot, match.token)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def match_confirmation_remind(self):
        matches = await Match.find().to_list()
        now = datetime.now().timestamp()

        for match in matches:
            p0, p1 = match.participants[0], match.participants[1]

            # skip if both confirmed or both unconfirmed (equal states)
            if bool(p0.confirmed) == bool(p1.confirmed):
                continue

            # Keep the reminder at three quarters of the confirmation window.
            if now - match.matched_time < 32400:
                continue

            # find the unconfirmed participant and remind once
            for part in (p0, p1):
                if part.confirmed:
                    continue

                user = await self.bot.fetch_user(int(part.user_id))

                # avoid duplicate reminders if the last DM already warned them
                dm = await user.fetch_dm(force=True)
                last = await dm.fetch_messages(limit=1)
                if last and last[0].content.startswith(f"{IMPORTANT_EMOJI} Please remember"):
                    break

                await user.send(
                    f"{IMPORTANT_EMOJI} Please remember to confirm your match, thank you!\n\n"
                    f"*Please cancel the match, if you do not have time to play.*"
                )
                break

    @ipy.Task.create(ipy.IntervalTrigger(minutes=5))
    async def repair_match_publications(self) -> None:
        async with self._match_publication_repair_lock:
            matches = await Match.find({
                "result_operation.completed": True,
                "result_operation.published": False,
                "result_operation.cancelled": False,
            }).to_list()
            for match in matches:
                try:
                    await self._finalize_match_results(match.token)
                except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
                    logging.exception("Unable to retry result publication for match %s", match.token)
            await repair_match_messages(self.bot)

    @ipy.Task.create(ipy.IntervalTrigger(hours=6))
    async def match_data_clean_up(self):
        matches = await Match.find().to_list()

        for match in matches:
            if not match.thread:
                continue

            try:
                await self.bot.http.get_channel(int(match.thread))
            except ipy.errors.NotFound:
                pass
            except ipy.errors.HTTPException:
                logging.warning("Unable to confirm whether match %s's thread was deleted", match.token, exc_info=True)
                continue
            else:
                continue
            async with BASE_STORE_RESERVATION_LOCK:
                match = await Match.find_one({"_id": match.id, "thread": match.thread})
                if not match:
                    continue
                await clear_match_base_stores(match, reset_stored_flags=True, bot=self.bot, reason="thread_deleted")
                if not await delete_unreserved_match(match):
                    logging.warning("Match %s changed before missing-thread cleanup could finish", match.token)
                    continue
                await free_match_clans(match)

    @ipy.listen(ipy.events.MessageCreate)
    async def on_message_create(self, event: ipy.events.MessageCreate):
        if not isinstance(event.message.channel, ipy.ThreadChannel):
            return

        if int(event.message.channel.parent_channel.id) != MATCH_CHANNEL:
            return

        if f"<@&{STAFF_ROLE}>" not in event.message.content:
            return

        # find the match whose thread is this channel
        match = await Match.find_one(Match.thread == str(event.message.channel.id))
        if match is None:
            return

        author_id = str(event.message.author.id)
        if author_id not in {str(match.participants[0].user_id), str(match.participants[1].user_id)}:
            return

        await append_helper_task(match.thread, author_id)

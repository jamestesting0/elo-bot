import asyncio
import copy
import functools
import re
from dataclasses import replace
from datetime import datetime
from typing import assert_never

import interactions as ipy
from interactions import utils

import elo_bot.features.base_store.publication as base_store_publication
import elo_bot.features.hosting.live.helper_live as helper_live
import elo_bot.features.hosting.live.live_start as live_start
import elo_bot.features.hosting.war.helper_host as helper_host
import elo_bot.features.hosting.war.war_start as war_start
from elo_bot.config.emojis import (
    ERROR_EMOJI,
    SUCCESS_EMOJI,
)
from elo_bot.config.game import (
    DISTRICT_CHOICES,
    HOST_CLAN_CHOICES,
    HOST_CLAN_OPTIONS,
)
from elo_bot.config.ids import (
    ADMIN_ROLE,
    BOT_ID,
    LVL2_ROLES,
    MATCH_CHANNEL,
    MODERATOR_ROLE,
    SEN_ROLE,
    STAFF_ROLE,
)
from elo_bot.config.presentation import (
    COLOR,
    FOOTER_IMAGE,
    FOOTER_TEXT,
    GREEN_COLOR,
)
from elo_bot.core.parsing import extract_integer
from elo_bot.core.raid_weekend import is_raid_weekend
from elo_bot.db import Match, Player
from elo_bot.discord.channel_edits import schedule_channel_name_edit
from elo_bot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import mention_user
from elo_bot.features.base_store.match_adapters import (
    construct_occupied_slots_error, construct_start_slot_instruction, get_start_base_store_slot_availability,
)
from elo_bot.extensions.match.groups import base, war_group
from elo_bot.features.base_store.live import live_stores, ready_live_reservation_id
from elo_bot.features.base_store.models import (
    StoredBaseActionRequest,
    bind_legacy_store_message,
    stored_base_matches,
)
from elo_bot.features.base_store.slots import (
    get_participant_base_store,
)
from elo_bot.features.base_store.start import StoredStartState, held_store, shared_store_clan
from elo_bot.features.finals.matches import schedule_final_match
from elo_bot.core.finals import FinalError, is_final_match
from elo_bot.features.hosting.live.preflight import LivePreflight
from elo_bot.features.hosting.war.confirmation import PendingBaseConfirmation, confirm_pending_opponent_bases
from elo_bot.features.hosting.war.district_selection import match_district_index, started_host_clan_tag
from elo_bot.features.matches.attack_bases import is_missing_scout_view
from elo_bot.features.matches.controls import check_match_control, reject_outdated_match_control, update_match_control
from elo_bot.features.matches.lifecycle import send_warnings
from elo_bot.features.matches.message_lock import coordinated_match_message
from elo_bot.features.matches.messages import (
    MatchMessageState,
    publish_match_message,
)
from elo_bot.features.matches.resources import (
    append_helper_task,
    available_resource_clans_text,
    claim_resource_clan,
    is_clan_available,
    is_resource_clan_locked,
    try_reserve_resource_clan,
    try_reserve_resource_match_side,
)
from elo_bot.features.matches.timing import hosting_started
from elo_bot.features.matches.tips import choose_tip
from elo_bot.core.match_results import is_match_finished


class MatchHostingCommands:
    @ipy.Task.create(ipy.IntervalTrigger(minutes=20))
    async def host_notify(self):
        ts = int(datetime.now().timestamp())
        matches = await Match.find_all().to_list()

        for match in matches:
            if not match.thread:
                continue

            if match.is_war:
                # check both slots
                for i in range(2):
                    if match.times[i] and (match.times[i] - 3600 - ts) < 1200 and not match.hosts[i]:
                        date = datetime.fromtimestamp(match.times[i] - 3600)
                        task = ipy.Task(
                            functools.partial(send_warnings, self.bot, match.token),
                            ipy.DateTrigger(date),
                        )
                        task.start()
            else:
                # non-war: check single time/host
                if match.times[0] and (match.times[0] - 3600 - ts) < 1200 and not match.hosts[0]:
                    date = datetime.fromtimestamp(match.times[0] - 3600)
                    task = ipy.Task(
                        functools.partial(send_warnings, self.bot, match.token),
                        ipy.DateTrigger(date),
                    )
                    task.start()

    async def _start_war(self, ctx: ipy.SlashContext | ipy.ComponentContext, player: int,
                         clan: str, host: ipy.User = None, scout: bool = False, slot: str = None,
                         automatic: bool = False, district: str | None = None,
                         stored_reservation_id: str | None = None):
        await ctx.defer(ephemeral=True)

        if not host:
            host = ctx.author

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match, player=player):
            player_id = match.participants[player].user_id
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        if automatic and district is not None:
            if not match.is_war:
                await ctx.send(f"{ERROR_EMOJI} This is not a war-style match.", ephemeral=True)
                return
            host_clan_tag = started_host_clan_tag(match, player)
            if host_clan_tag is None:
                await ctx.send(
                    f"{ERROR_EMOJI} You can only select a district after this side of the match has started.",
                    ephemeral=True,
                )
                return
            if match_district_index(match, district) is None:
                await ctx.send(
                    f"{ERROR_EMOJI} **{district}** is not one of this match's districts.",
                    ephemeral=True,
                )
                return
            if clan != host_clan_tag:
                await ctx.send(
                    f"{ERROR_EMOJI} This side of the match is hosted in **{host_clan_tag}**. "
                    "Select that clan for a district rerun.",
                    ephemeral=True,
                )
                return
            clan_obj = await self.coc_client.get_clan(host_clan_tag)
            side_reservation = await try_reserve_resource_match_side(str(match.token), player)
            if side_reservation is None:
                await ctx.send(
                    f"{ERROR_EMOJI} A Helper job is already running for this side of the match.",
                    ephemeral=True,
                )
                return
            try:
                await helper_host.host_war_district_automatically(
                    self,
                    ctx,
                    helper_host.AutomaticWarDistrictHost(
                        match=match,
                        player=player,
                        clan=clan_obj,
                        district=district,
                        requested_by=str(ctx.author.id),
                    ),
                )
            finally:
                side_reservation.release()
            return

        stored_clan, stored_slot = get_participant_base_store(match.participants[player])
        if isinstance(ctx, ipy.ComponentContext) and ctx.custom_id.startswith("start_match|"):
            request = StoredBaseActionRequest(match.token, player, stored_reservation_id, int(ctx.message.id))
            store = match.participants[player].base_store
            if (
                    not stored_base_matches(match, request) or (clan, slot) != (stored_clan, stored_slot)
                    or (store is not None and store.ready is False)
            ):
                failure = f"{ERROR_EMOJI} This message no longer has an active stored-base reservation."
                await base_store_publication.mark_store_unavailable(ctx.message)
                await ctx.send(failure, ephemeral=True)
                return
            if not await bind_legacy_store_message(match, player, int(ctx.message.id)):
                failure = f"{ERROR_EMOJI} The stored-base reservation changed; the match was not started."
                await base_store_publication.mark_store_unavailable(ctx.message)
                await ctx.send(failure, ephemeral=True)
                return
        if stored_clan and not isinstance(ctx, ipy.SlashContext):
            clan = stored_clan
            slot = stored_slot

        # prevent re-init if already set up as a live (non-war) match with a time
        if not match.is_war and match.times[0]:
            await ctx.send(f"{ERROR_EMOJI} A live match is already initiated.", ephemeral=True)
            return

        if automatic and hosting_started(match, player):
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return

        if isinstance(ctx, ipy.SlashContext):
            participant = match.participants[player]
            store = held_store(participant)
            if (participant.base_store is not None or participant.opponent_bases_stored) and (
                    store is None or (store.reservation_id is not None and store.message_id is None)
            ):
                await ctx.send(
                    f"{ERROR_EMOJI} Base storage is incomplete. Wait for **Bases Stored**, "
                    "or use its Undo control before storing again.", ephemeral=True,
                )
                return

        if not await confirm_pending_opponent_bases(
                self.bot,
                ctx,
                PendingBaseConfirmation(match=match, player=player),
        ):
            return

        opponent = 0 if player else 1
        opponent_id = match.participants[opponent].user_id

        # optional: warn if opponent’s bases are missing scout image(s)
        if scout:
            district, link = is_missing_scout_view(match, opponent_id)
            if district:
                await ctx.send(
                    f"{ERROR_EMOJI} The opponent's (Player {opponent + 1}) submitted [base]({link}) for **{district}** "
                    f"is missing a scout view. Please manually set the scout view by running "
                    f"{self.bot.mention_command('match base set-image')}!",
                    ephemeral=True
                )

        stored_start = None
        if is_raid_weekend() and isinstance(ctx, ipy.SlashContext):
            store = held_store(match.participants[player])
            if store is None and int(self.bot.user.id) == BOT_ID:
                await ctx.send(
                    f"{ERROR_EMOJI} You can only start a match with ready stored bases during the raid weekend.",
                    ephemeral=True,
                )
                return
            if store is not None:
                if clan != store.clan_tag:
                    await ctx.send(
                        f"{ERROR_EMOJI} This player's bases are stored in **{store.clan_tag}**, **{store.slot}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                slot = store.slot
                stored_start = StoredStartState.capture_stores(match, (player,))

        player_id = match.participants[player].user_id
        if (
            not automatic
            and player_id == str(host.id)
            and str(host.id) != str(self.bot.owner.id)
        ):
            await ctx.send(f"{ERROR_EMOJI} You cannot host your own matches.", ephemeral=True)
            return

        clan_obj = await self.coc_client.get_clan(clan)
        side_reservation = await try_reserve_resource_match_side(str(match.token), player)
        if not side_reservation:
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        reservation = await try_reserve_resource_clan(clan_obj.tag)
        if not reservation:
            side_reservation.release()
            await ctx.send(
                f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        try:
            if isinstance(ctx, ipy.ComponentContext) and ctx.custom_id.startswith("start_match|"):
                refreshed_match = await Match.find_one(match.base_store_filter(player))
                if not refreshed_match:
                    failure = f"{ERROR_EMOJI} The stored-base reservation changed; the match was not started."
                    await base_store_publication.mark_store_unavailable(ctx.message)
                    await ctx.send(failure, ephemeral=True)
                    return
                match = refreshed_match
            if not await is_clan_available(clan_obj.tag):
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
                return
            if not automatic and stored_start is not None:
                slot_instruction = ""
            elif not automatic:
                available_slots, slot_owners = await get_start_base_store_slot_availability(
                    clan_obj.tag, match.thread, bot=self.bot,
                )
                if not available_slots:
                    await ctx.send(
                        construct_occupied_slots_error(clan_obj.tag, clan_obj.name, slot_owners),
                        ephemeral=True,
                    )
                    return
                slot_instruction = construct_start_slot_instruction(available_slots, clan_obj.name)

            if automatic:
                await helper_host.host_war_automatically(
                    self,
                    ctx,
                    helper_host.AutomaticWarHost(
                        match=match,
                        player=player,
                        clan=clan_obj,
                        host=host,
                        scout=scout,
                        clan_reservation=reservation,
                        side_reservation=side_reservation,
                        stored_start=stored_start,
                    ),
                )
                return

            match ctx:
                case ipy.SlashContext() if stored_start is not None:
                    assert slot is not None
                    reply = war_start.StoredWarStartReply(slot=slot, slot_instruction=slot_instruction)
                case ipy.SlashContext():
                    reply = war_start.SlashWarStartReply(slot_instruction=slot_instruction)
                case ipy.ComponentContext():
                    assert slot
                    reply = war_start.StoredWarStartReply(slot=slot, slot_instruction=slot_instruction)
                case unreachable:
                    assert_never(unreachable)

            started_message = await war_start.start_war(
                self.bot,
                ctx,
                war_start.WarStart(
                    match=match,
                    player=player,
                    host=host,
                    clan=clan_obj,
                    coc_client=self.coc_client,
                    reply=reply,
                    scout=scout,
                    stored_start=stored_start,
                ),
            )
            if not started_message:
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
        finally:
            reservation.release()
            side_reservation.release()

    @war_group.subcommand(sub_cmd_name="start", sub_cmd_description="Start a war-style match instantly")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_war_start(self, ctx: ipy.SlashContext | ipy.ComponentContext, player: int,
                              clan: str, scout: bool = False, slot: str = None):
        await self._start_war(ctx, player, clan, scout=scout, slot=slot)

    @war_group.subcommand(sub_cmd_name="autostart", sub_cmd_description="Use Helper to host a war-style match")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    @ipy.slash_option(
        name="district",
        description="Rerun one district after this side starts; leave blank to host all",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=False,
    )
    async def match_war_autostart(self, ctx: ipy.SlashContext, player: int,
                                  clan: str, scout: bool = False,
                                  district: str | None = None):
        await self._start_war(
            ctx,
            player,
            clan,
            scout=scout,
            automatic=True,
            district=district,
        )

    @ipy.component_callback(re.compile(r"^host_clan_select\|\w+\|\d+$"))
    @coordinated_match_message
    async def host_clan_select(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        match = await Match.find_one(Match.token == token)

        if match is None:
            await ctx.message.edit(
                components=utils.misc_utils.disable_components(*ctx.message.components)
            )
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.",
                ephemeral=True,
            )
            return

        if not await check_match_control(ctx, match, index):
            return
        expected = MatchMessageState.capture(match, index)

        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot set a clan for a match that has ended.",
                ephemeral=True,
            )
            return

        # authorization: unchanged logic, read from the document
        if match.hosts[index] and str(ctx.author.id) != match.hosts[index]:
            await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
            return

        # fetch clan and update only the changed field
        clan = await self.coc_client.get_clan(ctx.values[0])
        reservation = await try_reserve_resource_clan(clan.tag)
        if not reservation or not await claim_resource_clan(
                reservation,
                match,
                index,
                allow_current_owner=True,
                expected_control=expected,
        ):
            await ctx.send(
                f"{ERROR_EMOJI} **{clan.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        # update the message embed as before
        ctx.message.embeds[0].fields[2].value = f"[{clan.name} ({clan.tag})]({clan.share_link})"
        embed = copy.deepcopy(ctx.message.embeds[0])
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, index), embed=embed, existing_message=ctx.message,
        )

        await ctx.send(
            f"{SUCCESS_EMOJI} You have set [{clan.name} ({clan.tag})]({clan.share_link}) as the host clan!",
            ephemeral=True,
        )

    @war_group.subcommand(sub_cmd_name="schedule", sub_cmd_description="Schedule a match for a specific player")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="timestamp",
        description="A Discord timestamp or a Unix timestamp",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @coordinated_match_message
    async def match_war_schedule(self, ctx: ipy.SlashContext, player: int, timestamp: str):
        await ctx.defer(ephemeral=True)

        ts = extract_integer(timestamp)
        if not ts:
            await ctx.send(f"{ERROR_EMOJI} The timestamp you provided is not valid.", ephemeral=True)
            return

        # find the match by the current thread/channel id
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match, player=player):
            player_id = match.participants[player].user_id
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        # prevent scheduling if a live (non-war) match already initiated (mirrors old logic)
        if (not match.is_war) and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A live match is already initiated.", ephemeral=True)
            return

        if ts > 32536799999:
            await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
            return

        # determine embed title (Scheduled/Rescheduled)
        prev_ts = match.times[player]
        title = "**Match Scheduled**"
        if prev_ts and prev_ts != ts:
            title = "**Match Rescheduled**"

        # pick the player user for mention (from participants; fallback to command invoker)
        player_obj = await self.bot.fetch_user(match.participants[player].user_id)
        clan_tag = match.host_clans[player]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        embed = ipy.Embed(
            title=title,
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**", value="Not assigned"),
                ipy.EmbedField(name="**Host Clan**",
                               value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned")
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        host_clan_select = ipy.StringSelectMenu(
            *HOST_CLAN_OPTIONS,
            placeholder="🛡️ Set the host clan",
            custom_id=f"host_clan_select|{match.token}|{player}"
        )

        host_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Host",
            custom_id=f"match_host|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="🙋")
        )

        store_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Store Result",
            custom_id=f"match_result_store|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="💾")
        )

        abort_button = ipy.Button(
            style=ipy.ButtonStyle.RED,
            label="Abort",
            custom_id=f"match_abort|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="🛑")
        )

        if is_final_match(match):
            try:
                match = await schedule_final_match(match, int(ts), player=player)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
        else:
            await match.set({
                "is_war": True,
                f"hosts.{player}": None,
                f"times.{player}": int(ts),
            })
        match.is_war = True
        match.hosts[player] = None
        match.times[player] = int(ts)
        await append_helper_task(match.thread, match.participants[player].user_id, ts)
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, player), content=player_obj.mention, embed=embed,
            components=[ipy.ActionRow(host_clan_select), ipy.ActionRow(host_button, store_button, abort_button)],
        )

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully scheduled a match for {player_obj.mention}!",
                       ephemeral=True)

        # if both players have scheduled times, flip the channel name style
        if all(match.times):
            schedule_channel_name_edit(ctx.channel, "🔵")

    @ipy.component_callback(re.compile(r"^match_abort\|\w+\|\w+$"))
    async def abort_match_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        # load match by token from Mongo
        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(f"{ERROR_EMOJI} You cannot abort a match that has ended.")
            return

        # authorization check against a host list
        if match.hosts and match.hosts[index] and str(ctx.author.id) != match.hosts[index]:
            await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
            return

        abort = war_start.WarAbort(match=match, player=index, message=ctx.message)
        aborted = await war_start.abort_war_start(ctx, abort)
        if not aborted:
            await ctx.send(
                f"{ERROR_EMOJI} The match changed before it could be aborted safely. Please contact an operator.",
                ephemeral=True,
            )
            return

        await ctx.send(f"{SUCCESS_EMOJI} The match has been aborted.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^match_host\|\w+\|\w+$"))
    @coordinated_match_message
    async def host_match_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        # fetch the match by token
        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if not await check_match_control(ctx, match, index):
            return

        # role/permission check (unchanged)
        user_roles = {int(role.id) for role in ctx.author.roles}
        if not user_roles.intersection({STAFF_ROLE, SEN_ROLE, MODERATOR_ROLE, ADMIN_ROLE}):
            await ctx.send(f"{ERROR_EMOJI} You do not have permission to host this match.", ephemeral=True)
            return

        # disallow hosting own match
        player_id = match.participants[index].user_id
        if player_id and str(player_id) == str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot host your own matches.", ephemeral=True)
            return

        # prevent hosting ended match (keep old UX)
        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(f"{ERROR_EMOJI} You cannot host a match that has ended.", ephemeral=True)
            return

        if not await update_match_control(
                match, MatchMessageState.capture(match, index), {f"hosts.{index}": str(ctx.author.id)},
        ):
            await reject_outdated_match_control(ctx)
            return
        match.hosts[index] = str(ctx.author.id)

        # update the embed field “Host”
        ctx.message.embeds[0].fields[1].value = mention_user(ctx.author, mention_type="Helper")
        embed = copy.deepcopy(ctx.message.embeds[0])
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, index), embed=embed, existing_message=ctx.message,
        )

        await ctx.send(f"{SUCCESS_EMOJI} You have set yourself as the host of this match!", ephemeral=True)

        # if both hosts chosen, flip the channel to green
        if all(match.hosts):
            schedule_channel_name_edit(ctx.channel, "🟢")

    @base.subcommand(sub_cmd_name="abort", sub_cmd_description="Abort a live match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    async def match_abort(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # a live match must have a scheduled time
        if not match.times[0]:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # only the assigned host(s) can abort (mirrors "you are not the host")
        if match.hosts and any(match.hosts):
            if str(ctx.author.id) not in {h for h in match.hosts if h}:
                await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
                return

        # disallow abort if any result already stored for either player
        p0 = match.participants[0].result
        p1 = match.participants[1].result
        has_results = any(p0) or any(p1)
        if has_results:
            await ctx.send(
                f"{ERROR_EMOJI} You cannot abort a match that already has a stored result.",
                ephemeral=True
            )
            return

        old_msg = (
            await ctx.channel.fetch_message(match.old_msgs[0])
            if match.old_msgs and match.old_msgs[0]
            else None
        )
        aborted = await live_start.abort_live_start(
            ctx,
            live_start.LiveAbort(match=match, message=old_msg),
        )
        if not aborted:
            await ctx.send(
                f"{ERROR_EMOJI} The match changed before it could be aborted safely. Please contact an operator.",
                ephemeral=True,
            )
            return
        await ctx.send(f"{SUCCESS_EMOJI} The match has been aborted.", ephemeral=True)

    @base.subcommand(sub_cmd_name="start", sub_cmd_description="Start a live match instantly")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_start(self, ctx: ipy.SlashContext, clan: str, scout: bool = False):
        await self._start_live(
            ctx,
            LivePreflight(clan=clan, scout=scout, automatic=False),
        )

    @base.subcommand(sub_cmd_name="autostart", sub_cmd_description="Use Helper to host a live match")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True,
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN,
    )
    @ipy.slash_option(
        name="district",
        description="Rerun one district after the match starts; leave blank to host all",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=False,
    )
    async def match_autostart(
            self,
            ctx: ipy.SlashContext,
            clan: str,
            scout: bool = False,
            district: str | None = None,
    ) -> None:
        await self._start_live(
            ctx,
            LivePreflight(
                clan=clan,
                scout=scout,
                automatic=True,
                requested_by=str(ctx.author.id),
                district=district,
            ),
        )

    async def _host_live_district(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            request: LivePreflight,
            match: Match,
    ) -> None:
        district = request.district
        assert district is not None
        assert request.requested_by is not None
        if not request.automatic or match.is_war:
            await ctx.send(f"{ERROR_EMOJI} This is not a live match.", ephemeral=True)
            return
        host_clan_tag = started_host_clan_tag(match, 0)
        if host_clan_tag is None:
            await ctx.send(
                f"{ERROR_EMOJI} You can only select a district after the live match has started.",
                ephemeral=True,
            )
            return
        if match_district_index(match, district) is None:
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not one of this match's districts.",
                ephemeral=True,
            )
            return
        if request.clan != host_clan_tag:
            await ctx.send(
                f"{ERROR_EMOJI} This match is hosted in **{host_clan_tag}**. "
                "Select that clan for a district rerun.",
                ephemeral=True,
            )
            return

        player1_id = match.participants[0].user_id
        player2_id = match.participants[1].user_id
        player1 = await Player.find_one(Player.user_id == str(player1_id))
        player2 = await Player.find_one(Player.user_id == str(player2_id))
        if player1 is None or player2 is None:
            await ctx.send(f"{ERROR_EMOJI} Both players must be registered for Helper hosting.", ephemeral=True)
            return

        side0 = await try_reserve_resource_match_side(str(match.token), 0)
        if side0 is None:
            await ctx.send(
                f"{ERROR_EMOJI} A Helper job is already running for this match.",
                ephemeral=True,
            )
            return
        side1 = await try_reserve_resource_match_side(str(match.token), 1)
        if side1 is None:
            side0.release()
            await ctx.send(
                f"{ERROR_EMOJI} A Helper job is already running for this match.",
                ephemeral=True,
            )
            return
        try:
            clan = await self.coc_client.get_clan(host_clan_tag)
            await helper_live.host_live_district_automatically(
                self,
                ctx,
                helper_live.AutomaticLiveDistrictHost(
                    match=match,
                    clan=clan,
                    player1_tag=player1.tag,
                    player2_tag=player2.tag,
                    district=district,
                    requested_by=request.requested_by,
                    public_progress=request.public_progress,
                    allow_opponent_cancel=request.allow_opponent_cancel,
                ),
            )
        finally:
            side1.release()
            side0.release()

    async def _start_live(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            request: LivePreflight,
    ) -> None:
        if request.defer_response:
            await ctx.defer(ephemeral=True)

        host = request.host
        scout = request.scout
        if not host:
            host = ctx.author

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        if request.district is not None:
            await self._host_live_district(ctx, request, match)
            return

        # disallow if a war-style match already initiated for one side
        if match.is_war and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A war-style match is already initiated for one of the players.",
                           ephemeral=True)
            return

        if hosting_started(match):
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        if request.automatic:
            reservation_id = ready_live_reservation_id(match, request.stored_reservation_id)
            if request.stored_reservation_id and reservation_id is None:
                await ctx.send(f"{ERROR_EMOJI} The stored live bases are no longer ready.", ephemeral=True)
                return
            if reservation_id:
                stored_clan = live_stores(match, reservation_id)[0][1].clan_tag
                if request.clan != stored_clan:
                    await ctx.send(
                        f"{ERROR_EMOJI} Both players' bases are stored in **{stored_clan}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                request = replace(request, stored_reservation_id=reservation_id)

        # host cannot be one of the two players
        player1_id = match.participants[0].user_id
        player2_id = match.participants[1].user_id
        if str(host.id) in {str(player1_id), str(player2_id)} and str(host.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} The players themselves cannot be the host.", ephemeral=True)
            return

        # player account checks
        p1_doc = await Player.find_one(Player.user_id == str(player1_id))
        p2_doc = await Player.find_one(Player.user_id == str(player2_id))

        player1_acc = await self.coc_client.get_player(p1_doc.tag)
        if player1_acc.town_hall >= 6:
            await ctx.send(f"{ERROR_EMOJI} <@{player1_id}>'s account is above TH6, which enables scouting traps "
                           f"from the FC preview. Please ask the player to change his account to a TH3!",
                           ephemeral=True)
            return

        stored_start = None
        if is_raid_weekend() and int(self.bot.user.id) == BOT_ID:
            if request.automatic and request.stored_reservation_id is None:
                await ctx.send(f"{ERROR_EMOJI} You cannot start a live match during the raid weekend.", ephemeral=True)
                return
            if not request.automatic:
                stored_clan = shared_store_clan(match)
                if stored_clan is None:
                    await ctx.send(
                        f"{ERROR_EMOJI} During raid weekend, `/match start` requires both players' bases "
                        "to be ready in separate slots in the same clan.", ephemeral=True,
                    )
                    return
                if request.clan != stored_clan:
                    await ctx.send(
                        f"{ERROR_EMOJI} Both players' bases are stored in **{stored_clan}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                stored_start = StoredStartState.capture_stores(match, (0, 1))

        player2_acc = await self.coc_client.get_player(p2_doc.tag)
        if player2_acc.town_hall >= 6:
            await ctx.send(f"{ERROR_EMOJI} <@{player2_id}>'s account is above TH6, which enables scouting traps "
                           f"from the FC preview. Please ask the player to change his account to a TH3!",
                           ephemeral=True)
            return

        if scout:
            player_index = 1
            for pid in (player1_id, player2_id):
                district, link = is_missing_scout_view(match, str(pid))
                if district:
                    await ctx.send(f"{ERROR_EMOJI} Player {player_index}'s submitted [base]({link}) for "
                                   f"**{district}** is missing a scout view. Please manually set the scout view "
                                   f"by running {self.bot.mention_command('match base set-image')}!",
                                   ephemeral=True)
                    return
                player_index += 1

        clan_obj = await self.coc_client.get_clan(request.clan)
        if is_resource_clan_locked(clan_obj.tag) or not await is_clan_available(clan_obj.tag):
            await ctx.send(
                f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        slot_instruction = ""
        if request.automatic:
            assert request.requested_by is not None
            await helper_live.host_live_automatically(
                self,
                ctx,
                helper_live.AutomaticLiveHost(
                    match=match,
                    clan=clan_obj,
                    host=host,
                    player1=player1_acc,
                    player2=player2_acc,
                    player1_elo=p1_doc.elo,
                    player2_elo=p2_doc.elo,
                    scout=scout,
                    requested_by=request.requested_by,
                    public_progress=request.public_progress,
                    allow_opponent_cancel=request.allow_opponent_cancel,
                    stored_reservation_id=request.stored_reservation_id,
                ),
            )
            return
        if stored_start is None:
            available_slots, slot_owners = await get_start_base_store_slot_availability(
                clan_obj.tag, match.thread, bot=self.bot,
            )
        else:
            available_slots, slot_owners = [store.slot for _, store in stored_start.stores], {}
        if not available_slots:
            await ctx.send(
                construct_occupied_slots_error(clan_obj.tag, clan_obj.name, slot_owners),
                ephemeral=True,
            )
            return
        slot_instruction = construct_start_slot_instruction(available_slots, clan_obj.name)
        if stored_start is not None:
            slot_instruction = "\n" + "\n".join(
                f"Player {player + 1} attacks the bases in **{store.slot}** in **{clan_obj.name}**."
                for player, store in stored_start.stores
            )
        reply = live_start.ManualLiveStartReply(slot_instruction=slot_instruction, stored=stored_start is not None)
        request = live_start.LiveStart(
            match=match,
            host=host,
            clan=clan_obj,
            player1=player1_acc,
            player2=player2_acc,
            player1_elo=p1_doc.elo,
            player2_elo=p2_doc.elo,
            scout=scout,
            reply=reply,
            stored_start=stored_start,
        )
        outcome = await live_start.start_live_match(self.bot, ctx, request)
        match outcome:
            case live_start.LiveStartSucceeded():
                return
            case live_start.LiveStartBusy():
                await ctx.send(
                    f"{ERROR_EMOJI} This match is already being started.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
            case live_start.LiveStartClanUnavailable():
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
            case unreachable:
                assert_never(unreachable)

    @base.subcommand(sub_cmd_name="schedule", sub_cmd_description="Schedule a live match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="timestamp",
        description="A Discord timestamp or a Unix timestamp",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @coordinated_match_message
    async def match_schedule(self, ctx: ipy.SlashContext, timestamp: str):
        await ctx.defer(ephemeral=True)

        ts = extract_integer(timestamp)
        if not ts:
            await ctx.send(f"{ERROR_EMOJI} The timestamp you provided is not valid.", ephemeral=True)
            return

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # disallow if a war-style match already initiated for one side
        if match.is_war and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A war-style match is already initiated for one of the players.",
                           ephemeral=True)
            return

        if ts > 32536799999:
            await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
            return

        prev_time = next((t for t in (match.times or []) if t), None)
        title = "**Match Scheduled**" if (prev_time is None or prev_time == ts) else "**Match Rescheduled**"

        if is_final_match(match):
            try:
                match = await schedule_final_match(match, int(ts))
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
        else:
            await match.set({
                "is_war": False,
                "times.0": int(ts),
                "times.1": int(ts),
            })
        match.is_war = False
        match.times = [int(ts), int(ts)]

        # resolve host (if already chosen) and host clan (if set)
        host_id = next((h for h in match.hosts if h), None)
        host_user = await self.bot.fetch_user(int(host_id)) if host_id else None

        clan_tag = match.host_clans[0] or match.host_clans[1]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        p1_id = int(match.participants[0].user_id)
        p2_id = int(match.participants[1].user_id)

        embed = ipy.Embed(
            title=title,
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**",
                               value=mention_user(host_user, mention_type="Helper") if host_user else "Not assigned"),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await append_helper_task(match.thread, match.participants[0].user_id, ts)
        await append_helper_task(match.thread, match.participants[1].user_id, ts)
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, 0), content=f"<@{p1_id}>\n<@{p2_id}>", embed=embed,
        )

        # public message in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        guild = await member_guild(ctx)
        members = await asyncio.gather(guild.fetch_member(p1_id), guild.fetch_member(p2_id))
        public_embed = ipy.Embed(
            title=title,
            description=("Spectate in the host clan with a TH5 or lower account. "
                         + (f"Host: {mention_user(host_user, mention_type='Helper')}."
                            if host_user else "Currently the host is not determined.")),
            fields=[
                ipy.EmbedField(name="**Players**",
                               value=f"{mention_user(members[0], p1_id)} vs. {mention_user(members[1], p2_id)}"),
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host Clan**",
                               value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=GREEN_COLOR,
        )
        if match.msg:
            pub = await channel.fetch_message(int(match.msg))
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                existing_message=pub, public_channel=channel,
            )
        else:
            tip = await choose_tip()
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                content=tip, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully scheduled the match!", ephemeral=True)

        # channel name color based on whether a host is set
        if host_user is None:
            schedule_channel_name_edit(ctx.channel, "🔵")
        else:
            schedule_channel_name_edit(ctx.channel, "🟢")

    @ipy.Task.create(ipy.CronTrigger(cron="0 7 * * 4"))  # triggers 7:00 AM every Thursday UTC
    async def raid_weekend_start_notify(self):
        matches = await Match.find().to_list()
        for match in matches:
            if not match.thread:
                continue

            # skip if match has ended
            if is_match_finished(match):
                continue

            thread = await self.bot.fetch_channel(match.thread)
            if not thread or "🔴" in getattr(thread, "name", ""):
                continue

            for participant in match.participants:
                # skip if opponent bases are already stored
                if participant.opponent_bases_stored:
                    continue

                # skip if all results already recorded
                if all(participant.result):
                    continue

                await thread.send(
                    f"<@{participant.user_id}>, raid weekend is starting soon! "
                    f"To play during the raid weekend, please inform the helpers to store bases for you."
                )

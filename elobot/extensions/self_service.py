import calendar
import re
from datetime import UTC, datetime

import interactions as ipy

from elobot.features.hosting.live.request import (
    LIVE_REQUEST_ACCEPT_PATTERN,
    LIVE_REQUEST_REJECT_PATTERN,
    LiveRequestCoordinator,
    LiveRequestIdentity,
)
import elobot.features.base_store.live_controls as live_store_controls
from elobot.features.base_store import controls as base_store_controls
from elobot.features.base_store.models import StoredBaseActionRequest
import elobot.features.hosting.war.helper_spectate as helper_spectate
import elobot.features.hosting.self_hosted.hosting as self_hosting
import elobot.features.hosting.war.helper_reinvite as helper_reinvite
from elobot.features.matches.resources import (
    format_available_resource_clans, try_reserve_resource_clan, try_reserve_resource_match_side,
)
from elobot.features.results.validation import is_match_finished
from elobot.features.hosting.war.confirmation import PendingBaseConfirmation, confirm_pending_opponent_bases
from elobot.features.hosting.war.helper_host import (
    AutomaticWarHost,
    host_war_automatically,
)
from elobot.config.ids import AUTOMATIC_MATCH_THREAD, BOT_ID, LVL2_ROLES
from elobot.config.emojis import ERROR_EMOJI
from elobot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elobot.features.base_store.start import held_store
from elobot.discord.checks import has_any
from elobot.features.matches.timing import hosting_started
from elobot.core.raid_weekend import is_raid_weekend
from elobot.db import Clan, Match, Player


async def is_automatic_match_thread_member(ctx: ipy.BaseContext) -> bool:
    thread = await ctx.bot.fetch_channel(AUTOMATIC_MATCH_THREAD)
    return any(int(member._user_id) == int(ctx.author.id) for member in await thread.fetch_members())


def is_self_store_available(now: datetime) -> bool:
    now_utc = now.astimezone(UTC)
    return (
        now_utc.weekday() == calendar.THURSDAY and now_utc.hour >= 7
        or now_utc.weekday() == calendar.FRIDAY and now_utc.hour < 7
    )


class SelfCommands(ipy.Extension):
    def __init__(self, bot: ipy.Client):
        self.bot: ipy.Client = bot
        self.helper_api_base_url = HELPER_API_BASE_URL
        self.helper_api_key = HELPER_API_KEY
        self.live_requests = LiveRequestCoordinator(self, self._accept_live_request)

    base = ipy.SlashCommand(name="self", description="")

    host_group = base.group(name="host", description="")

    @host_group.subcommand(
        sub_cmd_name="live",
        sub_cmd_description="Start a Helper-hosted live match",
    )
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_host_live(self, ctx: ipy.SlashContext) -> None:
        await self_hosting.request_live_match(self, ctx)

    @ipy.component_callback(LIVE_REQUEST_ACCEPT_PATTERN)
    async def self_host_live_accept(self, ctx: ipy.ComponentContext) -> None:
        await self.live_requests.resolve(ctx)

    @ipy.component_callback(LIVE_REQUEST_REJECT_PATTERN)
    async def self_host_live_reject(self, ctx: ipy.ComponentContext) -> None:
        await self.live_requests.resolve(ctx)

    @ipy.component_callback(re.compile(r"^live_spectate\|\w+\|[0-9]+$"))
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    async def live_match_spectate(self, ctx: ipy.ComponentContext) -> None:
        await helper_spectate.spectate_match(self, ctx)

    @ipy.component_callback(re.compile(r"^war_spectate\|\w+\|[01]\|[0-9]+$"))
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    async def war_match_spectate(self, ctx: ipy.ComponentContext) -> None:
        await helper_spectate.spectate_match(self, ctx, war_side=int(ctx.custom_id.split("|")[2]))

    @ipy.component_callback(re.compile(r"^helper_stored_live_start\|\w+\|[0-9a-f]{16}$"))
    async def self_stored_live_start(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        await live_store_controls.request_stored_live_match(self.live_requests, ctx)

    @ipy.component_callback(re.compile(r"^helper_stored_live_undo\|\w+\|[0-9a-f]{16}$"))
    async def self_stored_live_undo(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        match_commands = self.bot.get_ext("MatchCommands")
        if match_commands is None:
            await ctx.send(f"{ERROR_EMOJI} Base storage is currently unavailable. Please contact a helper.", ephemeral=True)
            return
        await live_store_controls.undo_live_storage(match_commands, ctx)

    async def _accept_live_request(
            self,
            ctx: ipy.ComponentContext,
            identity: LiveRequestIdentity,
            match: Match,
            requester_index: int,
    ) -> None:
        await self_hosting.start_accepted_live_match(self, ctx, identity, match, requester_index)

    @host_group.subcommand(sub_cmd_name="war", sub_cmd_description="Start a match with a bot hosting you")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_host_war(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        participants = match.participants[:2] if match else []
        caller_id = str(ctx.author.id)
        if caller_id not in [participant.user_id for participant in participants]:
            await ctx.send(f"{ERROR_EMOJI} Only a match participant can host this war match.", ephemeral=True)
            return

        index = next(index for index, participant in enumerate(participants) if participant.user_id == caller_id)
        if is_match_finished(match, player=index):
            await ctx.send(f"{ERROR_EMOJI} This match is not available for self hosting.", ephemeral=True)
            return

        if hosting_started(match, index):
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return

        participant = match.participants[index]
        store = held_store(participant)
        has_store = participant.base_store is not None or participant.opponent_bases_stored
        if not has_store:
            if not await confirm_pending_opponent_bases(
                    self.bot,
                    ctx,
                    PendingBaseConfirmation(match=match, player=index),
            ):
                return
        if store is None and is_raid_weekend() and int(self.bot.user.id) == BOT_ID:
            await ctx.send(
                f"{ERROR_EMOJI} You can only start a match with ready stored bases during the raid weekend.",
                ephemeral=True,
            )
            return
        if has_store:
            if store is None or store.ready is False or store.message_id is None:
                await ctx.send(
                    f"{ERROR_EMOJI} Base storage is incomplete. Wait for **Bases Stored**, "
                    "or use its Undo control before storing again.",
                    ephemeral=True,
                )
                return
            match_commands = self.bot.get_ext("MatchCommands")
            if match_commands is None:
                await ctx.send(f"{ERROR_EMOJI} Stored-base hosting is currently unavailable.", ephemeral=True)
                return
            await base_store_controls.start_stored_bases(
                match_commands,
                ctx,
                StoredBaseActionRequest(str(match.token), index, store.reservation_id, store.message_id),
            )
            return
        clans = await Clan.find_all().to_list()
        available_clans = [clan for clan in clans if clan.availability_str == "Available"]
        if not available_clans:
            await ctx.send(
                f"{ERROR_EMOJI} There are no available helper clans.\n\n{format_available_resource_clans(clans)}",
                ephemeral=True,
            )
            return
        available_clans = await self_hosting.order_self_host_clans(self.bot, match, index, available_clans)
        reservation = None
        helper_clan = None
        for candidate in available_clans:
            reservation = await try_reserve_resource_clan(candidate.tag)
            if reservation:
                helper_clan = candidate
                break
        if not helper_clan:
            await ctx.send(
                f"{ERROR_EMOJI} There are no available helper clans.\n\n{format_available_resource_clans(clans)}",
                ephemeral=True,
            )
            return

        side_reservation = await try_reserve_resource_match_side(str(match.token), index)
        if not side_reservation:
            reservation.release()
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{format_available_resource_clans(await Clan.find_all().to_list())}",
                ephemeral=True,
            )
            return

        try:
            fresh_clan = await Clan.find_one(Clan.tag == helper_clan.tag)
            if not fresh_clan or fresh_clan.availability_str != "Available":
                await ctx.send(
                    f"{ERROR_EMOJI} The selected helper clan is no longer available.\n\n"
                    f"{format_available_resource_clans(await Clan.find_all().to_list())}",
                    ephemeral=True,
                )
                return
            clan_obj = await self.bot.coc_client.get_clan(helper_clan.tag)
            await host_war_automatically(
                self,
                ctx,
                AutomaticWarHost(
                    match=match,
                    player=index,
                    clan=clan_obj,
                    host=self.bot.user,
                    submitter_id=str(self.bot.user.id),
                    clan_reservation=reservation,
                    side_reservation=side_reservation,
                ),
            )
        finally:
            reservation.release()
            side_reservation.release()

    @base.subcommand(sub_cmd_name="re-invite", sub_cmd_description="Kick and re-invite yourself to your match clan")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    async def self_reinvite(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)

        caller_id = str(ctx.author.id)
        matches = await Match.find({"participants.user_id": caller_id}).to_list()
        match_clan_tag = None
        for match in matches:
            player = next(
                (index for index, participant in enumerate(match.participants[:2]) if participant.user_id == caller_id),
                None,
            )
            if player is None:
                continue
            match_player = player if match.is_war else None
            shared_index = player if match.is_war else 0
            clan_tag = match.host_clans[shared_index]
            if match.times[shared_index] and clan_tag and not is_match_finished(match, player=match_player):
                match_clan_tag = clan_tag
                break

        if match_clan_tag is None:
            await ctx.send(
                f"{ERROR_EMOJI} You do not have a started war or live match that is still active.",
                ephemeral=True,
            )
            return

        player_doc = await Player.find_one(Player.user_id == caller_id)
        if player_doc is None:
            await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
            return

        await helper_reinvite.kick_and_reinvite(
            self,
            ctx,
            helper_reinvite.HelperReinviteRequest(
                requested_by=caller_id,
                host_clan_tag=match_clan_tag,
                player_tag=player_doc.tag,
            ),
        )

    store_group = base.group(name="store", description="")

    @store_group.subcommand(sub_cmd_name="base", sub_cmd_description="Store your bases for the current match")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_store_base(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)

        if not is_self_store_available(datetime.now(UTC)) and int(self.bot.user.id) == BOT_ID:
            await ctx.send(
                f"{ERROR_EMOJI} Self-service base storage is available from Thursday <t:1767250800:t> "
                "until Friday <t:1767337200:t>.",
                ephemeral=True,
            )
            return

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        participants = match.participants[:2] if match else []
        caller_id = str(ctx.author.id)
        if caller_id not in [participant.user_id for participant in participants]:
            await ctx.send(f"{ERROR_EMOJI} Only a match participant can store their own bases.", ephemeral=True)
            return

        player = next(index for index, participant in enumerate(participants) if participant.user_id == caller_id)
        match_commands = self.bot.get_ext("MatchCommands")
        await match_commands.store_self_bases(ctx, match, player)

    @store_group.subcommand(sub_cmd_name="base-live", sub_cmd_description="Store both players' bases in one clan")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_store_base_live(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)
        if not is_self_store_available(datetime.now(UTC)) and int(self.bot.user.id) == BOT_ID:
            await ctx.send(
                f"{ERROR_EMOJI} Self-service base storage is available from Thursday <t:1767250800:t> "
                "until Friday <t:1767337200:t>.", ephemeral=True,
            )
            return
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        participants = match.participants[:2] if match else []
        if len(participants) != 2 or str(ctx.author.id) not in [player.user_id for player in participants]:
            await ctx.send(f"{ERROR_EMOJI} Only a match participant can store bases for a live match.", ephemeral=True)
            return
        match_commands = self.bot.get_ext("MatchCommands")
        if match_commands is None:
            await ctx.send(f"{ERROR_EMOJI} Base storage is currently unavailable. Please contact a helper.", ephemeral=True)
            return
        await match_commands.store_self_live_bases(ctx, match)

    @store_group.subcommand(sub_cmd_name="result", sub_cmd_description="Store your result with Helper")
    @ipy.max_concurrency(ipy.Buckets.USER, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_store_result(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        participants = match.participants[:2] if match else []
        caller_id = str(ctx.author.id)
        if caller_id not in [participant.user_id for participant in participants]:
            await ctx.send(f"{ERROR_EMOJI} Only a match participant can store their own result.", ephemeral=True)
            return

        player = next(index for index, participant in enumerate(participants) if participant.user_id == caller_id)
        if not match.is_war:
            await ctx.send(f"{ERROR_EMOJI} This match is not available for automatic result storage.", ephemeral=True)
            return
        if not match.times[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match has not started yet.", ephemeral=True)
            return
        if not match.host_clans[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match does not have a helper clan yet.", ephemeral=True)
            return
        if len(match.final_districts) != 3 or not all(match.final_districts):
            await ctx.send(f"{ERROR_EMOJI} This match does not have three final districts yet.", ephemeral=True)
            return

        match_commands = self.bot.get_ext("MatchCommands")
        if not match_commands:
            await ctx.send(f"{ERROR_EMOJI} Automatic result storage is currently unavailable. Please contact a helper.",
                           ephemeral=True)
            return
        await match_commands.store_self_result(ctx, match, player)

    @store_group.subcommand(sub_cmd_name="result-live", sub_cmd_description="Store both live match results with Helper")
    @ipy.max_concurrency(ipy.Buckets.CHANNEL, 1)
    @ipy.check(has_any(ipy.has_any_role(*LVL2_ROLES), is_automatic_match_thread_member))
    async def self_store_result_live(self, ctx: ipy.SlashContext) -> None:
        await ctx.defer(ephemeral=False)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        participants = match.participants[:2] if match else []
        if str(ctx.author.id) not in [participant.user_id for participant in participants]:
            await ctx.send(f"{ERROR_EMOJI} Only a match participant can store live match results.", ephemeral=False)
            return
        if match.is_war or len(participants) != 2:
            await ctx.send(f"{ERROR_EMOJI} This match is not available for live result storage.", ephemeral=False)
            return
        if not match.times[0]:
            await ctx.send(f"{ERROR_EMOJI} This live match has not started yet.", ephemeral=False)
            return
        if not match.host_clans[0]:
            await ctx.send(f"{ERROR_EMOJI} This live match does not have a helper clan yet.", ephemeral=False)
            return
        if len(match.final_districts) != 3 or not all(match.final_districts):
            await ctx.send(f"{ERROR_EMOJI} This match does not have three final districts yet.", ephemeral=False)
            return

        match_commands = self.bot.get_ext("MatchCommands")
        if not match_commands:
            await ctx.send(f"{ERROR_EMOJI} Automatic result storage is currently unavailable. Please contact a helper.",
                           ephemeral=False)
            return
        await match_commands.store_self_live_result(ctx, match)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self) -> None:
        print("➤ Self commands loaded")

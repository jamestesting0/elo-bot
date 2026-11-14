from elobot.features.finals.types import is_final_match
from copy import deepcopy
import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
import random
from typing import assert_never

from beanie import PydanticObjectId
from pydantic import JsonValue

import coc
import interactions as ipy
from elobot.discord.mentions import mention_user
from elobot.discord.members import member_guild

import elobot.extensions.match as match_commands
import elobot.features.hosting.live.live_reservations as live_reservations
from elobot.features.base_store.publication import BaseStoreMessageConflict, mark_store_reserved
from elobot.features.base_store.live import live_pair_ready, live_stores
from elobot.features.base_store.start import StoredStartState
from elobot.discord.channel_edits import schedule_channel_name_edit
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, GREEN_COLOR, RED_COLOR
from elobot.config.ids import MATCH_CHANNEL
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.matches.messages import MatchMessageState, match_message_embeds, publish_match_message
from elobot.features.matches.message_lock import coordinated_match_message, match_message_lock
from elobot.db import Match
from elobot.features.finals.hosting import accept_final_start
from elobot.features.finals.types import FinalError
from elobot.config.emojis import ERROR_EMOJI
from elobot.db.match_models import ExpectedLiveStartState
from elobot.features.matches.timing import hosting_started


@dataclass(frozen=True, slots=True)
class ManualLiveStartReply:
    slot_instruction: str
    stored: bool = False


@dataclass(frozen=True, slots=True)
class AutomaticLiveStartReply:
    pass


type LiveStartReply = ManualLiveStartReply | AutomaticLiveStartReply


@dataclass(frozen=True, slots=True)
class LiveStart:
    match: Match
    host: ipy.BaseUser
    clan: coc.Clan
    player1: coc.Player
    player2: coc.Player
    player1_elo: int
    player2_elo: int
    scout: bool
    reply: LiveStartReply
    reservations: live_reservations.LiveStartReservations | None = None
    stored_reservation_id: str | None = None
    stored_start: StoredStartState | None = None


@dataclass(frozen=True, slots=True)
class StoredLiveStartState(MatchMessageState):
    reservation_id: str = ""
    clan_tag: str = ""

    def binding_filter(self) -> dict[str, JsonValue | PydanticObjectId]:
        query = dict(MatchMessageState.binding_filter(self))
        query.update({"times": list(self.times), "hosts": list(self.hosts)})
        for index in (0, 1):
            query.update({
                f"participants.{index}.user_id": self.participants[index],
                f"participants.{index}.base_store.reservation_id": self.reservation_id,
                f"participants.{index}.base_store.clan_tag": self.clan_tag,
                f"participants.{index}.base_store.slot": f"Slot {index + 1}",
                f"participants.{index}.base_store.is_live": True,
                f"participants.{index}.base_store.ready": True,
            })
        return query


@dataclass(frozen=True, slots=True)
class ClaimedLiveStart:
    start: LiveStart
    timestamp: int


@dataclass(frozen=True, slots=True)
class LiveStartSucceeded:
    message: ipy.Message


@dataclass(frozen=True, slots=True)
class LiveStartBusy:
    pass


@dataclass(frozen=True, slots=True)
class LiveStartClanUnavailable:
    pass


type LiveStartOutcome = LiveStartSucceeded | LiveStartBusy | LiveStartClanUnavailable


@dataclass(frozen=True, slots=True)
class LiveAbort:
    match: Match
    message: ipy.Message | None


async def reserve_live_start(match_token: str, clan_tag: str) -> live_reservations.LiveReservationOutcome:
    async def find_match(token: str) -> Match | None:
        return await match_commands.Match.find_one({"token": token})

    return await live_reservations.reserve_live_start(
        live_reservations.LiveReservationOperations(
            reserve_match_side=match_commands.try_reserve_resource_match_side,
            reserve_clan=match_commands.try_reserve_resource_clan,
            find_match=find_match,
            is_match_finished=match_commands.is_match_finished,
        ),
        match_token,
        clan_tag,
    )


@coordinated_match_message
async def start_live_match(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        start: LiveStart,
) -> LiveStartOutcome:
    token = str(start.match.token)
    owns_reservations = start.reservations is None
    reservation_outcome = start.reservations or await reserve_live_start(token, start.clan.tag)
    match reservation_outcome:
        case live_reservations.LiveStartReservations() as reservations:
            pass
        case live_reservations.LiveReservationsBusy():
            return LiveStartBusy()
        case live_reservations.LiveReservationsClanUnavailable():
            return LiveStartClanUnavailable()
        case unreachable:
            assert_never(unreachable)
    try:
        fresh_match = await match_commands.Match.find_one({"token": token})
        if (
                fresh_match is None
                or match_commands.is_match_finished(fresh_match)
                or hosting_started(fresh_match)
        ):
            return LiveStartBusy()
        expected_control = start.stored_start
        if start.stored_reservation_id:
            if (not live_pair_ready(fresh_match, start.stored_reservation_id)
                    or live_stores(fresh_match, start.stored_reservation_id)[0][1].clan_tag != start.clan.tag):
                return LiveStartBusy()
            expected_control = replace(
                StoredLiveStartState.capture(fresh_match, 0),
                reservation_id=start.stored_reservation_id, clan_tag=start.clan.tag,
            )
        start.match.is_war = fresh_match.is_war
        start.match.times = list(fresh_match.times)
        start.match.hosts = list(fresh_match.hosts)
        start.match.host_clans = list(fresh_match.host_clans)
        start.match.old_msgs = list(fresh_match.old_msgs)
        if expected_control is None and is_final_match(fresh_match):
            expected_control = MatchMessageState.capture(fresh_match, 0)
        timestamp = int(datetime.now().timestamp())
        try:
            await accept_final_start(fresh_match, str(ctx.author.id), clan_tag=start.clan.tag, player=None,
                expected_source=dict(expected_control.binding_filter() if expected_control is not None
                                     else MatchMessageState.capture(fresh_match, 0).binding_filter()) if is_final_match(fresh_match) else None)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return LiveStartBusy()
        claimed = await match_commands.claim_resource_clan(
            reservations.clan,
            start.match,
            0,
            player2_index=1,
            side_reservation=reservations.side0,
            **({"expected_control": expected_control} if expected_control is not None else {}),
            updates={
                "is_war": False,
                "times.0": timestamp,
                "hosts.0": str(start.host.id),
            },
        )
        if not claimed:
            return LiveStartClanUnavailable()
        message = await complete_live_start(bot, ctx, ClaimedLiveStart(start=start, timestamp=timestamp))
        return LiveStartSucceeded(message=message)
    finally:
        if owns_reservations:
            reservations.release()


@coordinated_match_message
async def complete_live_start(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        claimed: ClaimedLiveStart,
) -> ipy.Message:
    start = claimed.start
    timestamp = claimed.timestamp
    match_doc = start.match
    expected_message_state = MatchMessageState.capture(match_doc, 0)
    player1_id = match_doc.participants[0].user_id
    player2_id = match_doc.participants[1].user_id
    guild = await member_guild(ctx)
    members = await asyncio.gather(
        guild.fetch_member(player1_id), guild.fetch_member(player2_id),
    )
    player1_name = mention_user(members[0], player1_id)
    player2_name = mention_user(members[1], player2_id)
    if start.player1_elo == start.player2_elo:
        chosen_player = random.choice([str(player1_id), str(player2_id)])
    elif start.player1_elo > start.player2_elo:
        chosen_player = str(player1_id)
    else:
        chosen_player = str(player2_id)
    mentions = (
        f"<@{player1_id}> (Attacking First)\n<@{player2_id}>"
        if chosen_player == str(player1_id)
        else f"<@{player1_id}>\n<@{player2_id}> (Attacking First)"
    )
    long_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.LongDateTime)
    relative_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.RelativeTime)
    title = "**Match Started**"
    embed = ipy.Embed(
        title=title,
        fields=[
            ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
            ipy.EmbedField(name="**Host**", value=mention_user(start.host)),
            ipy.EmbedField(
                name="**Host Clan**",
                value=f"[{start.clan.name} ({start.clan.tag})]({start.clan.share_link})",
            ),
        ],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    spectate_controls = [ipy.ActionRow(ipy.Button(
        style=ipy.ButtonStyle.SECONDARY, label="Spectate", emoji="👀",
        custom_id=f"live_spectate|{match_doc.token}|{timestamp}",
    ))]
    message = await publish_match_message(
        ctx, match_doc, expected_message_state, content=mentions, embed=embed,
    )

    channel = await bot.fetch_channel(match_doc.routing_channel_id if is_final_match(match_doc)
                                     else MATCH_CHANNEL)
    public_embed = ipy.Embed(
        title=title,
        description=(
            "Spectate in the host clan with a TH5 or lower account. "
            f"Host: {mention_user(start.host)}."
        ),
        fields=[
            ipy.EmbedField(name="**Players**", value=f"{player1_name} vs. {player2_name}"),
            ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
            ipy.EmbedField(
                name="**Host Clan**",
                value=f"[{start.clan.name} ({start.clan.tag})]({start.clan.share_link})",
            ),
        ],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=GREEN_COLOR,
    )
    if match_doc.msg:
        public = await channel.fetch_message(int(match_doc.msg))
        await publish_match_message(
            ctx, match_doc, MatchMessageState.capture(match_doc, 0), embed=public_embed,
            components=spectate_controls, existing_message=public, public_channel=channel,
        )
    else:
        await publish_match_message(
            ctx, match_doc, MatchMessageState.capture(match_doc, 0), embed=public_embed,
            content=await match_commands.choose_tip(), components=spectate_controls, public_channel=channel,
        )

    await match_commands.send_bases(bot, match_doc.token, 1, 0, scout=start.scout)
    await match_commands.send_bases(bot, match_doc.token, 0, 1, scout=start.scout)
    for player in range(len(match_doc.participants)):
        if not await mark_store_reserved(bot, match_doc, player):
            raise BaseStoreMessageConflict("The match started, but its held base-slot message could not be updated.")

    match_doc = await match_commands.Match.find_one(match_commands.Match.token == match_doc.token)
    assert match_doc is not None
    match start.reply:
        case AutomaticLiveStartReply():
            pass
        case ManualLiveStartReply(slot_instruction=slot_instruction, stored=True):
            await ctx.send(
                f"{SUCCESS_EMOJI} Successfully started the match! Use the stored bases."
                f"{slot_instruction}", ephemeral=True,
            )
        case ManualLiveStartReply(slot_instruction=slot_instruction):
            player1_bases = []
            player2_bases = []
            for index, district in enumerate(match_doc.final_districts):
                if match_doc.participants[0].bases[index]:
                    player1_bases.append(f"[{district}]({match_doc.participants[0].bases[index].link})")
                if match_doc.participants[1].bases[index]:
                    player2_bases.append(f"[{district}]({match_doc.participants[1].bases[index].link})")
            await ctx.send(
                f"{SUCCESS_EMOJI} Successfully started the match! Please set the players' bases "
                f"in the correct clan and slot.\n\n"
                f"**<@{player1_id}>'s Bases ([{start.player1.tag}]({start.player1.share_link}))**\n"
                f"{'\n'.join(player1_bases)}\n\n"
                f"**<@{player2_id}>'s Bases ([{start.player2.tag}]({start.player2.share_link}))**\n"
                f"{'\n'.join(player2_bases)}{slot_instruction}",
                ephemeral=True,
                suppress_embeds=True,
            )
        case unreachable:
            assert_never(unreachable)
    await match_commands.remove_helper_task(match_doc.thread, match_doc.participants[0].user_id)
    await match_commands.remove_helper_task(match_doc.thread, match_doc.participants[1].user_id)
    schedule_channel_name_edit(ctx.channel, "🟢")
    return message


async def abort_live_start(
        ctx: ipy.ComponentContext | ipy.SlashContext,
        abort: LiveAbort,
) -> bool:
    async with match_message_lock(abort.match.thread):
        return await _abort_live_start_locked(ctx, abort)


async def _abort_live_start_locked(
        ctx: ipy.ComponentContext | ipy.SlashContext,
        abort: LiveAbort,
) -> bool:
    match_doc = abort.match
    old_message = match_doc.old_msgs[0] if match_doc.old_msgs else None
    host_clans = {clan_tag for clan_tag in match_doc.host_clans if clan_tag}
    if abort.message is not None:
        if old_message is None or int(abort.message.id) != old_message:
            return False
    expected = ExpectedLiveStartState(
        times=tuple(match_doc.times),
        hosts=tuple(match_doc.hosts),
        host_clans=tuple(match_doc.host_clans),
        old_messages=tuple(match_doc.old_msgs),
    )
    if not await match_doc.abort_started_live_match(expected):
        return False
    publication_error: ipy.errors.HTTPException | None = None
    for channel_id, message_id, supplied, is_public in (
            (int(match_doc.thread), old_message, abort.message, False),
            (match_doc.routing_channel_id if is_final_match(match_doc) else MATCH_CHANNEL,
             int(match_doc.msg) if match_doc.msg else None, None, True),
    ):
        if message_id is None:
            continue
        try:
            message = supplied
            if message is None:
                channel = await ctx.bot.fetch_channel(channel_id)
                message = await channel.fetch_message(message_id)
            embeds = deepcopy(message.embeds) or [ipy.Embed()]
            embeds[0].title = "**Match Aborted**"
            embeds[0].description = None
            if is_public:
                embeds[0].color = RED_COLOR
            await message.edit(embeds=match_message_embeds(embeds), components=[])
        except ipy.errors.NotFound:
            continue
        except ipy.errors.HTTPException as error:
            publication_error = error
    for host_clan in host_clans:
        await match_commands.free_clan(host_clan, expected_thread=str(match_doc.thread))
    for player in range(len(match_doc.participants)):
        if not await mark_store_reserved(ctx.bot, match_doc, player, ready=True):
            raise BaseStoreMessageConflict("The match was aborted, but its held base-slot message could not be updated.")
    schedule_channel_name_edit(ctx.channel, "🟡")
    if publication_error is not None:
        raise publication_error
    return True

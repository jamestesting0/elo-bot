from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

import coc
import interactions as ipy
from elobot.discord.mentions import mention_user

from elobot.features.base_store.publication import BaseStoreMessageConflict, mark_store_reserved
import cogs.match_commands as match_commands
from elobot.discord.channel_edits import schedule_channel_name_edit
from elobot.features.matches.messages import MatchMessageState, match_message_embeds, publish_match_message
from elobot.features.matches.message_lock import coordinated_match_message, match_message_lock
from elobot.features.base_store.start import FreshWarStartState, StoredStartState, held_store
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elobot.config.game import HOST_CLAN_OPTIONS
from elobot.db import Match
from elobot.features.finals.hosting import accept_final_start
from elobot.features.finals.types import FinalError, is_final_match


@dataclass(frozen=True, slots=True)
class SlashWarStartReply:
    slot_instruction: str


@dataclass(frozen=True, slots=True)
class StoredWarStartReply:
    slot: str
    slot_instruction: str
    helper_hosted: bool = False


@dataclass(frozen=True, slots=True)
class SelfHostedWarStartReply:
    pass


type WarStartReply = SlashWarStartReply | StoredWarStartReply | SelfHostedWarStartReply


@dataclass(frozen=True, slots=True)
class WarStart:
    match: Match
    player: int
    host: ipy.BaseUser
    clan: coc.Clan
    coc_client: coc.Client
    reply: WarStartReply
    scout: bool = False
    clan_reservation: "match_commands.ResourceClanReservation | None" = None
    side_reservation: "match_commands.ResourceMatchSideReservation | None" = None
    stored_start: StoredStartState | None = None


@dataclass(frozen=True, slots=True)
class WarAbort:
    match: Match
    player: int
    message: ipy.Message


@coordinated_match_message
async def start_war(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        start: WarStart,
) -> ipy.Message | None:
    match_doc = start.match
    player = start.player
    participant = match_doc.participants[player]
    store = held_store(participant)
    if (participant.base_store is not None or participant.opponent_bases_stored) and (
            store is None or (store.reservation_id is not None and store.message_id is None)
    ):
        await ctx.send(
            f"{ERROR_EMOJI} Base storage is incomplete. Wait for **Bases Stored**, "
            "or use its Undo control before storing again.",
            ephemeral=True,
        )
        return None
    expected_control = start.stored_start or (
        StoredStartState.capture_stores(match_doc, (player,)) if store is not None
        else FreshWarStartState.capture(match_doc, player)
    )
    timestamp = int(datetime.now().timestamp())
    host_id = str(start.host.id)
    clan_tag = start.clan.tag
    reservation = start.clan_reservation or match_commands.current_resource_clan_reservation(clan_tag)
    if not reservation:
        reservation = await match_commands.try_reserve_resource_clan(clan_tag)
    if not reservation:
        return None
    try:
        await accept_final_start(match_doc, str(ctx.author.id), clan_tag=clan_tag, player=player,
            expected_source=dict(expected_control.binding_filter()) if is_final_match(match_doc) else None)
    except FinalError as error:
        reservation.release()
        await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
        return None
    except BaseException:
        reservation.release()
        raise
    if not await match_commands.claim_resource_clan(
            reservation,
            match_doc,
            player,
            updates={
                "is_war": True,
                f"times.{player}": timestamp,
                f"hosts.{player}": host_id,
            },
            side_reservation=start.side_reservation,
            expected_control=expected_control,
    ):
        return None
    return await complete_war_start(bot, ctx, start)


@coordinated_match_message
async def complete_war_start(
        bot: ipy.Client,
        ctx: ipy.SlashContext | ipy.ComponentContext,
        start: WarStart,
) -> ipy.Message:
    match_doc = start.match
    player = start.player
    expected_message_state = MatchMessageState.capture(match_doc, player)
    opponent = 1 - player
    player_id = match_doc.participants[player].user_id
    start_timestamp = match_doc.times[player]
    assert start_timestamp is not None

    long_ts = ipy.Timestamp.fromtimestamp(start_timestamp).format(ipy.TimestampStyles.LongDateTime)
    relative_ts = ipy.Timestamp.fromtimestamp(start_timestamp).format(ipy.TimestampStyles.RelativeTime)
    player_obj = await bot.fetch_user(int(player_id)) if player_id.isdigit() else await bot.fetch_user(player_id)
    embed = ipy.Embed(
        title="**Match Started**",
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

    token = match_doc.token
    host_clan_select = ipy.StringSelectMenu(
        *HOST_CLAN_OPTIONS,
        placeholder="🛡️ Set the host clan",
        custom_id=f"host_clan_select|{token}|{player}",
    )
    host_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Host",
        custom_id=f"match_host|{token}|{player}",
        emoji=ipy.PartialEmoji(name="🙋"),
    )
    store_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Store Result",
        custom_id=f"match_result_store|{token}|{player}",
        emoji=ipy.PartialEmoji(name="💾"),
    )
    spectate_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY, label="Spectate", emoji=ipy.PartialEmoji(name="👀"),
        custom_id=f"war_spectate|{token}|{player}|{start_timestamp}",
    )
    abort_button = ipy.Button(
        style=ipy.ButtonStyle.RED,
        label="Abort",
        custom_id=f"match_abort|{token}|{player}",
        emoji=ipy.PartialEmoji(name="🛑"),
    )

    match ctx:
        case ipy.SlashContext():
            components = [ipy.ActionRow(host_clan_select)]
        case ipy.ComponentContext():
            components = []
        case unreachable:
            assert_never(unreachable)
    components.append(ipy.ActionRow(host_button, store_button, spectate_button, abort_button))

    message = await publish_match_message(
        ctx, match_doc, expected_message_state, content=player_obj.mention, embed=embed, components=components,
    )

    await match_commands.send_bases(bot, token, player, opponent, scout=start.scout)
    if not await mark_store_reserved(bot, match_doc, player):
        raise BaseStoreMessageConflict("The match started, but its held base-slot message could not be updated.")
    channel_status = "🟢" if all(match_doc.hosts) else "🔵"

    match start.reply:
        case SelfHostedWarStartReply():
            pass
        case StoredWarStartReply(helper_hosted=True) if str(ctx.author.id) == player_id:
            await ctx.send(
                f"{SUCCESS_EMOJI} Successfully started the match.",
                suppress_embeds=True,
                ephemeral=True,
            )
        case SlashWarStartReply() | StoredWarStartReply():
            refreshed_match = await match_commands.Match.find_one(match_commands.Match.token == token)
            assert refreshed_match is not None
            opponent_bases = []
            for index in range(len(refreshed_match.final_districts)):
                opponent_base = (
                    refreshed_match.participants[opponent].bases[index]
                    if index < len(refreshed_match.participants[opponent].bases)
                    else None
                )
                if not opponent_base:
                    continue
                district = (
                    refreshed_match.final_districts[index]
                    if index < len(refreshed_match.final_districts)
                    else None
                )
                if not district:
                    continue
                opponent_bases.append(f"[{district}]({opponent_base.link})")
            opponent_bases_text = "\n".join(opponent_bases)

            player_doc = await match_commands.Player.find_one(match_commands.Player.user_id == player_id)
            player_tag = player_doc.tag if player_doc else None
            if not player_tag:
                await ctx.send(f"{ERROR_EMOJI} Player not found in database.", ephemeral=True)
                if all(match_doc.times):
                    schedule_channel_name_edit(ctx.channel, channel_status)
                return message
            player_account: coc.Player = await start.coc_client.get_player(player_tag)

            match start.reply:
                case SlashWarStartReply(slot_instruction=slot_instruction):
                    response = (
                        f"{SUCCESS_EMOJI} Successfully started the match for {player_obj.mention} "
                        f"([{player_account.tag}]({player_account.share_link}))! "
                        f"Please set the bases in an appropriate clan and invite the player.{slot_instruction}\n\n"
                        f"**<@{match_doc.participants[opponent].user_id}>'s Bases**\n{opponent_bases_text}\n\n"
                        f"{IMPORTANT_EMOJI} *Make sure to clear the bases right after sending the FC!*"
                    )
                case StoredWarStartReply(
                        slot=slot,
                        helper_hosted=helper_hosted,
                ):
                    player_account_text = (
                        player_account.tag
                        if helper_hosted
                        else f"[{player_account.tag}]({player_account.share_link})"
                    )
                    slot_status = (
                        f"**{slot.lower()}** remains reserved for this match."
                        if helper_hosted
                        else (
                            f"<@{match_doc.participants[opponent].user_id}>'s bases are stored in "
                            f"**{slot.lower()}** in **{start.clan.name}**."
                        )
                    )
                    response = (
                        f"{SUCCESS_EMOJI} Successfully started the match for {player_obj.mention} "
                        f"({player_account_text})! "
                        f"{slot_status}"
                    )
                case unreachable:
                    assert_never(unreachable)
            await ctx.send(response, suppress_embeds=True, ephemeral=True)
        case unreachable:
            assert_never(unreachable)

    await match_commands.remove_helper_task(match_doc.thread, match_doc.participants[player].user_id)
    if all(match_doc.times):
        schedule_channel_name_edit(ctx.channel, channel_status)
    return message


async def abort_war_start(ctx: ipy.ComponentContext | ipy.SlashContext, abort: WarAbort) -> bool:
    async with match_message_lock(abort.match.thread):
        return await _abort_war_start_locked(ctx, abort)


async def _abort_war_start_locked(ctx: ipy.ComponentContext | ipy.SlashContext, abort: WarAbort) -> bool:
    match_doc = abort.match
    player = abort.player
    expected_old_message = match_doc.old_msgs[player]
    if expected_old_message is None:
        return False
    if int(abort.message.id) != expected_old_message:
        return False
    old_clan = match_doc.host_clans[player]
    if not await match_doc.abort_started_war_side(
            player,
            match_doc.times[player],
            match_doc.hosts[player],
            old_clan,
            expected_old_message,
    ):
        return False
    publication_error: ipy.errors.HTTPException | None = None
    try:
        embeds = deepcopy(abort.message.embeds) or [ipy.Embed()]
        embeds[0].title = "**Match Aborted**"
        embeds[0].description = None
        await abort.message.edit(embeds=match_message_embeds(embeds), components=[])
    except ipy.errors.NotFound:
        publication_error = None
    except ipy.errors.HTTPException as error:
        publication_error = error
    await match_commands.free_clan(old_clan, expected_thread=str(match_doc.thread))
    if not await mark_store_reserved(ctx.bot, match_doc, player, ready=True):
        raise BaseStoreMessageConflict("The match was aborted, but its held base-slot message could not be updated.")
    schedule_channel_name_edit(ctx.channel, "🟡")
    if publication_error is not None:
        raise publication_error
    return True

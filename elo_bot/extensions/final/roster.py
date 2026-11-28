import logging
import re
import time
from copy import copy

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, LVL1_ROLES
from elo_bot.core.finals import FINAL_SEATS, FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_invitation_models import FinalInvitation
from elo_bot.db.match_models import Match
from elo_bot.discord.channel_edits import schedule_channel_name_edit
from elo_bot.discord.decorators import ignore_repeated_clicks
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import fetch_members, member_names
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.discord.modals import ModalSelectLabel, modal_user_id
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals.invitations import (
    accept_invitation, add_players, decline_invitation, invitation_embed, invite_players, notify, open_seats,
    require_registration, retire_outdated_invitations, withdraw_invitation,
)
from elo_bot.features.finals.labels import final_player_link, thread_label
from elo_bot.features.finals.head_to_head import FINAL_MATCHUP_FILENAME
from elo_bot.features.tokens.round_betting import publish_betting_round
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.opponent_picks import available_opponents, pick_prompt, pick_receipt, pick_turn, save_pick
from elo_bot.features.finals.posts import staff_notice
from elo_bot.features.finals.registration_posts import sync_registration_posts
from elo_bot.features.finals.replacement import replace_entrant, replaceable_entrants

ROSTER_TITLES = {"add": "Add Players", "invite": "Invite Players", "replace": "Replace Player"}


def replace_modal(final: EloFinal, owner: str) -> ipy.Modal:
    options = [
        ipy.StringSelectOption(label=final.player_nicknames.get(user, user)[:100], value=user)
        for user in replaceable_entrants(final)
    ]
    return ipy.Modal(
        ModalSelectLabel(
            label="Leaving player",
            component=ipy.StringSelectMenu(*options, custom_id="leaving"),
        ),
        ModalSelectLabel(
            label="Replacement",
            component=ipy.UserSelectMenu(custom_id="replacement", placeholder="Select a player"),
        ),
        title=ROSTER_TITLES["replace"],
        custom_id=f"finalrostermodal|replace|{final.id}|{owner}",
    )


def roster_modal(action: str, final: EloFinal, owner: str) -> ipy.Modal:
    seats = open_seats(final)
    return ipy.Modal(
        ModalSelectLabel(
            label="Players",
            description=f"Up to {seats} more player{'s' if seats > 1 else ''} can join.",
            component=ipy.UserSelectMenu(custom_id="players", placeholder="Select players", max_values=min(seats, 25)),
        ),
        title=ROSTER_TITLES[action],
        custom_id=f"finalrostermodal|{action}|{final.id}|{owner}",
    )


def require_owner(ctx: ipy.InteractionContext, owner: str) -> None:
    if owner != str(ctx.author.id):
        raise FinalError("You cannot interact with another user's components.")


class Roster:
    @ipy.component_callback(re.compile(r"^finalroster\|(add|invite|replace)\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_roster_open(self, ctx: ipy.ComponentContext):
        _, action, final_id, owner = ctx.custom_id.split("|")
        require_owner(ctx, owner)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        if action == "replace":
            if not replaceable_entrants(edition):
                raise FinalError("Every quarterfinal has started. Use `/final match forfeit`.")
            await ctx.send_modal(replace_modal(edition, owner))
            return
        require_registration(edition)
        if open_seats(edition) <= 0:
            raise FinalError(f"All {FINAL_SEATS} seats are filled.")
        await ctx.send_modal(roster_modal(action, edition, owner))

    @ipy.modal_callback(re.compile(r"^finalrostermodal\|(add|invite|replace)\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_roster_submit(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(edit_origin=True)
        _, action, final_id, owner = ctx.custom_id.split("|")
        require_owner(ctx, owner)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        if action == "replace":
            joining = modal_user_id(response["replacement"][0])
            outcome = await self._replace(edition, await member_guild(ctx), response["leaving"][0], joining)
            await self._show_dashboard(ctx, await reload_final(edition), outcome=outcome, edit=True)
            return
        require_registration(edition)
        player_ids = [modal_user_id(player) for player in response["players"]]
        members = await fetch_members(await member_guild(ctx), player_ids)
        if action == "add":
            outcome = (await add_players(edition, members)).line("Added")
            await retire_outdated_invitations(self.bot)
            edition = await reload_final(edition)
            await sync_registration_posts(self.bot, edition)
        else:
            outcome = (await invite_players(edition, members, str(ctx.author.id))).line("Invited")
        await self._show_dashboard(ctx, await reload_final(edition), outcome=outcome, edit=True)

    @ipy.component_callback(re.compile(r"^finalinvwithdraw\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_invitation_withdraw(self, ctx: ipy.ComponentContext):
        _, final_id, owner = ctx.custom_id.split("|")
        require_owner(ctx, owner)
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        invitation = await withdraw_invitation(edition, ctx.values[0])
        await retire_outdated_invitations(self.bot)
        names = await member_names(await member_guild(ctx), [invitation.user_id])
        await self._show_dashboard(ctx, edition, edit=True,
                                   outcome=f"{SUCCESS_EMOJI} Withdrew the invitation for {names[invitation.user_id]}.")

    @ipy.component_callback(re.compile(r"^finalinvite\|(accept|decline)\|[a-f0-9]{24}$"))
    @ignore_repeated_clicks
    async def final_invitation_answer(self, ctx: ipy.ComponentContext):
        _, action, invitation_id = ctx.custom_id.split("|")
        invitation_id = PydanticObjectId(invitation_id)
        try:
            invitation = await FinalInvitation.get(invitation_id)
            if invitation is None:
                raise FinalError("This invitation no longer exists.")
            member = await self._invitee(invitation)
            answer = (await accept_invitation(invitation_id, member) if action == "accept"
                      else await decline_invitation(invitation_id))
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        await ctx.edit_origin(embed=invitation_embed(await FinalInvitation.get(invitation_id)), components=[])
        if answer.closed_now:
            await notify(self.bot, answer.state, invitation.user_id, member, answer.entrants)
            if answer.state == "accepted":
                edition = await EloFinal.get(invitation.final_id)
                if edition is not None:
                    await sync_registration_posts(self.bot, edition)

    @ipy.component_callback(re.compile(r"^finalpick\|[a-f0-9]{24}\|\d+\|[0-2]\|(select|none|\d{1,20})$"))
    @ignore_repeated_clicks
    async def final_opponent_pick(self, ctx: ipy.ComponentContext) -> None:
        _, final_id, generation, turn_index, action = ctx.custom_id.split("|")
        index = int(turn_index)
        final = await EloFinal.get(PydanticObjectId(final_id))
        picks = final.opponent_picks if final else None
        if (final is None or picks is None or picks.generation != int(generation) or pick_turn(final) != index
                or (ctx.guild_id and ctx.guild_id != final.guild_id)):
            await ctx.send(f"{ERROR_EMOJI} This pick is closed.", ephemeral=True)
            return
        if picks.seeds[index] != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} This pick belongs to another player.", ephemeral=True)
            return
        now = int(time.time())
        deadline = picks.turns[index].deadline
        if deadline is None or now >= deadline:
            await ctx.send(f"{ERROR_EMOJI} This pick has expired.", ephemeral=True)
            return
        opponent = ctx.values[0] if action == "select" and ctx.values else action
        if opponent not in available_opponents(picks):
            await ctx.send(f"{ERROR_EMOJI} Choose an available opponent.", ephemeral=True)
            return
        if action == "select":
            embed, controls = pick_prompt(final, index, selected=opponent)
            await ctx.edit_origin(embed=embed, components=controls)
            return

        await ctx.defer(edit_origin=True)
        if not await save_pick(final, index, int(time.time()), opponent):
            await ctx.send(f"{ERROR_EMOJI} This pick is closed.", ephemeral=True)
            return
        try:
            await ctx.edit_origin(embed=pick_receipt(final, opponent, timed_out=False), components=[])
        finally:
            await sync_registration_posts(self.bot, final)

    async def _replace(self, edition: EloFinal, guild: ipy.Guild, leaving: str, joining: str) -> str:
        members = await fetch_members(guild, [leaving, joining])
        if members[joining] is None:
            raise FinalError("The replacement is not in the server.")
        if members[joining].has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("The replacement is banned from Elo.")
        names = {user: final_player_link(edition, user, member) for user, member in members.items()}
        replaced = await replace_entrant(edition, leaving, joining, members[joining].display_name)
        summary = f"🔁 {names[joining]} replaced {names[leaving]} in {replaced.node_id}"
        lines = [f"{SUCCESS_EMOJI} {names[joining]} replaced {names[leaving]} in {replaced.node_id}."]
        lines += [f"{IMPORTANT_EMOJI} {item}" for item in await self._recover(replaced.final)]
        if replaced.thread_id:
            try:
                await self._move_thread(replaced, leaving, joining, names)
            except ipy.errors.HTTPException as error:
                lines.append(f"{ERROR_EMOJI} The match thread could not be updated: {error}")
        if replaced.round_started:
            current = await reload_final(edition)
            _, _, errors = await self._provision_node(current, await self._dashboard_state(current), replaced.node_id)
            lines += [f"{ERROR_EMOJI} {error}" for error in errors]
            try:
                await publish_betting_round(self.bot, current, "Quarterfinals")
            except FinalError as error:
                lines.append(f"{IMPORTANT_EMOJI} {error}")
        await staff_notice(self.bot, summary)
        return "\n".join(lines)

    async def _move_thread(self, replaced, leaving: str, joining: str, names: dict[str, str]) -> None:
        thread = await self.bot.fetch_channel(int(replaced.thread_id))
        node = replaced.final.nodes[replaced.node_id]
        first, second = node.contestants
        players = (await self.bot.fetch_user(int(first)), await self.bot.fetch_user(int(second)))
        schedule_channel_name_edit(thread, thread.name.partition(" ")[0], thread_label(replaced.final, node.id, players))
        source = await Match.get(node.match_id)
        if source and source.thread_message_id:
            opening = await thread.fetch_message(source.thread_message_id)
            embed = copy(opening.embeds[0])
            attachments = [item for item in opening.attachments if item.filename != FINAL_MATCHUP_FILENAME]
            if len(attachments) != len(opening.attachments):
                embed.images = []
            await edit_channel_message(opening, content=opening.content.replace(f"<@{leaving}>", f"<@{joining}>"),
                                       embeds=[embed, *opening.embeds[1:]], attachments=attachments)
        await thread.add_member(int(joining))
        await thread.remove_member(int(leaving))
        await thread.send(f"{IMPORTANT_EMOJI} <@{joining}> replaced {names[leaving]}.",
                          allowed_mentions=ipy.AllowedMentions(users=[int(joining)]))

    async def _invitee(self, invitation: FinalInvitation) -> ipy.Member | None:
        final = await EloFinal.get(invitation.final_id)
        guild = await self.bot.fetch_guild(final.guild_id) if final else None
        return await guild.fetch_member(invitation.user_id) if guild else None

    async def _retire_invitations(self) -> None:
        try:
            await retire_outdated_invitations(self.bot)
        except (PyMongoError, ipy.errors.HTTPException):
            logging.getLogger(__name__).exception("Final invitation cleanup failed")

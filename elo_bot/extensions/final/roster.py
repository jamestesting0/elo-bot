import logging
import re

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
from elo_bot.discord.mentions import fetch_members, member_names, mention_user
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals.invitations import (
    accept_invitation, add_players, decline_invitation, invitation_embed, invite_players, notify, open_seats,
    require_registration, retire_outdated_invitations, withdraw_invitation,
)
from elo_bot.features.finals.labels import thread_label
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.posts import staff_notice
from elo_bot.features.finals.replacement import replace_entrant, replaceable_entrants

ROSTER_TITLES = {"add": "Add Players", "invite": "Invite Players", "replace": "Replace Player"}


def replace_modal(final: EloFinal, owner: str) -> dict:
    options = [{"label": final.player_nicknames.get(user, user)[:100], "value": user}
               for user in replaceable_entrants(final)]
    return {
        "type": int(ipy.CallbackType.MODAL),
        "data": {
            "title": ROSTER_TITLES["replace"],
            "custom_id": f"finalrostermodal|replace|{final.id}|{owner}",
            "components": [
                {"type": 18, "label": "Leaving player",
                 "component": {"type": 3, "custom_id": "leaving", "options": options, "required": True}},
                {"type": 18, "label": "Replacement",
                 "component": {"type": int(ipy.ComponentType.USER_SELECT), "custom_id": "replacement",
                               "placeholder": "Select a player", "min_values": 1, "max_values": 1,
                               "required": True}},
            ],
        },
    }


def roster_modal(action: str, final: EloFinal, owner: str) -> dict:
    seats = open_seats(final)
    return {
        "type": int(ipy.CallbackType.MODAL),
        "data": {
            "title": ROSTER_TITLES[action],
            "custom_id": f"finalrostermodal|{action}|{final.id}|{owner}",
            "components": [{
                "type": 18,
                "label": "Players",
                "description": f"Up to {seats} more player{'s' if seats > 1 else ''} can join.",
                "component": {
                    "type": int(ipy.ComponentType.USER_SELECT),
                    "custom_id": "players",
                    "placeholder": "Select players",
                    "min_values": 1,
                    "max_values": min(seats, 25),
                    "required": True,
                },
            }],
        },
    }


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
            outcome = await self._replace(edition, await member_guild(ctx), response["leaving"][0],
                                          response["replacement"][0])
            await self._show_dashboard(ctx, await reload_final(edition), outcome=outcome, edit=True)
            return
        require_registration(edition)
        members = await fetch_members(await member_guild(ctx), response["players"])
        if action == "add":
            outcome = (await add_players(edition, members)).line("Added")
            await retire_outdated_invitations(self.bot)
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

    async def _replace(self, edition: EloFinal, guild: ipy.Guild, leaving: str, joining: str) -> str:
        members = await fetch_members(guild, [leaving, joining])
        if members[joining] is None:
            raise FinalError("The replacement is not in the server.")
        if members[joining].has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("The replacement is banned from Elo.")
        names = {user: mention_user(member, user) for user, member in members.items()}
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
        await staff_notice(self.bot, summary)
        return "\n".join(lines)

    async def _move_thread(self, replaced, leaving: str, joining: str, names: dict[str, str]) -> None:
        thread = await self.bot.fetch_channel(int(replaced.thread_id))
        node = replaced.final.nodes[replaced.node_id]
        players = [await self.bot.fetch_user(int(user)) for user in node.contestants]
        schedule_channel_name_edit(thread, thread.name.partition(" ")[0], thread_label(node.id, players))
        source = await Match.get(node.match_id)
        if source and source.thread_message_id:
            opening = await thread.fetch_message(source.thread_message_id)
            await edit_channel_message(opening, content=opening.content.replace(f"<@{leaving}>", f"<@{joining}>"))
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

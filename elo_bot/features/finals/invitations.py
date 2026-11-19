import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from elo_bot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.core.finals import FINAL_SEATS, FinalError
from elo_bot.db import EloFinal, Player
from elo_bot.db.final_invitation_models import FinalInvitation, InvitationState
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.features.finals.invitation_store import expire_invitations, withdraw_pending
from elo_bot.features.finals.lifecycle import add_entrant
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.posts import staff_notice

INVITATION_LIFETIME = 24 * 60 * 60
CLOSED_TEXT: dict[InvitationState, str] = {
    "accepted": "You're in. The bracket is seeded by Elo once it's set.",
    "declined": "You declined this invitation.",
    "expired": "This invitation expired.",
    "withdrawn": "This invitation was withdrawn.",
    "full": "The Final filled up before you accepted.",
}


@dataclass(slots=True)
class RosterOutcome:
    done: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)

    def line(self, verb: str) -> str:
        lines = [f"{SUCCESS_EMOJI} {verb} {', '.join(self.done)}"] if self.done else []
        lines += [f"{ERROR_EMOJI} {name}: {reason}" for name, reason in self.refused]
        return "\n".join(lines)


def open_seats(final: EloFinal) -> int:
    return FINAL_SEATS - len(final.entrant_ids)


def require_registration(final: EloFinal) -> None:
    if not final.active or final.state != "REGISTRATION":
        raise FinalError("Players can join only until the bracket is created.")


async def pending_invitations(final_id: PydanticObjectId) -> list[FinalInvitation]:
    return await FinalInvitation.find({"final_id": final_id, "state": "pending",
                                       "expires_at": {"$gt": int(time.time())}}).sort("created_at").to_list()


async def _refusal(final: EloFinal, user_id: str, member: ipy.Member | None) -> str | None:
    if member is None:
        return "not in the server"
    if member.has_role(ELO_BLACKLIST_ROLE):
        return "banned from Elo"
    if user_id in final.entrant_ids:
        return "already an entrant"
    if await Player.find_one({"user_id": user_id}) is None:
        return "not registered"
    return None


async def add_players(final: EloFinal, members: Mapping[str, ipy.Member | None]) -> RosterOutcome:
    outcome = RosterOutcome()
    for user_id, member in members.items():
        name = mention_user(member, user_id)
        if reason := await _refusal(final, user_id, member):
            outcome.refused.append((name, reason))
            continue
        try:
            final = await add_entrant(final, user_id, member.display_name)
        except FinalError:
            outcome.refused.append((name, "the Final is full"))
            continue
        outcome.done.append(name)
    return outcome


def invitation_embed(invitation: FinalInvitation) -> ipy.Embed:
    season = invitation.season_number
    description = (f"You're invited to the Season {season} Elo Final.\n"
                   f"Expires <t:{invitation.expires_at}:R>" if invitation.state == "pending"
                   else CLOSED_TEXT[invitation.state])
    embed = ipy.Embed(title=f"**Season {season} Final · Invitation**", description=description, color=COLOR)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


def invitation_controls(invitation: FinalInvitation) -> list[ipy.ActionRow]:
    return [ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Accept", emoji=TICK_EMOJI,
                   custom_id=f"finalinvite|accept|{invitation.id}"),
        ipy.Button(style=ipy.ButtonStyle.DANGER, label="Decline", emoji=CROSS_EMOJI,
                   custom_id=f"finalinvite|decline|{invitation.id}"),
    )]


async def invite_players(final: EloFinal, members: Mapping[str, ipy.Member | None], inviter: str) -> RosterOutcome:
    outcome = RosterOutcome()
    await expire_invitations()
    for user_id, member in members.items():
        name = mention_user(member, user_id)
        if reason := await _refusal(final, user_id, member):
            outcome.refused.append((name, reason))
            continue
        now = int(time.time())
        invitation = FinalInvitation(final_id=final.id, season_number=final.season_number, user_id=user_id,
                                     display_name=member.display_name, invited_by=inviter, created_at=now,
                                     expires_at=now + INVITATION_LIFETIME)
        try:
            await invitation.insert()
        except DuplicateKeyError:
            outcome.refused.append((name, "already invited"))
            continue
        try:
            message = await member.send(embed=invitation_embed(invitation), components=invitation_controls(invitation))
        except ipy.errors.HTTPException:
            await invitation.delete()
            outcome.refused.append((name, "DMs are closed"))
            continue
        await invitation.set({"dm_channel_id": str(message.channel.id), "dm_message_id": str(message.id)})
        outcome.done.append(name)
    return outcome


@dataclass(frozen=True, slots=True)
class InvitationAnswer:
    state: InvitationState
    entrants: int
    closed_now: bool


async def accept_invitation(invitation_id: PydanticObjectId, member: ipy.Member | None) -> InvitationAnswer:
    """Close the invitation, adding the player when a seat is free."""
    async def accept(session) -> InvitationAnswer:
        collection = FinalInvitation.get_pymongo_collection()
        invitation = await FinalInvitation.get(invitation_id, session=session)
        if invitation is None:
            raise FinalError("This invitation no longer exists.")
        final = await EloFinal.get(invitation.final_id, session=session)
        entrants = len(final.entrant_ids) if final else 0
        if invitation.state != "pending":
            return InvitationAnswer(invitation.state, entrants, False)

        async def close(state: InvitationState) -> InvitationAnswer:
            await collection.update_one({"_id": invitation.id, "state": "pending"}, {"$set": {"state": state}},
                                        session=session)
            return InvitationAnswer(state, entrants, True)

        if invitation.expires_at <= time.time():
            return await close("expired")
        if final is None or not final.active or final.state != "REGISTRATION":
            return await close("withdrawn")
        if invitation.user_id in final.entrant_ids:
            return await close("accepted")
        if member is None:
            raise FinalError("Join the server to accept this invitation.")
        if member.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("You are banned from using Elo commands. Please contact the staff team if you have doubts.")
        if await Player.find_one({"user_id": invitation.user_id}, session=session) is None:
            raise FinalError("Register with `/player register` first, then accept again.")
        if entrants >= FINAL_SEATS:
            return await close("full")
        await close("accepted")
        final = await add_entrant(final, invitation.user_id, member.display_name, session=session)
        return InvitationAnswer("accepted", len(final.entrant_ids), True)
    return await transaction(accept)


async def decline_invitation(invitation_id: PydanticObjectId) -> InvitationAnswer:
    invitation = await FinalInvitation.get(invitation_id)
    if invitation is None:
        raise FinalError("This invitation no longer exists.")
    state: InvitationState = "expired" if invitation.expires_at <= time.time() else "declined"
    saved = await FinalInvitation.get_pymongo_collection().update_one(
        {"_id": invitation.id, "state": "pending"}, {"$set": {"state": state}})
    if saved.modified_count:
        return InvitationAnswer(state, 0, True)
    return InvitationAnswer((await FinalInvitation.get(invitation_id)).state, 0, False)


async def withdraw_invitation(final: EloFinal, invitation_id: str) -> FinalInvitation:
    invitation = await FinalInvitation.get(PydanticObjectId(invitation_id))
    if invitation is None or invitation.final_id != final.id or invitation.state != "pending":
        raise FinalError("This invitation is no longer pending. Refresh first.")
    await withdraw_pending(final.id, user_id=invitation.user_id)
    return invitation


async def notify(bot: ipy.Client, state: InvitationState, user_id: str, member: ipy.Member | None,
                 entrants: int) -> None:
    name = mention_user(member, user_id)
    match state:
        case "accepted":
            content = f"{SUCCESS_EMOJI} {name} accepted the invitation · `{entrants}/{FINAL_SEATS}` entrants"
        case "declined":
            content = f"{ERROR_EMOJI} {name} declined the invitation"
        case _:
            return
    await staff_notice(bot, content)


async def retire_outdated_invitations(bot: ipy.Client) -> None:
    await expire_invitations()
    for invitation in await FinalInvitation.find({"dm_outdated": True}).to_list():
        if invitation.dm_channel_id and invitation.dm_message_id:
            try:
                channel = await bot.fetch_channel(int(invitation.dm_channel_id))
                message = await channel.fetch_message(int(invitation.dm_message_id)) if channel else None
                if message is not None:
                    await edit_channel_message(message, embed=invitation_embed(invitation), components=[])
            except ipy.errors.NotFound:
                pass
            except ipy.errors.HTTPException:
                logging.exception("Could not retire the DM of Final invitation %s", invitation.id)
                continue
        await FinalInvitation.get_pymongo_collection().update_one(
            {"_id": invitation.id, "state": invitation.state}, {"$set": {"dm_outdated": False}})

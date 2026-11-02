import logging
from collections.abc import Sequence
from hashlib import sha256
from typing import assert_never

import anyio
import interactions as ipy
from beanie import Link
from interactions.api.http.route import Route
from pymongo.errors import PyMongoError

from common.constants import AUTOMATIC_MATCH_THREAD, LVL2_ROLES
from database_models import Player, RecordedMatch
from database_models.final_models import FinalPlayerProfile


INVITATION_LOCKS: dict[str, anyio.Lock] = {}


async def has_two_completed_matches(player: Player) -> bool:
    """Include ranked records, autowins and official Final results, without counting corrections twice."""
    count = len(player.matches)
    if count >= 2:
        return True
    autowins = set(player.autowin_receipts)
    for record in player.matches:
        match record:
            case RecordedMatch(operation_key=str() as key):
                autowins.discard(key.split(":", 1)[0])
            case RecordedMatch() | Link():
                pass
            case unreachable:
                assert_never(unreachable)
    count += len(autowins)
    if count >= 2:
        return True
    profile = await FinalPlayerProfile.find_one({"user_id": player.user_id})
    if profile is not None:
        count += sum(stats.played + stats.forfeits for stats in profile.editions.values())
    return count >= 2


async def invite_eligible_players(bot: ipy.Client, user_ids: Sequence[str], *, pending_only: bool = False) -> None:
    """Check newly stored completions and recover interrupted invitations without another ping."""
    for user_id in dict.fromkeys(user_ids):
        async with INVITATION_LOCKS.setdefault(user_id, anyio.Lock()):
            try:
                player = await Player.find_one({"user_id": user_id}, fetch_links=True)
                if player is None or player.self_host_invitation_message_id is not None:
                    continue
                if pending_only and not player.self_host_invitation_attempted:
                    continue
                if not await has_two_completed_matches(player):
                    continue
                # An individual lookup also covers members beyond the first page of the thread member list.
                try:
                    await bot.http.request(Route(
                        "GET", "/channels/{thread_id}/thread-members/{user_id}",
                        thread_id=AUTOMATIC_MATCH_THREAD, user_id=user_id,
                    ))
                except ipy.errors.NotFound:
                    pass
                else:
                    continue

                thread = await bot.fetch_channel(AUTOMATIC_MATCH_THREAD)
                member = await bot.fetch_member(user_id, thread.guild.id, force=True)
                if member is None or any(member.has_role(role) for role in LVL2_ROLES):
                    continue
                nonce = sha256(f"self-host-invitation:{player.id}".encode()).hexdigest()[:24]
                message = None
                if player.self_host_invitation_attempted:
                    async for previous in thread.history(limit=0):
                        if str(previous.nonce) == nonce and previous.author.id == bot.user.id:
                            message = previous
                            break
                collection = Player.get_pymongo_collection()
                if message is None:
                    claim = await collection.update_one(
                        {"_id": player.id, "self_host_invitation_message_id": None},
                        {"$set": {"self_host_invitation_attempted": True}},
                    )
                    if claim.matched_count != 1:
                        continue
                    message = await thread.send(
                        f"<@{user_id}> You can now self-host your matches! "
                        "Use `/self host war` or `/self host live` in your match thread.",
                        allowed_mentions=ipy.AllowedMentions(users=[user_id]),
                        nonce=nonce,
                        enforce_nonce=True,
                    )
                await collection.update_one(
                    {"_id": player.id, "self_host_invitation_message_id": None},
                    {"$set": {"self_host_invitation_message_id": int(message.id)}},
                )
            except (ipy.errors.HTTPException, PyMongoError):
                logging.exception("Self-host invitation for player %s remains pending", user_id)

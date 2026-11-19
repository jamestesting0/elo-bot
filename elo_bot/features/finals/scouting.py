import asyncio
import logging
import secrets
import time
from pathlib import Path

import interactions as ipy

from elo_bot.core.finals import FINAL_SCOUT_WINDOW, FinalError
from elo_bot.db import EloFinal, Match
from elo_bot.features.bases.screenshots import capture_base_screenshot
from elo_bot.features.matches.attack_bases import choose_bases

_PREPARING = asyncio.Lock()
FINAL_BASES_LOCKED = "Your bases are locked because scouting has opened for this match."


def scout_opens_at(match: Match, attacker: int) -> int | None:
    """When the attacker may scout the opponent's bases. The opponent's bases lock at the same moment."""
    scheduled = match.scheduled_times[attacker]
    return None if scheduled is None else scheduled - FINAL_SCOUT_WINDOW


async def store_scout_image(bot: ipy.Client, base_link: str, requested_by: str, *,
                            ctx: ipy.InteractionContext | None = None) -> str | None:
    path = Path(f"temp/{secrets.token_hex(8)}.webp")
    if not await capture_base_screenshot(base_link, requested_by, str(path), ctx=ctx, keep_secret=True,
                                         scout_view=True):
        return None
    try:
        message = await bot.owner.send(file=ipy.File(str(path)), silent=True)
    finally:
        path.unlink(missing_ok=True)
    return message.attachments[0].url


async def _prepare_side(bot: ipy.Client, match: Match, attacker: int) -> None:
    defender = 1 - attacker
    if not all(match.participants[defender].bases):
        users = [await bot.fetch_user(int(participant.user_id)) for participant in match.participants]
        match = await choose_bases(match.token, users[attacker], users[defender], return_match_data=True)
    for index, base in enumerate(match.participants[defender].bases):
        if base is None or base.scout_view:
            continue
        image = await store_scout_image(bot, base.link, str(bot.user.id))
        if image is None:
            continue
        field = f"participants.{defender}.bases.{index}"
        await Match.get_pymongo_collection().update_one(
            {"_id": match.id, f"{field}.link": base.link},
            {"$set": {f"{field}.image": image, f"{field}.scout_view": True}})


async def prepare_open_scouting(bot: ipy.Client) -> None:
    """Once a side's scouting opens, fill the opponent's missing bases with Helper bases and capture scout views."""
    if _PREPARING.locked():
        return
    async with _PREPARING:
        finals = [str(final.id) for final in await EloFinal.find({"active": True}).to_list()]
        now = int(time.time())
        for match in await Match.find({"final_id": {"$in": finals}, "final_superseded": False,
                                       "final_result_id": None, "thread": {"$ne": None}}).to_list():
            for attacker in (0, 1):
                opens = scout_opens_at(match, attacker)
                if opens is None or now < opens or match.final_started_times[attacker] is not None:
                    continue
                try:
                    await _prepare_side(bot, match, attacker)
                except (FinalError, ipy.errors.HTTPException):
                    logging.exception("Scouting for Final match %s could not be prepared", match.token)

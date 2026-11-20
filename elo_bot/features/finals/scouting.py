import asyncio
import logging
import time

import interactions as ipy

from elo_bot.core.finals import FINAL_SCOUT_WINDOW, FinalError
from elo_bot.db import EloFinal, Match
from elo_bot.features.finals.scout_images import request_scout_views
from elo_bot.features.matches.attack_bases import choose_bases

_PREPARING = asyncio.Lock()
FINAL_BASES_LOCKED = "Your bases are locked because scouting has opened for this match."


def scheduled_scout_opens_at(match: Match, attacker: int) -> int | None:
    scheduled = match.scheduled_times[attacker]
    return None if scheduled is None else scheduled - FINAL_SCOUT_WINDOW


def scout_opens_at(match: Match, attacker: int) -> int | None:
    if attacker in match.final_scouting_paused_sides:
        return None
    return scheduled_scout_opens_at(match, attacker)


async def _prepare_side(bot: ipy.Client, match: Match, attacker: int) -> None:
    defender = 1 - attacker
    opens = scout_opens_at(match, attacker)
    if (opens is not None and time.time() >= opens and match.final_started_times[attacker] is None
            and not all(match.participants[defender].bases)):
        users = [await bot.fetch_user(int(participant.user_id)) for participant in match.participants]
        match = await choose_bases(match.token, users[attacker], users[defender], return_match_data=True)
    if any(base and not base.scout_view for base in match.participants[defender].bases):
        request_scout_views(bot, match.id, defender)


async def prepare_open_scouting(bot: ipy.Client) -> None:
    """Retry pending captures and fill missing bases once the opponent's scouting opens."""
    if _PREPARING.locked():
        return
    async with _PREPARING:
        finals = [str(final.id) for final in await EloFinal.find({"active": True}).to_list()]
        for match in await Match.find({"final_id": {"$in": finals}, "final_superseded": False,
                                       "final_result_id": None, "final_result_operation": None,
                                       "thread": {"$ne": None}}).to_list():
            for attacker in (0, 1):
                try:
                    await _prepare_side(bot, match, attacker)
                except (FinalError, ipy.errors.HTTPException):
                    logging.exception("Scouting for Final match %s could not be prepared", match.token)

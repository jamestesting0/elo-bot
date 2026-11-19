from datetime import UTC, datetime

from elo_bot.core.season_calendar import SeasonCalendar, season_calendar
from elo_bot.db import EloFinal, Season


async def latest_archive() -> Season | None:
    return await Season.find_all().sort("-season_number").first_or_none()


async def ongoing_season_number() -> int:
    archive = await latest_archive()
    return archive.season_number + 1 if archive else 1


async def season_final(season_number: int) -> EloFinal | None:
    return await EloFinal.find_one({"season_number": season_number, "state": {"$ne": "CANCELLED"}})


async def ongoing_season_calendar(now: datetime) -> SeasonCalendar:
    archive = await latest_archive()
    final = await season_final(archive.season_number + 1 if archive else 1)
    return season_calendar(now, datetime.fromtimestamp(archive.end, UTC) if archive else None,
                           final.id.generation_time if final else None)

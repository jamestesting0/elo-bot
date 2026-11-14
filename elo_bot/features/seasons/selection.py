from datetime import datetime

from elo_bot.db import Season


class InvalidSeasonSelectionError(ValueError):
    pass


def get_season_by_number(seasons: list[Season], season_number: int) -> Season | None:
    return next((season for season in seasons if season.season_number == season_number), None)


def interpret_season_string(seasons: list[Season], season: str) -> tuple[str, int, int]:
    if season == "All Seasons":
        scope = season
        season_start = -1
        season_end = datetime.now().timestamp()
    elif season == "Current Season":
        scope = season
        season_start = seasons[-1].end if seasons else -1
        season_end = datetime.now().timestamp()
    else:
        if not season.isdigit():
            raise InvalidSeasonSelectionError(f"\"{season}\" is not a valid season string.")

        season_doc = get_season_by_number(seasons, int(season))
        if season_doc is None:
            raise InvalidSeasonSelectionError(f"Season {season} does not exist.")

        scope = f"Season {season_doc.season_number}"
        season_start = int(season_doc.start)
        season_end = int(season_doc.end)

    return scope, season_start, season_end

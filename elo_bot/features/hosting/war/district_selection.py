from elo_bot.db import Match


def match_district_index(match: Match, district: str) -> int | None:
    return next(
        (index for index, match_district in enumerate(match.final_districts) if match_district == district),
        None,
    )


def started_host_clan_tag(match: Match, player: int) -> str | None:
    if not match.times[player] or not match.hosts[player]:
        return None
    return match.host_clans[player]

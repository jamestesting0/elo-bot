from elo_bot.db import RecordedMatch


def get_district_matches(season_matches: list[RecordedMatch], district: str, player_id: str) -> list[RecordedMatch]:
    if not season_matches:
        return []

    return list(
        filter(lambda m: district in m.players[player_id].districts, season_matches)
    )


def get_season_matches(player_matches: list[RecordedMatch], season_start: int | float,
                       season_end: int | float) -> list[RecordedMatch]:
    if not player_matches:
        return []

    return list(
        filter(lambda m: season_start < m.time < season_end, player_matches)
    )


def get_wins(season_matches: list[RecordedMatch], player_id: str) -> int:
    return sum(1 for m in season_matches if m.players[player_id].won)


def get_winrate(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    wins = get_wins(season_matches, player_id)
    return float(wins / len(season_matches)) * 100.0


def get_avg_stars(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    attacks = len(season_matches) * 3
    return float(sum(m.players[player_id].stars for m in season_matches) / attacks)


def get_avg_destruction(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    attacks = len(season_matches) * 3
    return float(sum(m.players[player_id].destruction for m in season_matches) / attacks)


def get_opponent_avg_stars(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    attacks = len(season_matches) * 3
    return float(sum(m.players[player_id].opponent_stars for m in season_matches) / attacks)


def get_opponent_avg_destruction(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    attacks = len(season_matches) * 3
    return float(sum(m.players[player_id].opponent_destruction for m in season_matches) / attacks)


def get_hitrate(season_matches: list[RecordedMatch], player_id: str) -> float:
    if not season_matches:
        return 0.0

    avg_stars = get_avg_stars(season_matches, player_id)
    avg_destruction = get_avg_destruction(season_matches, player_id)
    return avg_stars * 1000 + avg_destruction


def get_district_avg_stars(district_matches: list[RecordedMatch], district: str, player_id: str) -> float:
    if not district_matches:
        return 0.0

    return float(
        sum(m.players[player_id].districts[district].stars for m in district_matches) / len(district_matches))


def get_district_avg_destruction(district_matches: list[RecordedMatch], district: str, player_id: str) -> float:
    if not district_matches:
        return 0.0

    return float(
        sum(m.players[player_id].districts[district].destruction for m in district_matches) / len(district_matches))


def get_district_hitrate(district_matches: list[RecordedMatch], district: str, player_id: str) -> float:
    if not district_matches:
        return 0.0

    avg_stars = get_district_avg_stars(district_matches, district, player_id)
    avg_destruction = get_district_avg_destruction(district_matches, district, player_id)

    return avg_stars * 1000 + avg_destruction

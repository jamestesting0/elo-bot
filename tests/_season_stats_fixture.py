from datetime import UTC, datetime

from common.matches.season_stats import RecordedStatsRow


def timestamp(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


def row(record_id: str, played: int, districts: tuple[str, ...] = (), **changes) -> RecordedStatsRow:
    data = {
        "_id": record_id, "time": played,
        "players": {
            user: {"districts": {district: {"stars": 3, "destruction": 100} for district in districts}}
            for user in ("100", "200")
        },
    }
    return RecordedStatsRow.model_validate(data | changes)

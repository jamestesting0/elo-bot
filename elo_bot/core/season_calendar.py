from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

SEASON_START_MONTHS = (2, 4, 6, 8, 10, 12)
# Seasons turn over when the raid weekend ends: Monday 07:00 UTC.
RAID_HOUR_UTC = 7
SELECTION_BEFORE_END = timedelta(days=10)
QUEUE_CLOSE_BEFORE_END = timedelta(days=3)
# A season whose Final has not started three days after its selection date runs on to the next boundary.
EXTENSION_GRACE = timedelta(days=3)
# `/season reset` runs around a boundary, sometimes a day early; the season it opens ends at a later one.
RESET_TOLERANCE = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class SeasonCalendar:
    final_selection: datetime
    queue_closes: datetime
    season_ends: datetime


def season_start(year: int, month: int) -> datetime:
    first = datetime(year, month, 1, RAID_HOUR_UTC, tzinfo=UTC)
    return first + timedelta(days=-first.weekday() % 7)


def next_boundary(after: datetime) -> datetime:
    year, month = after.year, after.month
    while month not in SEASON_START_MONTHS or season_start(year, month) <= after:
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return season_start(year, month)


def season_calendar(now: datetime, last_reset: datetime | None, final_started: datetime | None) -> SeasonCalendar:
    ends = next_boundary(last_reset + RESET_TOLERANCE if last_reset else final_started or now)
    started_by = final_started or now
    while started_by > ends - SELECTION_BEFORE_END + EXTENSION_GRACE:
        ends = next_boundary(ends)
    return SeasonCalendar(final_selection=ends - SELECTION_BEFORE_END, queue_closes=ends - QUEUE_CLOSE_BEFORE_END,
                          season_ends=ends)

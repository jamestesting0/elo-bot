from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from common.constants import DISTRICT_CHOICES
from database_models import Season
from database_models.player_models import MatchDistrictStats

DISTRICTS: Final = tuple(choice.value for choice in DISTRICT_CHOICES)


@dataclass(frozen=True, slots=True)
class SeasonStatsError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class SeasonWindow:
    key: str
    start: int | None
    end: int

    @property
    def current(self) -> bool:
        return self.key == "current"

    @property
    def label(self) -> str:
        return "Current Season" if self.current else f"Season {self.key}"


def season_windows(seasons: Sequence[Season], read_time: int) -> list[SeasonWindow]:
    archives = sorted(seasons, key=lambda season: season.season_number, reverse=True)
    if any(s.start < 0 or s.end <= s.start or s.end > 32536799999 for s in archives):
        raise SeasonStatsError("The stored season dates are invalid.")
    if len({s.season_number for s in archives}) != len(archives):
        raise SeasonStatsError("The stored season numbers are ambiguous.")
    start = archives[0].end if archives else None
    return [SeasonWindow("current", start, read_time),
            *(SeasonWindow(str(s.season_number), s.start, s.end) for s in archives)]


class StatsParticipant(BaseModel):
    model_config = ConfigDict(frozen=True)
    districts: dict[str, MatchDistrictStats] = Field(default_factory=dict)


class RecordedStatsRow(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)
    id: str = Field(alias="_id")
    time: int = Field(strict=True)
    is_challenge: bool = False
    players: dict[str, StatsParticipant] = Field(min_length=2, max_length=2)
    operation_key: str | None = None
    manual_submission_key: str | None = None
    manual_submission_completed: bool = False


@dataclass(frozen=True, slots=True)
class ActivityPeak:
    start: date
    count: int
    ties: int


@dataclass(frozen=True, slots=True)
class ActivePlayer:
    user_id: str
    count: int
    ties: int


@dataclass(frozen=True, slots=True)
class SeasonStats:
    matches: int
    challenges: int
    active_players: int
    active_days: int
    daily_average: float | None
    day: ActivityPeak | None
    week: ActivityPeak | None
    leader: ActivePlayer | None
    districts: tuple[tuple[str, int], ...]
    missing_districts: int


def _peak(counts: Counter[date]) -> ActivityPeak | None:
    if not counts:
        return None
    count = max(counts.values())
    tied = [day for day, value in counts.items() if value == count]
    return ActivityPeak(min(tied), count, len(tied))


class StatsAccumulator:
    """Accumulate streamed matches; only this per-read builder mutates the counters."""

    def __init__(self) -> None:
        self.matches = 0
        self.challenges = 0
        self.players: Counter[str] = Counter()
        self.days: Counter[date] = Counter()
        self.weeks: Counter[date] = Counter()
        self.districts: Counter[str] = Counter()
        self.missing_districts = 0

    def add(self, record: RecordedStatsRow, window: SeasonWindow) -> None:
        lower = window.start if window.start is not None else -1
        if not lower < record.time < window.end:
            return
        played = datetime.fromtimestamp(record.time, UTC).date()
        self.matches += 1
        self.challenges += int(record.is_challenge)
        self.players.update(record.players.keys())
        self.days[played] += 1
        self.weeks[played - timedelta(days=played.weekday())] += 1
        districts = {district for player in record.players.values() for district in player.districts}
        self.districts.update(districts)
        self.missing_districts += int(not districts)

    def finish(self, window: SeasonWindow) -> SeasonStats:
        average = None
        if window.start is not None:
            first = datetime.fromtimestamp(window.start, UTC).date()
            last = datetime.fromtimestamp(window.end - 1, UTC).date()
            average = self.matches / max((last - first).days + 1, 1)
        leader = None
        if self.players:
            count = max(self.players.values())
            tied = sorted(user for user, value in self.players.items() if value == count)
            leader = ActivePlayer(tied[0], count, len(tied))
        district_order = {name: index for index, name in enumerate(DISTRICTS)}
        names = set(DISTRICTS) | self.districts.keys()
        districts = tuple((name, self.districts[name]) for name in sorted(
            names, key=lambda name: (-self.districts[name], district_order.get(name, len(DISTRICTS)), name),
        ))
        return SeasonStats(self.matches, self.challenges, len(self.players), len(self.days), average,
                           _peak(self.days), _peak(self.weeks), leader, districts, self.missing_districts)

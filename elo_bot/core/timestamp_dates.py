import calendar
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final


class TimestampInputError(Exception):
    pass


WEEKDAYS: Final = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTH_NAMES: Final = ("January", "February", "March", "April", "May", "June",
                      "July", "August", "September", "October", "November", "December")
_MONTHS: Final = {form: number for number, name in enumerate(MONTH_NAMES, start=1)
                  for form in (name.lower(), name[:3].lower())} | {"sept": 9}
_SMALL: Final = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                        "fifteen sixteen seventeen eighteen nineteen".split(), range(20), strict=True))
_TENS: Final = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10), strict=True))
_UNITS: Final = {
    "s": (0, 0, 1), "m": (0, 0, 60), "h": (0, 0, 3600), "d": (0, 1, 0),
    "w": (0, 7, 0), "month": (1, 0, 0), "y": (12, 0, 0),
}
_UNIT_ALIASES: Final = {
    alias: unit for unit, aliases in {
        "s": "s sec secs second seconds", "m": "m min mins minute minutes", "h": "h hr hrs hour hours",
        "d": "d day days", "w": "w wk wks week weeks", "month": "mo month months", "y": "y yr yrs year years",
    }.items() for alias in aliases.split()
}
DATE_WORDS: Final = frozenset((*_MONTHS, *_SMALL, *_TENS, *_UNIT_ALIASES, "a", "an", "thurs")) | frozenset(
    form for weekday in WEEKDAYS for form in (weekday.lower(), weekday[:3].lower())
)
_DURATION_PART: Final = re.compile(
    r"(?P<amount>[0-9]{1,6}\s*|[a-z]+(?:[ -][a-z]+)?\s+)(?P<unit>"
    + "|".join(sorted(_UNIT_ALIASES, key=len, reverse=True)) + r")(?![a-z])"
)
_DURATION_SEPARATOR: Final = re.compile(r"\s*(?:,\s*)?(?:and\s+)?")
_MONTH_FIRST: Final = re.compile(r"([a-z]+) ([0-9]{1,2})(?:st|nd|rd|th)?(?:,? ([0-9]{4}))?")
_DAY_FIRST: Final = re.compile(r"([0-9]{1,2})(?:st|nd|rd|th)? ([a-z]+)(?:,? ([0-9]{4}))?")
CALENDAR_OFFSETS: Final = {"this": 0, "next": 1, "last": -1, "previous": -1}
_PERIOD: Final = re.compile(r"(this|next|last|previous) (day|week|weekend|month|year)")
_PERIOD_EDGE: Final = re.compile(r"(start|beginning|end|first day|last day) of (.+)")
_PERIOD_ORDINAL: Final = re.compile(r"([0-9]{1,2})(?:st|nd|rd|th) (?:of )?(.+ month)")


@dataclass(frozen=True, slots=True)
class Duration:
    months: int
    days: int
    seconds: int

    def shift_date(self, day: date) -> date:
        year, month = divmod(day.year * 12 + day.month - 1 + self.months, 12)
        last_day = calendar.monthrange(year, month + 1)[1]
        return date(year, month + 1, min(day.day, last_day)) + timedelta(days=self.days)


def _number(text: str) -> int | None:
    if text.isdecimal():
        return int(text)
    if text in {"a", "an"}:
        return 1
    first, _, last = text.replace("-", " ").partition(" ")
    if first in _TENS and (not last or last in _SMALL and 0 < _SMALL[last] < 10):
        return _TENS[first] + _SMALL.get(last, 0)
    return _SMALL.get(text)


def parse_duration(text: str, *, sign: int = 1) -> Duration:
    months = days = seconds = 0
    following = text
    while part := _DURATION_PART.match(following):
        amount = _number(part["amount"].rstrip())
        if amount is None:
            break
        month, day, second = _UNITS[_UNIT_ALIASES[part["unit"]]]
        months += amount * month
        days += amount * day
        seconds += amount * second
        following = following[part.end():]
        if not following:
            return Duration(sign * months, sign * days, sign * seconds)
        separator = _DURATION_SEPARATOR.match(following)
        if separator:
            following = following[separator.end():]
    raise TimestampInputError("Include a duration such as `in 2 days` or `in 1h 30m`.")


def weekday_number(text: str) -> int | None:
    for number, name in enumerate(WEEKDAYS):
        if text in {name.lower(), name[:3].lower()} or text == "thurs" and number == 3:
            return number
    return None


def _period_bounds(text: str, today: date) -> tuple[date, date] | None:
    if (period := _PERIOD.fullmatch(text)) is None:
        return None
    qualifier, unit = period.groups()
    offset = CALENDAR_OFFSETS[qualifier]
    if unit == "day":
        day = today + timedelta(days=offset)
        return day, day
    if unit in {"week", "weekend"}:
        monday = today + timedelta(days=7 * offset - today.weekday())
        sunday = monday + timedelta(days=6)
        return (sunday - timedelta(days=1), sunday) if unit == "weekend" else (monday, sunday)
    if unit == "month":
        year, month = divmod(today.year * 12 + today.month - 1 + offset, 12)
        return date(year, month + 1, 1), date(year, month + 1, calendar.monthrange(year, month + 1)[1])
    year = today.year + offset
    return date(year, 1, 1), date(year, 12, 31)


def calendar_dates(text: str, today: date) -> tuple[date, ...] | None:
    if (edge := _PERIOD_EDGE.fullmatch(text)) and (bounds := _period_bounds(edge[2], today)):
        return (bounds[1] if edge[1] in {"end", "last day"} else bounds[0],)
    if (ordinal := _PERIOD_ORDINAL.fullmatch(text)) and (bounds := _period_bounds(ordinal[2], today)):
        day = int(ordinal[1])
        if not 1 <= day <= bounds[1].day:
            raise TimestampInputError("That day does not exist in the selected month.")
        return (bounds[0].replace(day=day),)
    if bounds := _period_bounds(text, today):
        first, last = bounds
        if text.endswith(" year"):
            raise TimestampInputError("Choose a date in that year, such as `start of next year`.")
        return tuple(first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return None


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _dates_for_year(today: date, year: str | None, month: int, day: int) -> tuple[date, ...]:
    if year:
        result = _valid_date(int(year), month, day)
        return (result,) if result else ()
    for candidate_year in range(today.year, min(10000, today.year + 9)):
        result = _valid_date(candidate_year, month, day)
        if result and result >= today:
            return (result,)
    return ()


def date_literals(text: str, today: date) -> tuple[date, ...] | None:
    """Return None for an unrecognized form and an empty tuple for an impossible date."""
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", text):
        year, month, day = map(int, text.split("-"))
        result = _valid_date(year, month, day)
        return (result,) if result else ()
    if numeric := re.fullmatch(r"([0-9]{1,2})[/.]([0-9]{1,2})(?:[/.]([0-9]{4}))?", text):
        first, second, year = numeric.groups()
        return tuple(sorted(set(_dates_for_year(today, year, int(first), int(second))
                                + _dates_for_year(today, year, int(second), int(first)))))
    if named := _MONTH_FIRST.fullmatch(text):
        month, day, year = named.groups()
    elif named := _DAY_FIRST.fullmatch(text):
        day, month, year = named.groups()
    else:
        return None
    return _dates_for_year(today, year, _MONTHS[month], int(day)) if month in _MONTHS else None

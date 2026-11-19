import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from elo_bot.core.timestamp_dates import (
    CALENDAR_OFFSETS, WEEKDAYS, Duration, TimestampInputError, calendar_dates, date_literals, parse_duration, weekday_number,
)
from elo_bot.core.timestamp_input import correction_for, normalize_timestamp_text


type Candidate = date | datetime
CHOICE_COUNT: Final = 7
MAX_CHOICES: Final = 25
_CLOCK_TEXT: Final = r"(?:noon|midnight|[0-9]{1,2}(?::[0-9]{2}(?::[0-9]{2})?)?\s*(?:[ap]\.?m\.?)?)"
_CLOCK: Final = re.compile(r"([0-9]{1,2})(?::([0-9]{2})(?::([0-9]{2}))?)?\s*([ap]\.?m\.?)?")
_TRAILING_CLOCK: Final = re.compile(rf"(.+?)[ t]({_CLOCK_TEXT})")
_LEADING_CLOCK: Final = re.compile(rf"({_CLOCK_TEXT})\s+(.+)")
_DAYS: Final = {"today": 0, "tonight": 0, "tomorrow": 1, "yesterday": -1,
                "day after tomorrow": 2, "day before yesterday": -2}
_PARTIAL_DURATION: Final = re.compile(r"(?:.+ )?in (?:[0-9]{1,6}|[a-z]+(?:[- ][a-z]+)?)")
_PARTIAL_DAY: Final = re.compile(r"(?P<week>this |next |last |previous )?(?P<day>[a-z]{2,9})(?P<rest> at .+)?")
_PARTIAL_CLOCK: Final = re.compile(r"(.*\b[0-9]{1,2}):([0-5]?)")
_BARE_DURATION: Final = re.compile(r"(?:[0-9]{1,6}\s*|[a-z]+(?:[ -][a-z]+)?\s+)[a-z]+(?:[ ,][a-z0-9 ,\-]+)?")
_WEEK_FIRST: Final = re.compile(r"(this|next|last|previous) week (?:on )?([a-z]+)")
_WEEK_LAST: Final = re.compile(r"([a-z]+) (this|next|last|previous) week")
_YEAR_COMPLETION: Final = re.compile(r"(?:the )?((?:this|next|last|previous) year)( at .+)?")
_PERIOD_UNITS: Final = ("day", "week", "weekend", "month", "year")


def parse_clock(text: str) -> tuple[time, ...]:
    if text == "noon":
        return (time(12),)
    if text == "midnight":
        return (time(0),)
    clock = _CLOCK.fullmatch(text)
    if clock is None:
        return ()
    hour_text, minute_text, second_text, meridiem = clock.groups()
    hour, minute, second = int(hour_text), int(minute_text or 0), int(second_text or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise TimestampInputError("Enter a time such as `19:00` or `7pm`.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise TimestampInputError("Use an hour from 1 to 12 with AM or PM.")
        return (time(hour % 12 + (12 if meridiem.startswith("p") else 0), minute, second),)
    if minute_text is None and (len(hour_text) == 1 and hour != 0 or hour == 12):
        return (time(hour % 12), time(hour % 12 + 12))
    return (time(hour, minute, second),)


def local_instants(local: datetime, zone: ZoneInfo) -> tuple[datetime, ...]:
    instants = {local.replace(tzinfo=zone, fold=fold).astimezone(UTC) for fold in (0, 1)}
    return tuple(sorted(value for value in instants if value.astimezone(zone).replace(tzinfo=None) == local))


@dataclass(frozen=True, slots=True)
class DateChoices:
    days: tuple[date, ...]
    recurring: bool = False


@dataclass(frozen=True, slots=True)
class LocalReference:
    now: datetime
    zone: ZoneInfo


def _recurring_dates(day: date, spacing: int) -> DateChoices:
    return DateChoices(tuple(day + timedelta(days=spacing * offset) for offset in range(CHOICE_COUNT + 2)), True)


def _relative_duration(text: str) -> Duration | None:
    if text.startswith("in "):
        return parse_duration(text[3:])
    if text.endswith(" from now"):
        return parse_duration(text.removesuffix(" from now"))
    if text.endswith(" ago"):
        return parse_duration(text[:-4], sign=-1)
    if _BARE_DURATION.fullmatch(text):
        try:
            return parse_duration(text)
        except TimestampInputError:
            return None
    return None


def _date_choices(text: str, today: date) -> DateChoices | None:
    text = text.removeprefix("the ").removesuffix(",")
    if text in _DAYS:
        return DateChoices((today + timedelta(days=_DAYS[text]),))
    if week := _WEEK_FIRST.fullmatch(text):
        text = f"{week[1]} {week[2]}"
    if week := _WEEK_LAST.fullmatch(text):
        text = f"{week[2]} {week[1]}"
    qualifier, _, weekday = text.partition(" ")
    if qualifier in CALENDAR_OFFSETS and (number := weekday_number(weekday)) is not None:
        return DateChoices((today + timedelta(days=7 * CALENDAR_OFFSETS[qualifier] + number - today.weekday()),))
    if (number := weekday_number(text)) is not None:
        return _recurring_dates(today + timedelta(days=(number - today.weekday()) % 7), 7)
    if dates := calendar_dates(text, today):
        return DateChoices(dates)
    if duration := _relative_duration(text):
        if duration.seconds:
            raise TimestampInputError("Use days or weeks with a clock time, such as `19:00 in 2 days`.")
        return DateChoices((duration.shift_date(today),))
    if (dates := date_literals(text, today)) is not None:
        if not dates:
            raise TimestampInputError("That date does not exist. Enter a date such as `2026-11-22`.")
        return DateChoices(dates)
    return None


def _combine(dates: DateChoices, clocks: tuple[time, ...], reference: LocalReference) -> tuple[Candidate, ...]:
    if not clocks:
        return dates.days[:CHOICE_COUNT if dates.recurring else MAX_CHOICES]
    results: set[datetime] = set()
    now_utc = reference.now.astimezone(UTC)
    for clock in clocks:
        days = 0
        for day in dates.days:
            instants = local_instants(datetime.combine(day, clock), reference.zone)
            if dates.recurring:
                instants = tuple(value for value in instants if value > now_utc)
            if instants:
                results.update(instants)
                days += 1
                if dates.recurring and days == CHOICE_COUNT:
                    break
    if not results:
        raise TimestampInputError("That time does not occur on that date in this time zone. Choose another time.")
    return tuple(sorted(results)[:MAX_CHOICES])


def _date_and_time(day_text: str, clock_text: str, reference: LocalReference) -> tuple[Candidate, ...]:
    clocks = parse_clock(clock_text)
    dates = _date_choices(day_text, reference.now.date())
    if dates and clocks:
        return _combine(dates, clocks, reference)
    raise TimestampInputError("Enter a date and time such as `Thursday at 19:00` or `19:00 in 2 days`.")


def _resolve(text: str, reference: LocalReference) -> tuple[Candidate, ...]:
    now = reference.now
    text = normalize_timestamp_text(text).removeprefix("at ")
    if text == "now":
        return (now.astimezone(UTC),)
    if clock := parse_clock(text):
        return _combine(_recurring_dates(now.date(), 1), clock, reference)
    if " at " in text:
        day_text, _, clock_text = text.partition(" at ")
        return _date_and_time(day_text, clock_text, reference)
    if " on " in text:
        clock_text, _, day_text = text.partition(" on ")
        if parse_clock(clock_text):
            return _date_and_time(day_text, clock_text, reference)
    if duration := _relative_duration(text):
        if duration.days or duration.months:
            day = duration.shift_date(now.date())
            instants = local_instants(datetime.combine(day, now.time()), reference.zone)
            if not instants:
                raise TimestampInputError("That local time does not occur on the target date. Include a clock time.")
        else:
            instants = (now.astimezone(UTC),)
        return tuple(value + timedelta(seconds=duration.seconds) for value in instants)
    if (leading := _LEADING_CLOCK.fullmatch(text)) and (dates := _date_choices(leading[2], now.date())) is not None:
        return _combine(dates, parse_clock(leading[1]), reference)
    if dates := _date_choices(text.removeprefix("on "), now.date()):
        return _combine(dates, (), reference)
    if trailing := _TRAILING_CLOCK.fullmatch(text):
        return _date_and_time(trailing[1], trailing[2], reference)
    raise TimestampInputError("Try `in two hours`, `Thursday at 19:00`, or `19:00 in 2 days`.")


def resolve_timestamp(text: str, anchor: datetime, zone: ZoneInfo) -> tuple[Candidate, ...]:
    try:
        return _resolve(text, LocalReference(anchor.astimezone(zone), zone))
    except (ValueError, OverflowError) as error:
        raise TimestampInputError("That date is out of range. Choose a closer date.") from error


def autocomplete_expressions(text: str) -> tuple[str, ...]:
    text = normalize_timestamp_text(text)
    if not text:
        return ("in 30 minutes", "in two hours", "tomorrow at 19:00")
    if text == "tm":
        return ("tomorrow",)
    if year := _YEAR_COMPLETION.fullmatch(text):
        return tuple(f"{edge} of {year[1]}{year[2] or ''}" for edge in ("start", "end"))
    if clock := _PARTIAL_CLOCK.fullmatch(text):
        return (f"{clock[1]}:{clock[2] or '0'}0",)
    if _PARTIAL_DURATION.fullmatch(text):
        units = ("hours", "days", "weeks") if text.startswith("in ") else ("days", "weeks", "months")
        return (text, *(f"{text} {unit}" for unit in units))
    if partial := _PARTIAL_DAY.fullmatch(text):
        prefix = partial["day"]
        week = partial["week"]
        if prefix not in _DAYS and weekday_number(prefix) is None and (not week or prefix not in _PERIOD_UNITS):
            days = (*(_PERIOD_UNITS if week else _DAYS), *(day.lower() for day in WEEKDAYS))
            completions = tuple(f"{partial['week'] or ''}{day}{partial['rest'] or ''}"
                                for day in days if day.startswith(prefix))
            if completions:
                return (text, *completions)
    if correction := correction_for(text):
        return (text, correction)
    return (text,)

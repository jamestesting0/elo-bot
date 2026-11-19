import re
from datetime import UTC, date, datetime
from typing import Final, assert_never
from zoneinfo import ZoneInfo

import interactions as ipy

from elo_bot.core.timestamp_dates import MONTH_NAMES, WEEKDAYS, TimestampInputError
from elo_bot.core.timestamp_input import correction_for, normalize_timestamp_text
from elo_bot.core.timestamps import MAX_CHOICES, Candidate, autocomplete_expressions, resolve_timestamp
from elo_bot.db.timestamp_models import TimestampDraft


_SELECTED: Final = re.compile(r"([td])\|([^|]{1,19})\|([^|]+)")
_UTC_ZONE: Final = ZoneInfo("UTC")


def candidate_label(candidate: Candidate, zone: ZoneInfo) -> str:
    match candidate:
        case datetime():
            local = candidate.astimezone(zone)
            clock = local.strftime("%H:%M:%S" if local.second else "%H:%M")
            offset = local.strftime("%z")
            return f"{_date_label(local.date())} · {clock} · {local.tzname()} (UTC{offset[:3]}:{offset[3:5]})"
        case date():
            return f"{_date_label(candidate)} · Add a time"
        case unreachable:
            assert_never(unreachable)


def _date_label(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]}, {day.day} {MONTH_NAMES[day.month - 1][:3]} {day.year}"


def timestamp_choices(text: str, anchor: datetime, zone: ZoneInfo | None) -> list[ipy.SlashCommandChoice]:
    choices: list[ipy.SlashCommandChoice] = []
    seen: set[Candidate] = set()
    for expression in autocomplete_expressions(text):
        try:
            candidates = resolve_timestamp(expression, anchor, zone or _UTC_ZONE)
        except TimestampInputError:
            continue
        if zone is None:
            value = expression if len(expression) <= 100 else text
            choices.append(ipy.SlashCommandChoice(name=f"{expression.capitalize()[:75]} · Select country", value=value))
            continue
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            match candidate:
                case datetime():
                    value = f"t|{int(candidate.timestamp())}|{zone.key}"
                case date():
                    value = f"d|{candidate.isoformat()}|{zone.key}"
                case unreachable:
                    assert_never(unreachable)
            label = candidate_label(candidate, zone)
            if expression != text.strip().lower():
                label = f"{expression.capitalize()} · {label}"
            choices.append(ipy.SlashCommandChoice(name=label[:100], value=value))
    return choices[:MAX_CHOICES]


def submitted_candidates(expression: str, anchor: datetime, zone: ZoneInfo) -> tuple[Candidate, ...]:
    selected = _SELECTED.fullmatch(expression)
    if selected is None:
        try:
            return resolve_timestamp(expression, anchor, zone)
        except TimestampInputError as error:
            correction = correction_for(normalize_timestamp_text(expression))
            if correction is None:
                raise
            try:
                resolve_timestamp(correction, anchor, zone)
            except TimestampInputError:
                raise error from None
            raise TimestampInputError(
                f"Did you mean `{correction}`? Select it in the `when` suggestions or enter the corrected text."
            ) from None
    kind, value, selected_zone = selected.groups()
    if selected_zone != zone.key:
        raise TimestampInputError("The time zone changed. Choose a new suggestion for `when`.")
    try:
        if kind == "t":
            return (datetime.fromtimestamp(int(value), UTC),)
        return (date.fromisoformat(value),)
    except (ValueError, OverflowError, OSError) as error:
        raise TimestampInputError("That suggestion is invalid. Choose a new suggestion for `when`.") from error


def candidate_prompt(draft: TimestampDraft, candidates: tuple[Candidate, ...]) -> tuple[str, ipy.Button | ipy.StringSelectMenu]:
    custom_id = f"timestamp_choose|{draft.id}|{draft.owner_id}"
    if len(candidates) == 1:
        return (f"Enter a time for **{_date_label(candidates[0])}**.",
                ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Enter Time", custom_id=custom_id))
    zone = ZoneInfo(draft.zone)
    return "Choose a date or time for your timestamp.", ipy.StringSelectMenu(
        *(ipy.StringSelectOption(label=candidate_label(candidate, zone), value=str(index))
          for index, candidate in enumerate(candidates)),
        placeholder="🕒 Select a date or time",
        custom_id=custom_id,
    )

import re
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from elobot.db import Match


MATCH_RESULT_ENTRY_COUNT: Final = 3
_MATCH_RESULT_ENTRY_PATTERN: Final = re.compile(
    r"(?P<stars>[0-9]+)\.(?P<destruction>[0-9]+)"
)


def is_valid_match_result_entry(value: str) -> bool:
    match = _MATCH_RESULT_ENTRY_PATTERN.fullmatch(value)
    if match is None:
        return False

    stars = int(match.group("stars"))
    destruction = int(match.group("destruction"))
    return 0 <= stars <= 3 and 0 <= destruction <= 100


def is_valid_complete_match_result(value: str, entry_count: int = MATCH_RESULT_ENTRY_COUNT) -> bool:
    entries = re.split(r"\r?\n", value)
    return (
        len(entries) == entry_count
        and all(is_valid_match_result_entry(entry) for entry in entries)
    )


def is_match_finished(match: "Match", player: int | None = None) -> bool:
    if isinstance(getattr(match, "final_result_id", None), str) or getattr(match, "final_superseded", False) is True:
        return True
    if getattr(match, "is_autowin", False) is True or getattr(match, "autowin_completed", False) is True:
        return True
    if isinstance(getattr(match, "recorded_match_id", None), str):
        return True
    operation = getattr(match, "result_operation", None)
    if operation is not None and getattr(operation, "completed", False) is True:
        return True

    participants = match.participants[player:player + 1] if player is not None else match.participants[:2]
    expected_count = 1 if player is not None else 2
    district_count = 7 if getattr(match, "format", None) == "elo_final_seven" else 3
    return len(participants) == expected_count and all(
        len(getattr(participant, "result", [])) == district_count and all(participant.result)
        for participant in participants
    )


type ResultDetectionType = Literal["Strict", "Relaxed"]

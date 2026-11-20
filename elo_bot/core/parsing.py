import re
from typing import Final


_MAX_SCHEDULE_TIMESTAMP: Final = 32_536_799_999
_UNIX_SECONDS: Final = re.compile(r"[0-9]{1,11}")
_DISCORD_TIMESTAMP: Final = re.compile(r"<t:([0-9]{1,11})(?::[tTdDfFsSR])?>")


def future_schedule_timestamp(text: str, now: float, *, from_message: bool = False) -> int | None:
    """Read a slash argument or find the first future Discord timestamp in message text."""
    if from_message:
        candidates = (int(tag[1]) for tag in _DISCORD_TIMESTAMP.finditer(text))
        return next((ts for ts in candidates if now < ts <= _MAX_SCHEDULE_TIMESTAMP), None)

    value = text.strip().strip("`").strip()
    if tag := _DISCORD_TIMESTAMP.fullmatch(value):
        value = tag[1]
    if _UNIX_SECONDS.fullmatch(value) is None:
        return None
    ts = int(value)
    return ts if now < ts <= _MAX_SCHEDULE_TIMESTAMP else None


def get_stars(string: str) -> int:
    return int(string.split(".")[0])


def get_destruction(string: str) -> int:
    return int(string.split(".")[1])


def extract_integer(input_string: str, index: int = 0) -> int | None:
    if not input_string:
        return 0

    if isinstance(input_string, int):
        return input_string

    match = re.findall(r'\d+', input_string)
    if match:
        return int(match[index])

    return 0


def validate_dict(data: dict):
    if not isinstance(data, dict):
        return False

    player_result_keys = {
        "stars": int,
        "destruction": int,
        "opponent": str,
        "eloChange": int,
        "won": bool,
        "districts": list,
    }

    district_keys = {
        "name": str,
        "stars": int,
        "destruction": int
    }

    for i in range(2):
        player_id = list(data.keys())[i]
        player_result = data[player_id]
        for key, expected_type in player_result_keys.items():
            if key not in player_result or not isinstance(player_result[key], expected_type):
                return False

        for district in player_result["districts"]:
            if not isinstance(district, dict):
                return False

            for key, expected_type in district_keys.items():
                if key not in district or not isinstance(district[key], expected_type):
                    return False

    return True

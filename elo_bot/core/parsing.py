import re


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

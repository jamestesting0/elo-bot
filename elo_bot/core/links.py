import re

from elo_bot.config.presentation import PLAYER_LINK_PREFIX


def generate_player_link(tag: str):
    return PLAYER_LINK_PREFIX + tag.replace("#", "")


def format_base_link(link: str) -> str:
    return f"[Base Link]({link})"


def extract_base_link(text: str) -> str:
    first_line = text.partition("\n")[0]
    match = re.fullmatch(r"\[Base Link\]\((.+)\)", first_line)
    return match.group(1) if match else first_line

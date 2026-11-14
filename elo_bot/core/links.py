from elo_bot.config.presentation import PLAYER_LINK_PREFIX


def generate_player_link(tag: str):
    return PLAYER_LINK_PREFIX + tag.replace("#", "")

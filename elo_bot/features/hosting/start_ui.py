import interactions as ipy

from elo_bot.config.game import HOST_CLAN_OPTIONS
from elo_bot.db import Match


def spectate_button(custom_id: str) -> ipy.Button:
    return ipy.Button(style=ipy.ButtonStyle.GRAY, label="Spectate", emoji="👀", custom_id=custom_id)


def start_components(match: Match, player: int, *, show_clan_select: bool) -> list[ipy.ActionRow]:
    token = match.token
    rows = []
    if show_clan_select:
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(
            *HOST_CLAN_OPTIONS, placeholder="🛡️ Set the host clan", custom_id=f"host_clan_select|{token}|{player}",
        )))
    buttons = [
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Host", emoji="🙋", custom_id=f"match_host|{token}|{player}"),
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Store Result", emoji="💾",
                   custom_id=f"match_result_store|{token}|{player}"),
    ]
    if match.is_war:
        buttons.append(spectate_button(f"war_spectate|{token}|{player}|{match.times[player]}"))
    buttons.append(
        ipy.Button(style=ipy.ButtonStyle.RED, label="Abort", emoji="🛑", custom_id=f"match_abort|{token}|{player}"),
    )
    rows.append(ipy.ActionRow(*buttons))
    return rows

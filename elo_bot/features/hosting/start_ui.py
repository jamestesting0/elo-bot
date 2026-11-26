from copy import deepcopy

import interactions as ipy

from elo_bot.config.game import HOST_CLAN_OPTIONS
from elo_bot.db import Match

SCHEDULED_START = "scheduled_match_start"


def scheduled_start_message(ctx: ipy.InteractionContext) -> ipy.Message | None:
    if isinstance(ctx, ipy.ComponentContext) and str(getattr(ctx, "custom_id", "")).startswith(f"{SCHEDULED_START}|"):
        return ctx.message
    return None


def scheduled_thread_embed(match: Match, player: int, message: ipy.Message) -> ipy.Embed:
    publication = match.message_publications[player]
    if publication and publication.message_id == int(message.id) and publication.embeds:
        return ipy.Embed.from_dict(deepcopy(publication.embeds[0]))
    return deepcopy(message.embeds[0])


def spectate_button(custom_id: str) -> ipy.Button:
    return ipy.Button(style=ipy.ButtonStyle.GRAY, label="Spectate", emoji="👀", custom_id=custom_id)


def start_components(match: Match, player: int, *, show_clan_select: bool, scheduled: bool = False) -> list[ipy.ActionRow]:
    token = match.token
    rows = []
    if show_clan_select:
        clan = match.scheduled_host_clans[player] if scheduled else match.host_clans[player]
        options = [ipy.StringSelectOption(label=option.label, value=option.value, default=option.value == clan)
                   for option in HOST_CLAN_OPTIONS]
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(
            *options, placeholder="🛡️ Set the host clan", custom_id=f"host_clan_select|{token}|{player}",
        )))
    buttons = [
        ipy.Button(style=ipy.ButtonStyle.GRAY, label="Host", emoji="🙋", custom_id=f"match_host|{token}|{player}"),
    ]
    if scheduled:
        buttons.append(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Start Match", emoji="⚔️",
                                  custom_id=f"{SCHEDULED_START}|{token}|{player}"))
    else:
        buttons.append(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Store Result", emoji="💾",
                                  custom_id=f"match_result_store|{token}|{player}"))
    if match.is_war and not scheduled:
        buttons.append(spectate_button(f"war_spectate|{token}|{player}|{match.times[player]}"))
    buttons.append(
        ipy.Button(style=ipy.ButtonStyle.RED, label="Abort", emoji="🛑", custom_id=f"match_abort|{token}|{player}"),
    )
    rows.append(ipy.ActionRow(*buttons))
    return rows

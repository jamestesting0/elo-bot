import time

import interactions as ipy

from elo_bot.config.emojis import LOADING_EMOJI
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import Match
from elo_bot.features.finals.scouting import scout_opens_at


def scout_opening_text(opens: int | None, *, paused: bool) -> str:
    if opens is not None:
        return f"Scouting opens <t:{opens}:R>."
    if paused:
        return "Scouting opens 30 minutes before the rescheduled attack."
    return "Scouting opens 30 minutes before the scheduled match."


def scout_panel(match: Match, owner: str, attacker: int, district: int, *, manager: bool
                ) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    embed = ipy.Embed(title=f"**{FINAL_DISTRICTS[district]}**", color=embed_color(),
                      footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()))
    opens = scout_opens_at(match, attacker)
    if opens is None or time.time() < opens:
        embed.description = scout_opening_text(opens, paused=attacker in match.final_scouting_paused_sides)
    else:
        base = match.participants[1 - attacker].bases[district]
        if base and base.scout_view:
            embed.set_image(base.image)
        else:
            embed.description = f"{LOADING_EMOJI} The scout view is being prepared."

    prefix = f"finalscout|{owner}|{match.id}"
    controls = [ipy.ActionRow(ipy.StringSelectMenu(
        *(ipy.StringSelectOption(label=name, value=f"{attacker}|{index}", default=index == district)
          for index, name in enumerate(FINAL_DISTRICTS)),
        placeholder="🗺️ Select a district", custom_id=f"{prefix}|district",
    ))]
    if manager:
        controls.append(ipy.ActionRow(ipy.StringSelectMenu(
            *(ipy.StringSelectOption(label=f"Player {side + 1}'s view", value=f"{side}|{district}",
                                     description=f"Scout Player {2 - side}'s bases", default=side == attacker)
              for side in (0, 1)),
            placeholder="👁️ Select a player’s view", custom_id=f"{prefix}|view",
        )))
    return embed, controls

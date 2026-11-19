from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import interactions as ipy

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.features.seasons.ongoing import ongoing_season_calendar

README_SECTIONS: Final = {
    "getting_started": "Getting Started",
    "match_styles": "Match Styles",
    "districts_and_bases": "Districts & Bases",
    "penalties": "Rules & Penalties",
    "seasons": "Seasons",
    "elo_final": "Elo Final",
}


async def readme_page(section: str, user_id: int) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    title = README_SECTIONS[section]
    description = Path(f"assets/embeds/readme/{section}.md").read_text(encoding="utf-8")
    if section == "seasons":
        calendar = await ongoing_season_calendar(datetime.now(UTC))
        dates = "\n".join(
            f"**{label}:** <t:{int(moment.timestamp())}:D> · <t:{int(moment.timestamp())}:R>"
            for label, moment in (("Elo Final selection", calendar.final_selection),
                                 ("Queue closes", calendar.queue_closes),
                                 ("Season ends", calendar.season_ends))
        )
        description = f"{dates}\n\n{description}"
    embed = ipy.Embed(
        title=f"**README · {title}**",
        description=description,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    menu = ipy.StringSelectMenu(
        *(ipy.StringSelectOption(label=label, value=value, default=value == section)
          for value, label in README_SECTIONS.items()),
        placeholder="📖 Select a section",
        custom_id=f"readme_section|{user_id}",
    )
    return embed, [ipy.ActionRow(menu)]

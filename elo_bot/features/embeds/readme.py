from pathlib import Path
from typing import Final

import interactions as ipy

from elo_bot.config.emojis import render_emoji_markup
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT

README_SECTIONS: Final = {
    "getting_started": "Getting Started",
    "match_styles": "Match Styles",
    "districts_and_bases": "Districts & Bases",
    "penalties": "Rules & Penalties",
    "seasons": "Seasons",
    "elo_final": "Elo Final",
}


def readme_page(section: str, user_id: int) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    title = README_SECTIONS[section]
    description = render_emoji_markup(Path(f"assets/embeds/readme/{section}.md").read_text(encoding="utf-8"))
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

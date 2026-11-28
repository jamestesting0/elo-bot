from dataclasses import dataclass
from typing import TypedDict

import interactions as ipy

from elo_bot.db import BaseLayout
from elo_bot.discord.embeds import embed_color, v2_footer_text
from elo_bot.features.bases.layouts.ui import _base_details_embed, _base_names, _defense_log_line


_DEFENSES_PER_PAGE = 7


@dataclass(frozen=True, slots=True)
class BaseListState:
    district: str
    index: int
    embed_type: str
    quality: str
    builder_id: str
    total: int


class BaseListPayload(TypedDict, total=False):
    embed: ipy.Embed
    embeds: list[ipy.Embed]
    components: list[ipy.BaseComponent]
    allowed_mentions: ipy.AllowedMentions


def base_list_card(
        embed: ipy.Embed, image_url: str, *log_components: ipy.TextDisplayComponent | ipy.ActionRow,
) -> ipy.ContainerComponent:
    header = ipy.TextDisplayComponent(f"{embed.title}\n{embed.description}")
    details = [ipy.TextDisplayComponent(f"{field.name}\n{field.value}") for field in embed.fields]
    image = ipy.MediaGalleryComponent([ipy.MediaGalleryItem(ipy.UnfurledMediaItem(url=image_url))])
    footer = ipy.TextDisplayComponent(v2_footer_text())
    return ipy.ContainerComponent(header, *details, *log_components, image, footer, accent_color=embed_color())


def _defense_page_controls(base_id: str, page: int, pages: int) -> ipy.ActionRow:
    return ipy.ActionRow(
        ipy.Button(
            style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 0,
            custom_id=f"base_defense|{base_id}|{max(0, page - 1)}|newer",
        ),
        ipy.Button(
            style=ipy.ButtonStyle.SECONDARY, label=f"{page + 1}/{pages}", disabled=True,
            custom_id="base_defense_page_number",
        ),
        ipy.Button(
            style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page == pages - 1,
            custom_id=f"base_defense|{base_id}|{min(pages - 1, page + 1)}|older",
        ),
    )


async def base_defense_card(guild: ipy.Guild, base: BaseLayout, page: int) -> ipy.ContainerComponent:
    pages = max(1, (len(base.records) + _DEFENSES_PER_PAGE - 1) // _DEFENSES_PER_PAGE)
    page = min(page, pages - 1)
    end = len(base.records) - page * _DEFENSES_PER_PAGE
    records = base.records[max(0, end - _DEFENSES_PER_PAGE):end]
    names = await _base_names(guild, base.builder_id, {record[0] for record in records})
    embed = _base_details_embed(base, names)
    log = "\n".join(_defense_log_line(record, names) for record in reversed(records))
    contents: list[ipy.TextDisplayComponent | ipy.ActionRow] = [
        ipy.TextDisplayComponent(f"**Defense Log**\n{log or 'No defenses so far'}"),
    ]
    if pages > 1:
        contents.append(_defense_page_controls(str(base.id), page, pages))
    return base_list_card(embed, base.image_url, *contents)

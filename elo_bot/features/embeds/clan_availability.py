import re
from urllib.parse import quote

import interactions as ipy
from interactions.client.const import EMBED_FIELD_VALUE_LENGTH, EMBED_MAX_DESC_LENGTH

from elo_bot.config.presentation import CLAN_LINK_PREFIX
from elo_bot.db import Clan


def clan_availability_entry(clan: Clan, status: str) -> str:
    name = re.sub(r"([\\\[\]*_~`|])", r"\\\1", clan.name)
    url = CLAN_LINK_PREFIX + quote(clan.tag.removeprefix("#"), safe="")
    return f"**[{name}]({url})**\n{status}"


def set_clan_availability_entries(embed: ipy.Embed, entries: list[str]) -> None:
    blocks = [""]
    for entry in entries:
        combined = "\n\n".join(filter(None, (blocks[-1], entry)))
        limit = EMBED_MAX_DESC_LENGTH if len(blocks) == 1 else EMBED_FIELD_VALUE_LENGTH
        if len(combined) > limit:
            blocks.append(entry)
        else:
            blocks[-1] = combined

    embed.description = blocks[0]
    embed.fields = [ipy.EmbedField(name="\u200b", value=block) for block in blocks[1:]]

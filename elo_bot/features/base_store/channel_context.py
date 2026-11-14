from dataclasses import dataclass

import interactions as ipy
from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class ScheduledBaseStoreContext:
    """Publish scheduled storage through the normal controls without an interaction token."""
    bot: ipy.Client
    channel: ipy.ThreadChannel

    async def send(self, content: str | None = None, *, embed: ipy.Embed | None = None,
                   components: list[ipy.Button] | None = None, ephemeral: bool = False) -> ipy.Message:
        return await self.channel.send(content=content, embed=embed, components=components or [])

    async def edit(self, message: ipy.Message, *,
                   embed: ipy.Embed | dict[str, JsonValue] | list[dict[str, JsonValue]],
                   components: list[ipy.Button]) -> ipy.Message:
        return await message.edit(embed=embed, components=components)

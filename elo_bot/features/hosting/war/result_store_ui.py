from collections.abc import Sequence
from dataclasses import dataclass

import interactions as ipy

from elo_bot.config.emojis import CLASHING_SWORD_EMOJI, STAR_EMOJI
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.db import Match


type MessageComponents = ipy.BaseComponent | list[ipy.BaseComponent] | None


@dataclass(frozen=True, slots=True)
class StoredResultDmRequest:
    match: Match
    player: int
    user: ipy.User


@dataclass(frozen=True, slots=True)
class ScheduledResultStoreContext:
    author: ipy.SnowflakeObject
    channel: ipy.ThreadChannel

    async def send(
            self,
            content: str,
            *,
            components: MessageComponents = None,
            ephemeral: bool = False,
    ) -> ipy.Message:
        return await self.channel.send(content, components=components)

    @staticmethod
    async def edit(
            message: ipy.Message,
            *,
            content: str | None = None,
            components: MessageComponents = None,
    ) -> ipy.Message:
        if content is None:
            return await message.edit(components=components)
        return await message.edit(content=content, components=components)

    @staticmethod
    async def delete(message: ipy.Message) -> None:
        await message.delete()


def format_result_summary(
        districts: Sequence[str | None],
        scores: Sequence[str | None],
) -> str:
    result = ""
    for district, score in zip(districts, scores):
        if district is None or score is None:
            continue
        stars, destruction = score.split(".")
        result += f"{district}: {stars}{STAR_EMOJI} {destruction}%\n"
    return result


async def send_stored_result_dm(request: StoredResultDmRequest) -> None:
    match = request.match
    result = format_result_summary(match.final_districts, match.participants[request.player].result)

    embed = ipy.Embed(
        title="**Match Result Stored**",
        description=f"Result for <#{match.thread}> is stored, you may queue again!",
        fields=[ipy.EmbedField(name="**Result**", value=result)],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    play_again_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Play Again",
        custom_id="match_queue",
        emoji=CLASHING_SWORD_EMOJI,
    )
    await request.user.send(embed=embed, components=ipy.ActionRow(play_again_button))


async def notify_stored_result(request: StoredResultDmRequest) -> None:
    try:
        await send_stored_result_dm(request)
    except ipy.errors.Forbidden:
        return

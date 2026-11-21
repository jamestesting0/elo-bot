from datetime import UTC, datetime
from io import BytesIO
import logging

import anyio
import interactions as ipy

from elo_bot.config.emojis import application_emoji, render_emoji_markup
from elo_bot.config.presentation import BANNER_IMAGE, COLOR, FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.core.finals import FinalError
from elo_bot.features.embeds.elo_kings import elo_king_avatars, elo_king_rows
from elo_bot.features.finals.rendering import render_elo_kings
from elo_bot.features.seasons.ongoing import ongoing_season_calendar

STATIC_EMBED_TYPES = ("Elo Information", "Elo Final")


async def static_embed(embed_type: str, bot: ipy.Client) -> tuple[ipy.Embed, list[ipy.ActionRow], ipy.File | None]:
    """Build preset /embed posts for initial publication and sticky reposts."""
    match embed_type:
        case "Elo Information":
            return await elo_information(bot)
        case "Elo Final":
            embed, controls = elo_final()
            return embed, controls, None
    raise ValueError(f"Unknown static embed: {embed_type}")


async def elo_information(bot: ipy.Client) -> tuple[ipy.Embed, list[ipy.ActionRow], ipy.File | None]:
    calendar = await ongoing_season_calendar(datetime.now(UTC))
    dates = "\n".join(
        f"**{label}** · <t:{int(moment.timestamp())}:D> · <t:{int(moment.timestamp())}:R>"
        for label, moment in (
            ("Elo Final", calendar.final_selection),
            ("Queue closes", calendar.queue_closes),
            ("Season ends", calendar.season_ends),
        )
    )
    with open("assets/embeds/information.md", encoding="utf-8") as source:
        information = source.read()
    description = render_emoji_markup(information.format(season_dates=dates))
    embed = ipy.Embed(
        title="**Clan Capital Elo System**",
        description=description,
        footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=footer_image()),
        images=[ipy.EmbedAttachment(url=BANNER_IMAGE)],
        color=COLOR,
    )
    file = None
    rows = await elo_king_rows(information)
    if rows:
        avatars = await elo_king_avatars(bot, rows)
        portraits = [(row.label, row.highest_elo, avatars.get(row.user_id)) for row in rows]
        try:
            rendered = await anyio.to_thread.run_sync(render_elo_kings, portraits)
        except (OSError, FinalError):
            logging.exception("Could not render the Elo Kings leaderboard")
        else:
            file = ipy.File(BytesIO(rendered), file_name="elo-kings.png",
                            description="Elo Kings by highest Elo. " +
                            ", ".join(f"{row.label} {row.highest_elo}" for row in rows))
            embed.set_image(url="attachment://elo-kings.png")

    participate_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Participate",
        custom_id="participate_button",
        emoji=application_emoji("war")
    )

    build_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Base Building",
        custom_id="build_button",
        emoji=application_emoji("grandbuilder")
    )

    rewards_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Rewards",
        custom_id="rewards_button",
        emoji=application_emoji("goldpass")
    )

    support_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Support",
        custom_id="support_button",
        emoji="🛠️"
    )

    readme_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="README",
        custom_id="readme_button",
        emoji="📖"
    )

    video_button = ipy.Button(
        style=ipy.ButtonStyle.URL,
        label="Youtube",
        url="https://www.youtube.com/watch?v=1bUDs5g-YOE&list=PLCBSjHpUFbfGiwqK-xf1R5FdGqrFBf3rk&pp=gAQB",
        emoji=application_emoji("youtube")
    )
    return embed, [
        ipy.ActionRow(participate_button, support_button),
        ipy.ActionRow(rewards_button, build_button),
        ipy.ActionRow(readme_button, video_button),
    ], file


def elo_final() -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    embed = ipy.Embed(
        title="**Elo Final**",
        description="At the end of every season, the top Elo players meet in a knockout bracket for the "
                    "Elo Kings title. Predict the bracket and bet tokens on each match.",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=footer_image()),
        color=COLOR,
    )
    details_button = ipy.Button(style=ipy.ButtonStyle.GRAY, label="Details", emoji="📅",
                                custom_id="elo_final_details")
    return embed, [ipy.ActionRow(details_button)]

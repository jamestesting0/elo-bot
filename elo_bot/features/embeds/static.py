from datetime import UTC, datetime

import interactions as ipy

from elo_bot.config.emojis import application_emoji, render_emoji_markup
from elo_bot.config.presentation import BANNER_IMAGE, COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.features.seasons.ongoing import ongoing_season_calendar

STATIC_EMBED_TYPES = ("Elo Information", "Elo Final")


async def static_embed(embed_type: str) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    """Build preset /embed posts for initial publication and sticky reposts."""
    match embed_type:
        case "Elo Information":
            return await elo_information()
        case "Elo Final":
            return elo_final()
    raise ValueError(f"Unknown static embed: {embed_type}")


async def elo_information() -> tuple[ipy.Embed, list[ipy.ActionRow]]:
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
        description = render_emoji_markup(source.read().format(season_dates=dates))
    embed = ipy.Embed(
        title="**Clan Capital Elo System**",
        description=description,
        footer=ipy.EmbedFooter(text="Clan capital elo system", icon_url=FOOTER_IMAGE),
        images=[ipy.EmbedAttachment(url=BANNER_IMAGE)],
        color=COLOR,
    )

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
    ]


def elo_final() -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    embed = ipy.Embed(
        title="**Elo Final**",
        description="At the end of every season, the top Elo players meet in a knockout bracket for the "
                    "Elo Kings title. Predict the bracket and bet tokens on each match.",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )
    details_button = ipy.Button(style=ipy.ButtonStyle.GRAY, label="Details", emoji="📅",
                                custom_id="elo_final_details")
    return embed, [ipy.ActionRow(details_button)]

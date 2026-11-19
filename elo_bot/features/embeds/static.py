import interactions as ipy

from elo_bot.config.presentation import BANNER_IMAGE, COLOR, FOOTER_IMAGE, FOOTER_TEXT

STATIC_EMBED_TYPES = ("Elo Information", "Elo Final")


def static_embed(embed_type: str) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    """Fixed /embed posts; sticky reposts rebuild them from here so their buttons keep working."""
    match embed_type:
        case "Elo Information":
            return elo_information()
        case "Elo Final":
            return elo_final()
    raise ValueError(f"Unknown static embed: {embed_type}")


def elo_information() -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    with open("assets/embeds/information.md") as source:
        description = source.read()
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
        emoji=ipy.PartialEmoji(name="war", id=1050832305410805760)
    )

    build_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Base Building",
        custom_id="build_button",
        emoji=ipy.PartialEmoji(name="grandbuilder", id=1294298302069739642)
    )

    rewards_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Rewards",
        custom_id="rewards_button",
        emoji=ipy.PartialEmoji(name="goldpass", id=947797613825105950)
    )

    support_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Support",
        custom_id="support_button",
        emoji="🛠️"
    )

    rules_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Rules",
        custom_id="rules_button",
        emoji="📜"
    )

    docs_button = ipy.Button(
        style=ipy.ButtonStyle.URL,
        label="Documentation",
        url="https://docs.google.com/document/d/1__Ufp_KCFrTaiFMvEouafyGn08quPKkIo50OFvswkUw/edit#heading=h"
            ".kz8yj8uavt8i",
        emoji=ipy.PartialEmoji(name="googledocs", id=1294293700775641109)
    )

    video_button = ipy.Button(
        style=ipy.ButtonStyle.URL,
        label="Youtube",
        url="https://www.youtube.com/watch?v=1bUDs5g-YOE&list=PLCBSjHpUFbfGiwqK-xf1R5FdGqrFBf3rk&pp=gAQB",
        emoji=ipy.PartialEmoji(name="youtube", id=1124367780788056074)
    )
    return embed, [
        ipy.ActionRow(participate_button, support_button),
        ipy.ActionRow(rewards_button, build_button),
        ipy.ActionRow(rules_button, docs_button),
        ipy.ActionRow(video_button),
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

import asyncio

import interactions as ipy

from elobot.config.emojis import BASE_QUALITY_EMOJIS, ERROR_EMOJI, LOADING_EMOJI, RANK_EMOJIS, SUCCESS_EMOJI
from elobot.config.game import DISTRICT_CHOICES
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.db import BaseLayout
from elobot.discord.mentions import mention_user
from elobot.discord.pagination import build_page_button


def _fingerprint_status_emoji(status: str) -> ipy.PartialEmoji:
    if status in {"pending", "running"}:
        return LOADING_EMOJI
    if status in {"ready", "succeeded", "completed"}:
        return SUCCESS_EMOJI
    return ERROR_EMOJI


def _builder_names(bot: ipy.Client) -> dict[str, str]:
    names = getattr(bot, "builder_names", None)
    if names is None:
        names = {}
        bot.builder_names = names
    return names


def _cached_user_name(user) -> str | None:
    if user is None:
        return None
    tag = getattr(user, "tag", None)
    if isinstance(tag, str) and tag:
        return tag
    username = getattr(user, "username", None)
    if isinstance(username, str) and username:
        return username if username.startswith("@") else f"@{username}"
    name = str(user)
    return name if name else None


def _builder_choice_label(bot: ipy.Client, guild: ipy.Guild | None, builder_id: str | None) -> str:
    if not builder_id:
        return "@anonymous"
    builder_id = str(builder_id)
    names = _builder_names(bot)
    cached_name = names.get(builder_id)
    if cached_name:
        return cached_name
    cached = guild.get_member(builder_id) if guild is not None else None
    if cached is None:
        cached = bot.get_user(builder_id)
    name = _cached_user_name(cached)
    if name:
        names[builder_id] = name
        return name
    return f"<@{builder_id}>"


def _parse_base_lb_title(title: str):
    """extract district and optional quality from embed title."""
    cleaned = title.replace("**", "")  # "Best Performing X Bases" or "Best Performing X Bases (Quality)"
    cleaned = cleaned.replace("Best Performing ", "")

    quality = None
    if cleaned.endswith(")"):
        # extract quality from parentheses
        base_part, quality = cleaned.rsplit("(", 1)
        quality = quality.rstrip(")")
        cleaned = base_part.strip()

    district = cleaned.replace(" Bases", "")
    return district, quality


async def construct_base_leaderboard_string(bot: ipy.Client, bases_sorted: list[BaseLayout], starting_index: int = 0):
    leaderboard = ""
    index = 0
    for base in bases_sorted[starting_index:]:
        if index >= 10:
            break

        if not base.builder_id:
            builder_mention = mention_user(None, base.builder_id, mention_type="Builder")
        else:
            builder = await bot.fetch_user(base.builder_id)
            builder_mention = mention_user(builder)

        rank_index = starting_index + index
        if rank_index > 2:
            emoji = f"**#{rank_index + 1}**"
        else:
            emoji = RANK_EMOJIS[rank_index]

        entries = len(base.records)
        if entries > 0:
            avg_stars = sum(record[1] for record in base.records) / entries
            avg_destruction = sum(record[2] for record in base.records) / entries
        else:
            avg_stars = 0
            avg_destruction = 0

        leaderboard += (f"{emoji} {builder_mention} `{entries}` → "
                        f"`{avg_stars:.2f}⭐ {avg_destruction:.2f}%`\n")

        index += 1

    return leaderboard


async def base_embed_names(guild: ipy.Guild, base: BaseLayout, embed_type: str) -> dict[str, str]:
    user_ids = {record[0] for record in base.records[-10:]} if embed_type == "Defense Log" else set()
    if base.builder_id:
        user_ids.add(base.builder_id)
    members = await asyncio.gather(*(guild.fetch_member(user_id) for user_id in user_ids))
    return {user_id: mention_user(member, user_id, mention_type="Builder" if user_id == base.builder_id else "Player")
            for user_id, member in zip(user_ids, members)}


def construct_base_embed(
        base: BaseLayout, embed_type: str = "Performance", *, names: dict[str, str],
) -> ipy.Embed:
    builder = base.builder_id
    quality = base.quality

    expiration_ts = ipy.Timestamp.fromtimestamp(base.expiration_date)
    expiration_ts = expiration_ts.format(style=ipy.TimestampStyles.RelativeTime)

    embed = ipy.Embed(
        title=f"**{base.district} Base**",
        description=f"{base.link}\n"
                    f"*Expires {expiration_ts}*",
        fields=[
            ipy.EmbedField(
                name="**Base Builder**",
                value=names[builder] if builder else "Unknown builder"
            ),
            ipy.EmbedField(
                name="**Base Quality**",
                value=f"{BASE_QUALITY_EMOJIS[quality]} {quality}"
            )
        ],
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )

    embed.set_image(base.image_url)

    if embed_type == "Performance":
        total_destruction = 0
        total_1stars = 0
        total_2stars = 0
        total_3stars = 0
        total_entries = 0

        # iterate over the 'records' list from the model
        for record in base.records:
            total_entries += 1
            total_destruction += record[2]
            if record[1] == 1:
                total_1stars += 1
            if record[1] == 2:
                total_2stars += 1
            if record[1] == 3:
                total_3stars += 1

        embed.add_field(
            name="**Performance**",
            value=f"⭐ Rate: `{total_1stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"⭐⭐ Rate: `{total_2stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"⭐⭐⭐ Rate: `{total_3stars * 100 / total_entries if total_entries else 0:.2f}%`\n"
                  f"Average Destruction: `{total_destruction / total_entries if total_entries else 0:.2f}%`\n"
                  f"Total Entries: `{total_entries}`"
        )

    if embed_type == "Defense Log":
        log = ""
        count = 0
        for record in reversed(base.records):
            if count >= 10:
                break

            log += f"{names[record[0]]} → {record[1]}⭐ {record[2]}%"
            # check for optional timestamp in record tuple
            if len(record) == 4:
                ts = ipy.Timestamp.fromtimestamp(record[3]).format(ipy.TimestampStyles.RelativeTime)
                log += f" {ts}"
            log += "\n"
            count += 1

        embed.add_field(
            name="**Defense Log**",
            value=log if log else "No defenses so far"
        )

    return embed


def _iter_message_components(components):
    rows = [components] if isinstance(components, ipy.ActionRow) else components or []
    for row in rows:
        yield from getattr(row, "components", [])


def _parse_base_list_state(components):
    for component in _iter_message_components(components):
        custom_id = getattr(component, "custom_id", None) or ""
        if custom_id.startswith("base_left|") or custom_id.startswith("base_right|"):
            _, district, index, embed_type, base_quality, builder_id = custom_id.split("|")
            return district, int(index), embed_type, base_quality, builder_id
    raise ValueError("missing base list pagination state")


def _build_base_list_components(
        district: str, index: int, embed_type: str, base_quality: str, builder_id: str, total: int,
):
    district_select = ipy.StringSelectMenu(
        ipy.StringSelectOption(label="Any", value="Any"),
        *(ipy.StringSelectOption(label=str(choice.name), value=str(choice.value)) for choice in DISTRICT_CHOICES),
        placeholder="📊 Select the district type",
        custom_id="base_list_district_select",
    )
    embed_type_select = ipy.StringSelectMenu(
        ipy.StringSelectOption(label="Performance", value="Performance"),
        ipy.StringSelectOption(label="Defense Log", value="Defense Log"),
        placeholder="📄 Select the embed type",
        custom_id="base_list_embed_type_select",
    )
    left_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="◀️️",
        custom_id=f"base_left|{district}|{index}|{embed_type}|{base_quality}|{builder_id}",
        disabled=index == 0,
    )
    page_number_button = build_page_button(
        index + 1,
        total,
        f"base_page|{district}|{embed_type}|{base_quality}|{builder_id}",
    )
    right_button = ipy.Button(
        style=ipy.ButtonStyle.PRIMARY,
        label="▶️",
        custom_id=f"base_right|{district}|{index}|{embed_type}|{base_quality}|{builder_id}",
        disabled=(total - 1 <= index),
    )
    return [
        ipy.ActionRow(district_select),
        ipy.ActionRow(embed_type_select),
        ipy.ActionRow(left_button, page_number_button, right_button),
    ]

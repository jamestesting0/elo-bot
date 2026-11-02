from collections.abc import Sequence
from datetime import UTC, datetime, time

import interactions as ipy

from common.constants import (
    COLOR,
    DISTRICT_EMOJI,
    FOOTER_IMAGE,
    FOOTER_TEXT,
)
from common.discord.pagination import build_page_button
from common.matches.season_stats import (
    ActivityPeak,
    SeasonStats,
    SeasonStatsError,
    SeasonWindow,
)


def _period_text(peak: ActivityPeak | None) -> str:
    if peak is None:
        return "No matches recorded."
    start = ipy.Timestamp.fromdatetime(datetime.combine(peak.start, time.min, tzinfo=UTC))
    text = f"`{peak.count:,}` {start.format(style=ipy.TimestampStyles.LongDate)}"
    if peak.ties > 1:
        text += f" ({peak.ties} periods tied; earliest shown)"
    return text


def stats_embed(window: SeasonWindow, stats: SeasonStats) -> ipy.Embed:
    start = "Not recorded" if window.start is None else ipy.Timestamp.fromtimestamp(window.start).format(
        style=ipy.TimestampStyles.LongDateTime,
    )
    end = ipy.Timestamp.fromtimestamp(window.end).format(style=ipy.TimestampStyles.LongDateTime)
    period = f"Start: {start}\nEnd: {'Ongoing' if window.current else end}"
    embed = ipy.Embed(
        title=f"**{window.label} Stats**",
        description=period,
        color=COLOR,
        footer=ipy.EmbedFooter(text=FOOTER_TEXT,
        icon_url=FOOTER_IMAGE)
    )
    embed.add_field(name="**Activity**", value=(
        f"Total Matches: `{stats.matches:,}`\n"
        f"Challenges: `{stats.challenges:,}`\n"
        f"Matchmaking: `{stats.matches - stats.challenges:,}`\n"
        f"Busiest Week: {_period_text(stats.week)}\n"
        f"Busiest Day: {_period_text(stats.day)}\n"
        f"Players: `{stats.active_players:,}`"
    ), inline=False)
    districts = "\n".join(f"{DISTRICT_EMOJI} {name} `{count:,}`" for name, count in stats.districts)
    if stats.missing_districts:
        districts += f"\n\nDistrict details were not recorded for `{stats.missing_districts:,}` matches."
    if len(districts) > 1024:
        raise SeasonStatsError("Stored district details exceed the display limit.")
    embed.add_field(name="**Districts**", value=districts, inline=False)
    return embed


def stats_controls(windows: Sequence[SeasonWindow], index: int) -> list[ipy.ActionRow]:
    key = windows[index].key
    return [ipy.ActionRow(
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", custom_id=f"season_stats_left|{key}",
                   disabled=index == 0),
        build_page_button(index + 1, len(windows), f"season_stats_page|{key}"),
        ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", custom_id=f"season_stats_right|{key}",
                   disabled=index == len(windows) - 1),
    )]

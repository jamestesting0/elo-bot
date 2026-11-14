from collections.abc import Sequence
from datetime import UTC, datetime, time

import interactions as ipy

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.emojis import DISTRICT_EMOJI
from elo_bot.discord.pagination import build_page_button
from elo_bot.features.seasons.stats import (
    ActivityPeak,
    SeasonStats,
    SeasonStatsError,
    SeasonWindow,
)


def _period_text(peak: ActivityPeak) -> str:
    start = ipy.Timestamp.fromdatetime(datetime.combine(peak.start, time.min, tzinfo=UTC))
    text = f"`{peak.count:,}` {start.format(style=ipy.TimestampStyles.LongDate)}"
    if peak.ties > 1:
        text += f" ({peak.ties} periods tied; earliest shown)"
    return text


def _duration_text(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds + 0.5), 60)
    hours, minutes = divmod(minutes, 60)
    return " ".join(f"{value}{unit}" for value, unit in ((hours, "h"), (minutes, "m"), (seconds, "s"))
                    if value) or "0s"


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
    activity = (
        f"Total Matches: `{stats.matches:,}`\n"
        f"Challenges: `{stats.challenges:,}`\n"
        f"Matchmaking: `{stats.matches - stats.challenges:,}`\n"
    )
    if stats.week:
        activity += f"Busiest Week: {_period_text(stats.week)}\n"
    if stats.day:
        activity += f"Busiest Day: {_period_text(stats.day)}\n"
    activity += f"Players: `{stats.active_players:,}`"
    embed.add_field(name="**Activity**", value=activity, inline=False)
    if stats.confirmation_samples:
        overall = "N/A" if stats.confirmation_average is None else _duration_text(stats.confirmation_average)
        wait = f"Overall: `{overall}` ({stats.confirmation_samples:,} of {stats.matches * 2:,} participants)"
        if stats.non_instant_samples:
            non_instant = _duration_text(stats.non_instant_average)
            wait += f"\nNon-instant: `{non_instant}` (samples: {stats.non_instant_samples:,}; ready excluded)"
        embed.add_field(name="**Confirmation Wait**", value=wait, inline=False)
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

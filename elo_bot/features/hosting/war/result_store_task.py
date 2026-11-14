from elo_bot.core.finals import is_final_match
import functools
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

import interactions as ipy

from elo_bot.features.hosting.war import result_store
from elo_bot.features.hosting.war import result_store_ui
from elo_bot.core.match_results import is_match_finished
from elo_bot.db import Match


SCHEDULED_RESULT_STORE_DELAY: Final = timedelta(minutes=40)


@dataclass(frozen=True, slots=True)
class ScheduledResultStoreRequest:
    token: str
    player: int
    requested_by: str
    started_at: int
    submitter_id: str | None = None
    is_final: bool = False


def _scheduled_result_store_is_eligible(match: Match, request: ScheduledResultStoreRequest) -> bool:
    player = request.player
    return (
        player in {0, 1}
        and len(match.participants) > player
        and match.is_war
        and not is_final_match(match)
        and match.times[player] == request.started_at
        and bool(match.host_clans[player])
        and bool(match.thread)
        and len(match.final_districts) == 3
        and all(isinstance(district, str) and district for district in match.final_districts)
        and not is_match_finished(match, player=player)
    )


async def run_scheduled_result_store(bot: ipy.Client, request: ScheduledResultStoreRequest) -> None:
    match = await Match.find_one(Match.token == request.token)
    if match is None or not _scheduled_result_store_is_eligible(match, request):
        return

    channel = await bot.fetch_channel(int(match.thread))
    if "🔴" in channel.name:
        return
    match_commands = bot.get_ext("MatchCommands")
    if match_commands is None:
        return

    ctx = result_store_ui.ScheduledResultStoreContext(
        author=ipy.SnowflakeObject(int(request.requested_by)),
        channel=channel,
    )
    await result_store.store_match_result_automatically(
        match_commands,
        ctx,
        result_store.AutomaticResultStoreRequest(
            match=match,
            player=request.player,
            interactive=False,
            allow_cancellation=False,
            expected_started_at=request.started_at,
            scheduled_delay_minutes=int(SCHEDULED_RESULT_STORE_DELAY.total_seconds() // 60),
            submitter_id=request.submitter_id,
        ),
    )


def schedule_automatic_result_store(bot: ipy.Client, request: ScheduledResultStoreRequest) -> None:
    if request.is_final:
        return
    task = ipy.Task(
        functools.partial(run_scheduled_result_store, bot, request),
        ipy.DateTrigger(datetime.now() + SCHEDULED_RESULT_STORE_DELAY),
    )
    task.start()

from collections.abc import Sequence
from datetime import timedelta

from elo_bot.db.player_models import RecordedMatch


def weekly_challenge_balance(player_matches: Sequence[RecordedMatch], now: float) -> int:
    """Replay the trailing seven days without carrying challenge debt forward."""
    week_start = now - timedelta(days=7).total_seconds()
    recent_matches = [match for match in player_matches if week_start <= match.time <= now]
    balance = 0
    for match in sorted(recent_matches, key=lambda match: (match.time, int(match.id))):
        balance = max(0, balance - 1) if match.is_challenge else balance + 1
    return balance

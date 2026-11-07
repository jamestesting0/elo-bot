from common.finals.types import is_final_match
from database_models import Match


def hosting_started(match: Match, player: int | None = None) -> bool:
    """Distinguish committed hosting from schedules and accepted requests."""
    sides = (0, 1) if player is None else (player,)
    if is_final_match(match):
        for side in sides:
            if match.times[side] is None:
                continue
            if match.final_hosting_times[side] is not None:
                return True
            if match.scheduled_times[side] is not None:
                if (match.times[side] != match.scheduled_times[side]
                        or ("final_hosting_times" not in match.model_fields_set
                            or match.final_legacy_hosting_evidence[side])
                        and (match.hosts[side] or match.host_clans[side])):
                    return True
            elif (match.hosts[side] or match.host_clans[side] or match.final_started_times[side] is not None
                  or "scheduled_times" not in match.model_fields_set
                  and (match.final_started_at is not None or match.final_start_claim is not None)):
                return True
        return False
    if player is None:
        return bool(any(match.times or []) or any(match.hosts or []))
    return bool(match.hosts[player] or match.times[player])

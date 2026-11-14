from collections.abc import Mapping, Sequence

from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from common.results.operations import result_input_error
from common.results.validation import is_match_finished
from elobot.db import Match


def raid_storage_skip_reason(match: Match, player: int) -> str | None:
    if not match.thread or len(match.participants) != 2:
        return "no match thread"
    if not match.is_war:
        return "not a war match"
    if match.times[player] or match.hosts[player]:
        return "already started"
    if is_match_finished(match) or result_input_error(match) or any(match.participants[player].result):
        return "finished or results in progress"
    participant, opponent = match.participants[player], match.participants[1 - player]
    if participant.base_store is not None or participant.opponent_bases_stored:
        return "already stored"
    if opponent.submit_bases is None:
        return "opponent has not chosen whether to submit bases"
    if opponent.submit_bases and not all(opponent.bases):
        return "opponent bases incomplete"
    return None


def eligible_for_raid_storage(match: Match, player: int) -> bool:
    return raid_storage_skip_reason(match, player) is None


def select_raid_storage_slot(
        match: Match,
        player: int,
        slot_map: Mapping[str, Sequence[str]],
) -> tuple[str, str] | None:
    """Leave free capacity in two configured clans, with opponents stored separately."""
    available = {str(choice.value): slot_map.get(str(choice.value), ()) for choice in HOST_CLAN_CHOICES}
    opponent_store = match.participants[1 - player].base_store
    for slot in BASE_STORE_SLOTS:
        for clan_tag, slots in available.items():
            if slot not in slots or (opponent_store and opponent_store.clan_tag == clan_tag):
                continue
            remaining_clans = sum(
                bool(free_slots) and (tag != clan_tag or len(free_slots) > 1)
                for tag, free_slots in available.items()
            )
            if remaining_clans >= 2:
                return clan_tag, slot
    return None

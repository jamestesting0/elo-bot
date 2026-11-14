from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Optional

from elobot.db import Match, Player, Queue
from elobot.db.player_models import QueueDistrictSelection
from elobot.features.matches.occupancy import PlayerOccupied
from elobot.features.matches.queued_districts import find_attempt_queue
from elobot.features.queue.policy import AUTOQUEUE_FOREVER_EXPIRY, QUEUE_TIER1_AGED_AFTER_SECONDS, QUEUE_TIERS


class QueueOutcomeStatus(StrEnum):
    BLACKLISTED = "blacklisted"
    DISABLED = "disabled"
    UNREGISTERED = "unregistered"
    ALREADY_QUEUED = "already_queued"
    OCCUPIED = "occupied"
    MATCHED = "matched"
    QUEUED = "queued"
    BETA_QUEUED = "beta_queued"


@dataclass(frozen=True, slots=True)
class QueueOutcome:
    status: QueueOutcomeStatus
    occupied: PlayerOccupied | None = None
    selection: QueueDistrictSelection | None = None


def autoqueue_expiry(duration: str, now: int | None = None) -> str | None:
    current_epoch = int(datetime.now().timestamp()) if now is None else now
    match duration:
        case "Forever":
            return str(AUTOQUEUE_FOREVER_EXPIRY)
        case "7 Days":
            return str(current_epoch + 7 * 86400)
        case "3 Days":
            return str(current_epoch + 3 * 86400)
        case "1 Day":
            return str(current_epoch + 86400)
        case _:
            return None


def is_autoqueue_active(expiry: str, now: int | None = None) -> bool:
    if not expiry.isascii() or not expiry.isdecimal():
        return False

    current_epoch = int(datetime.now().timestamp()) if now is None else now
    return int(expiry) > current_epoch


async def get_last_opponent(player: Player) -> Optional[str]:
    if not player.matches:
        return None

    await player.fetch_link(Player.matches
                            )
    sorted_matches = sorted(player.matches, key=lambda m: int(m.id), reverse=True)
    last_match = sorted_matches[0]
            
    return last_match.players[player.user_id].opponent


async def get_match_queue(tier: int) -> Queue:
    queue = await Queue.find_one(Queue.tier == tier, fetch_links=True)
    if queue:
        return queue

    queue = Queue(tier=tier)
    await queue.insert()
    return queue


def get_queue_entered_at(queue: Queue) -> dict[str, float]:
    entered_at = getattr(queue, "entered_at", None)
    if not isinstance(entered_at, dict):
        entered_at = {}
        queue.entered_at = entered_at

    return entered_at


def normalize_queue_timestamps(queue: Queue, now: float | None = None) -> bool:
    now = now or datetime.now().timestamp()
    changed = False
    entered_at = get_queue_entered_at(queue)
    active_player_ids = set()

    for player in getattr(queue, "players", []) or []:
        player_id = str(player.user_id)
        active_player_ids.add(player_id)
        if player_id not in entered_at:
            entered_at[player_id] = now
            changed = True

    for player_id in list(entered_at):
        if player_id not in active_player_ids:
            entered_at.pop(player_id, None)
            changed = True

    for player_id in list(queue.district_selections):
        if player_id not in active_player_ids:
            queue.district_selections.pop(player_id)
            changed = True

    return changed


def add_player_to_queue(queue: Queue, player: Player, entered_at: float | None = None):
    player_id = str(player.user_id)
    if player_id not in [str(p.user_id) for p in getattr(queue, "players", []) or []]:
        queue.players.append(player)

    get_queue_entered_at(queue).setdefault(player_id, entered_at or datetime.now().timestamp())


def remove_player_from_queue(queue: Queue, player: Player | str) -> bool:
    player_id = str(player.user_id if hasattr(player, "user_id") else player)
    removed = False

    for queued_player in list(getattr(queue, "players", []) or []):
        if str(queued_player.user_id) == player_id:
            queue.players.remove(queued_player)
            removed = True

    get_queue_entered_at(queue).pop(player_id, None)
    queue.district_selections.pop(player_id, None)
    return removed


def move_player_between_queues(source_queue: Queue, target_queue: Queue, player: Player, now: float):
    player_id = str(player.user_id)
    selection = source_queue.district_selections.get(player_id)
    if remove_player_from_queue(source_queue, player_id):
        add_player_to_queue(target_queue, player, now)
        if selection is not None:
            target_queue.district_selections.setdefault(player_id, selection)


async def remove_matched_queue_attempts(match: Match) -> None:
    for participant in match.participants:
        if participant.queue_attempt_id is None:
            continue
        for tier in QUEUE_TIERS:
            queue = await find_attempt_queue(participant.user_id, participant.queue_attempt_id, [tier])
            if queue is not None:
                remove_player_from_queue(queue, participant.user_id)
                await queue.save_changes()


async def get_closest_elo_player(player: Player) -> Optional[Player]:
    candidates = [
        candidate
        for candidate in await Player.find_all().to_list()
        if str(candidate.user_id) != str(player.user_id)
    ]

    if not candidates:
        return None

    return min(candidates, key=lambda candidate: abs(candidate.elo - player.elo))


async def promote_tier1_aged_players(tier0_queue: Queue, tier1_queue: Queue, now: float):
    cutoff = now - QUEUE_TIER1_AGED_AFTER_SECONDS
    tier0_changed = normalize_queue_timestamps(tier0_queue, now)
    tier1_changed = normalize_queue_timestamps(tier1_queue, now)
    aged_players = [
        player
        for player in list(getattr(tier1_queue, "players", []) or [])
        if get_queue_entered_at(tier1_queue).get(str(player.user_id), now) <= cutoff
    ]

    for player in aged_players:
        move_player_between_queues(tier1_queue, tier0_queue, player, now)
        tier1_changed = True
        tier0_changed = True

    if tier0_changed:
        await tier0_queue.save_changes()

    if tier1_changed:
        await tier1_queue.save_changes()


async def move_queue_tier(source_tier: int, target_tier: int, now: float):
    source_queue = await get_match_queue(source_tier)
    target_queue = await get_match_queue(target_tier)

    if normalize_queue_timestamps(source_queue, now):
        await source_queue.save_changes()
    if normalize_queue_timestamps(target_queue, now):
        await target_queue.save_changes()

    if not source_queue.players:
        return

    for player in list(source_queue.players):
        move_player_between_queues(source_queue, target_queue, player, now)

    await target_queue.save_changes()
    await source_queue.save_changes()


async def reconcile_queue_memberships(queues: list[Queue]) -> None:
    seen: set[str] = set()
    for queue in sorted(queues, key=lambda item: item.tier):
        player_ids = {str(player.user_id) for player in queue.players}
        changed = False
        for player_id in player_ids & seen:
            changed = remove_player_from_queue(queue, player_id) or changed
        if changed:
            await queue.save_changes()
        seen.update(player_ids)

from common.finals.types import FinalError, is_final_match
from common.finals.matches import schedule_final_match
from common.matches.timing import hosting_started
import asyncio  # noqa: ANYIO_OK - interactions.py runtime and the requested registry use asyncio locks
import anyio
import copy
import logging
import math
import random
import secrets
import sys
import traceback
import functools
from collections.abc import Awaitable, Callable
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal, Optional, assert_never

from beanie.odm.operators.find.comparison import In, NotIn
from beanie.odm.operators.update.array import Push
from beanie.odm.operators.update.general import Inc
from pymongo.errors import PyMongoError
from interactions import utils
import cogs.base_store.presentation as base_store_presentation
import cogs.base_store.publication as base_store_publication
import cogs.base_store.storage as base_store_storage
from cogs.base_store.raid_task import store_before_raid_weekend
from cogs.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from common.base_store.live import live_stores, ready_live_reservation_id
from common.base_store.messages import BaseStoreReleaseReason
from common.base_store.start import StoredStartState, held_store, shared_store_clan
import cogs.live_hosting.helper_live as helper_live
import cogs.live_hosting.live_start as live_start
from cogs.base_store.models import (
    BaseStorePreparationFailure,
    BaseStorePreparationOutcome,
    BaseStorePreparationRequest,
    BaseStorePresentationRequest,
    PreparedBaseStore,
    SelfBaseStoreRequest,
    StoredBaseActionRequest,
    StoredBaseControlOutcome,
    StoredBaseControlState,
    stored_base_matches,
    bind_legacy_store_message,
)
from cogs.leaderboard_commands import get_wins, get_avg_destruction, get_opponent_avg_stars, \
    get_opponent_avg_destruction, get_avg_stars
from cogs.player_commands import fetch_user_match_id_choices, forget_replaced_match_choice
import cogs.war_hosting.helper_host as helper_host
import cogs.base_store.live_service as live_base_service
import cogs.live_hosting.result_store as live_result_store
import cogs.war_hosting.result_store as result_store
import cogs.war_hosting.result_store_ui as result_store_ui
import cogs.war_hosting.war_start as war_start
from cogs.war_hosting.confirmation import PendingBaseConfirmation, confirm_pending_opponent_bases
from cogs.war_hosting.district_selection import match_district_index, started_host_clan_tag
from cogs.war_hosting.helper_api import HelperScoreAttackLimit
from common.results.autowin import apply_autowin_player, claim_autowin, mark_autowin
from common.discord.channel_edits import schedule_channel_name_edit
from common.discord.message_edits import edit_channel_message
from common.results.helper_credits import award_helper_once
from common.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from common.constants import *
from common.discord.decorators import create_async_task
from common.results.validation import is_match_finished, is_valid_complete_match_result, is_valid_match_result_entry
from common.matches.base_selection import district_base_quality, ensure_secondary_base_index
from common.matches.controls import check_match_control, reject_outdated_match_control, update_match_control
from common.matches.queued_districts import (
    find_attempt_match, find_attempt_queue, queue_district_components, start_district_selection,
)
from database_models.player_models import QueueDistrictSelection
from common.matches.message_lock import coordinated_match_message, match_message_lock
from common.matches.recovery import repair_match_messages
from common.matches.messages import (
    MatchMessageState, mark_match_message_ended, match_message_embeds, publish_match_message,
)
from common.matches.thread_messages import (
    ThreadMessageBindingError, attach_thread_message, find_match_thread_announcement,
)
from common.discord.embeds import embed_edit_payload
from common.matches.cleanup import delete_unreserved_match, has_pending_match_operation, has_reserved_match_slots
from common.results.manual import (
    ManualResultConfirmationUnknown, complete_manual_result, manual_result_key, persist_manual_result,
)
from common.results.records import add_recorded_match, remove_base_record
from common.results.operations import (
    ResultOperationConflict, begin_result_operation, complete_result_operation, completed_result_changes, persist_result_record,
    publish_result_operation, result_input_error, rollback_result_operation, bind_result_message,
)
from common.players.data_boundary import PLAYER_DATA_BOUNDARY, PlayerDataReset
from common.players.self_hosting import invite_eligible_players
from database_models import Match, Participant, BaseLink, Clan, HelperTask, BaseLayout, \
    RecordedMatch, Player, Queue, MatchPlayerStats, MatchDistrictStats, Helper, HelperHistory, OverrideCode, ServerState
from common.utils import *


QUEUE_TIER_AGED = 0
QUEUE_TIER_MIXED = 1
QUEUE_TIER_PRIORITY = 2
QUEUE_TIER_NORMAL = 3
type ResultDetectionType = Literal["Strict", "Relaxed"]
QUEUE_TIERS = [
    QUEUE_TIER_AGED,
    QUEUE_TIER_MIXED,
    QUEUE_TIER_PRIORITY,
    QUEUE_TIER_NORMAL,
]
QUEUE_MATCH_RANGES = {
    QUEUE_TIER_AGED: 250,
    QUEUE_TIER_MIXED: 200,
    QUEUE_TIER_PRIORITY: 150,
    QUEUE_TIER_NORMAL: 100,
}
QUEUE_TIER_ADVANCEMENT = [
    (QUEUE_TIER_PRIORITY, QUEUE_TIER_MIXED),
    (QUEUE_TIER_NORMAL, QUEUE_TIER_PRIORITY),
]
QUEUE_TIER1_AGED_AFTER_SECONDS = 24 * 60 * 60
AUTOQUEUE_FOREVER_EXPIRY = 2 ** 63 - 1
QUEUE_OPERATION_LOCK = asyncio.Lock()
RESOURCE_CLAN_LOCKS: dict[str, asyncio.Lock] = {}
_RESOURCE_CLAN_RESERVATIONS: ContextVar[dict[str, "ResourceClanReservation"]] = ContextVar(
    "resource_clan_reservations",
    default={},
)
RESOURCE_MATCH_SIDE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}
_RESOURCE_MATCH_SIDE_RESERVATIONS: ContextVar[dict[tuple[str, int], "ResourceMatchSideReservation"]] = ContextVar(
    "resource_match_side_reservations",
    default={},
)


def get_district_options(
        wizard_valley_available: bool,
        capital_peak_available: bool,
) -> list[list[str]]:
    district_options = [
        ["Barbarian Camp", "Skeleton Park"],
        ["Balloon Lagoon", "Builder's Workshop", "Golem Quarry"],
        ["Dragon Cliffs", "Goblin Mines"],
    ]
    if wizard_valley_available:
        district_options[0].append("Wizard Valley")
    if capital_peak_available:
        district_options[2].append("Capital Peak")
    return district_options


@dataclass(frozen=True, slots=True)
class PlayerOccupied:
    message: str
    component: ipy.Button | None = None


@dataclass(frozen=True, slots=True)
class LivePreflight:
    clan: str
    scout: bool
    automatic: bool
    host: ipy.BaseUser | None = None
    requested_by: str | None = None
    defer_response: bool = True
    public_progress: bool = False
    district: str | None = None
    allow_opponent_cancel: bool = False
    stored_reservation_id: str | None = None


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


def collect_occupied_base_store_slots(matches: list[Match]) -> dict[str, set[str]]:
    return base_store_storage.collect_occupied_base_store_slots(matches)


def get_base_store_timestamp(participant: Participant) -> int | None:
    return base_store_storage.get_base_store_timestamp(participant)


async def clear_expired_base_store_reservations(matches: list[Match], *, bot: ipy.Client | None = None) -> None:
    await base_store_storage.clear_expired_base_store_reservations(matches, bot=bot)


async def fetch_reconciled_base_store_matches(*, bot: ipy.Client | None = None) -> list[Match]:
    return await base_store_storage.fetch_reconciled_base_store_matches(bot=bot)


def has_unmapped_base_store_reservations(matches: list[Match]) -> bool:
    return base_store_storage.has_unmapped_base_store_reservations(matches)


async def sync_base_store_slots(clan_tag: str | None = None, *, bot: ipy.Client | None = None) -> dict[str, list[str]]:
    return await base_store_storage.sync_base_store_slots(clan_tag, bot=bot)


async def get_start_base_store_slot_availability(
        clan_tag: str,
        current_thread: str,
        *, bot: ipy.Client | None = None,
) -> tuple[list[str], dict[str, tuple[str, str]]]:
    return await base_store_storage.get_start_base_store_slot_availability(clan_tag, current_thread, bot=bot)


def construct_start_slot_instruction(available_slots: list[str], clan_name: str) -> str:
    return base_store_presentation.construct_start_slot_instruction(available_slots, clan_name)


def construct_occupied_slots_error(
        clan_tag: str,
        clan_name: str,
        slot_owners: dict[str, tuple[str, str]],
) -> str:
    return base_store_presentation.construct_occupied_slots_error(clan_tag, clan_name, slot_owners)


async def clear_participant_base_store(
        match: Match, index: int, reset_stored_flag: bool = False, *, bot: ipy.Client | None = None,
        reason: BaseStoreReleaseReason,
) -> bool:
    return await base_store_storage.clear_participant_base_store(
        match,
        index,
        reset_stored_flag=reset_stored_flag,
        bot=bot,
        reason=reason,
    )


async def clear_match_base_stores(
        match: Match, reset_stored_flags: bool = False, *, bot: ipy.Client | None = None,
        reason: BaseStoreReleaseReason,
) -> None:
    await base_store_storage.clear_match_base_stores(
        match, reset_stored_flags=reset_stored_flags, bot=bot, reason=reason,
    )


async def is_clan_available(clan_tag: str) -> bool:
    if not clan_tag:
        return True

    clan = await Clan.find_one(Clan.tag == clan_tag)
    if not clan:
        return True

    return clan.availability_str == "Available"


class ResourceClanReservation:
    def __init__(self, clan_tag: str, lock: asyncio.Lock):
        self.clan_tag = clan_tag
        self.lock = lock
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        reservations = dict(_RESOURCE_CLAN_RESERVATIONS.get())
        if reservations.get(self.clan_tag) is self:
            del reservations[self.clan_tag]
            _RESOURCE_CLAN_RESERVATIONS.set(reservations)
        self.lock.release()


class ResourceMatchSideReservation:
    def __init__(self, match_token: str, player_index: int, lock: asyncio.Lock):
        self.key = (match_token, player_index)
        self.lock = lock
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        reservations = dict(_RESOURCE_MATCH_SIDE_RESERVATIONS.get())
        if reservations.get(self.key) is self:
            del reservations[self.key]
            _RESOURCE_MATCH_SIDE_RESERVATIONS.set(reservations)
        self.lock.release()


async def try_reserve_resource_clan(clan_tag: str) -> ResourceClanReservation | None:
    lock = RESOURCE_CLAN_LOCKS.setdefault(clan_tag, asyncio.Lock())
    if lock.locked():
        return None
    await lock.acquire()
    reservation = ResourceClanReservation(clan_tag, lock)
    reservations = dict(_RESOURCE_CLAN_RESERVATIONS.get())
    reservations[clan_tag] = reservation
    _RESOURCE_CLAN_RESERVATIONS.set(reservations)
    return reservation


def current_resource_clan_reservation(clan_tag: str) -> ResourceClanReservation | None:
    return _RESOURCE_CLAN_RESERVATIONS.get().get(clan_tag)


async def try_reserve_resource_match_side(
        match_token: str,
        player_index: int,
) -> ResourceMatchSideReservation | None:
    key = (match_token, player_index)
    lock = RESOURCE_MATCH_SIDE_LOCKS.setdefault(key, asyncio.Lock())
    if lock.locked():
        return None
    await lock.acquire()
    reservation = ResourceMatchSideReservation(match_token, player_index, lock)
    reservations = dict(_RESOURCE_MATCH_SIDE_RESERVATIONS.get())
    reservations[key] = reservation
    _RESOURCE_MATCH_SIDE_RESERVATIONS.set(reservations)
    return reservation


def current_resource_match_side_reservation(
        match_token: str,
        player_index: int,
) -> ResourceMatchSideReservation | None:
    return _RESOURCE_MATCH_SIDE_RESERVATIONS.get().get((match_token, player_index))


def is_resource_clan_locked(clan_tag: str) -> bool:
    lock = RESOURCE_CLAN_LOCKS.get(clan_tag)
    return bool(lock and lock.locked())


def format_available_resource_clans(clans: list[Clan]) -> str:
    available_clans = [
        clan for clan in clans
        if clan.availability_str == "Available"
        and not RESOURCE_CLAN_LOCKS.setdefault(clan.tag, asyncio.Lock()).locked()
    ]
    available = "\n".join(f"- {clan.name} ({clan.tag})" for clan in available_clans) or "- None"
    return f"Available clans:\n{available}"


async def available_resource_clans_text() -> str:
    return format_available_resource_clans(await Clan.find_all().to_list())


def _resource_clan_owner(availability_str: str) -> str | None:
    owner = re.search(r"<#(\d+)>", availability_str)
    return owner.group(1) if owner else None


async def claim_resource_clan(
        reservation: ResourceClanReservation,
        match: Match,
        player1_index: int,
        *,
        player2_index: int | None = None,
        side_reservation: ResourceMatchSideReservation | None = None,
        updates: dict | None = None,
        allow_current_owner: bool = False,
        expected_control: MatchMessageState | None = None,
) -> bool:
    match_token = str(match.token)
    expected_old_clan = (match.host_clans or [None, None])[player1_index]
    side_reservation = side_reservation or current_resource_match_side_reservation(match_token, player1_index)
    if not side_reservation:
        side_reservation = await try_reserve_resource_match_side(match_token, player1_index)
    if not side_reservation:
        reservation.release()
        return False
    try:
        fresh_match = await Match.find_one({"token": match.token})
        if not fresh_match:
            return False
        old_clan = (fresh_match.host_clans or [None, None])[player1_index]
        if old_clan != expected_old_clan:
            return False
        clan = await Clan.find_one({"tag": reservation.clan_tag})
        if not clan:
            return False
        same_owner = (
            old_clan == reservation.clan_tag
            and _resource_clan_owner(clan.availability_str) == str(match.thread)
        )
        if clan.availability_str != "Available" and not (allow_current_owner and same_owner):
            return False
        if "persistent_host_clans" not in fresh_match.model_fields_set:
            await fresh_match.ensure_persistent_host_clans()
        match_updates = dict(updates or {})
        if is_final_match(fresh_match) and f"times.{player1_index}" in match_updates:
            await fresh_match.ensure_final_hosting_times()
            for side in (player1_index,) if player2_index is None else (player1_index, player2_index):
                match_updates[f"final_hosting_times.{side}"] = match_updates[f"times.{player1_index}"]
        match_updates[f"host_clans.{player1_index}"] = reservation.clan_tag
        match_updates[f"persistent_host_clans.{player1_index}"] = reservation.clan_tag
        previous_values = {}
        for field in match_updates:
            path = field.split(".")
            value = getattr(fresh_match, path[0])
            previous_values[field] = copy.deepcopy(value if len(path) == 1 else value[int(path[1])])
        if expected_control is None:
            await fresh_match.set(match_updates)
        else:
            async with BASE_STORE_RESERVATION_LOCK if isinstance(expected_control, StoredStartState) else nullcontext():
                if not await update_match_control(fresh_match, expected_control, match_updates):
                    return False
        targets = [fresh_match] if fresh_match is match else [fresh_match, match]
        occupancy_complete = False
        try:
            for target in targets:
                for field, value in match_updates.items():
                    path = field.split(".")
                    if len(path) == 1:
                        setattr(target, field, value)
                    else:
                        getattr(target, path[0])[int(path[1])] = value
            if not player2_index:
                await occupy_clan(fresh_match, player1_index)
            else:
                await occupy_clan(fresh_match, player1_index, player2_index)
            occupancy_complete = True
        finally:
            if not occupancy_complete:
                if expected_control is None:
                    await fresh_match.set(previous_values)
                else:
                    rollback_filter = dict(expected_control.binding_filter())
                    rollback_filter.update(match_updates)
                    await fresh_match.get_pymongo_collection().update_one(
                        rollback_filter, {"$set": previous_values},
                    )
                for target in targets:
                    for field, value in previous_values.items():
                        path = field.split(".")
                        if len(path) == 1:
                            setattr(target, field, value)
                        else:
                            getattr(target, path[0])[int(path[1])] = value
    finally:
        reservation.release()
        side_reservation.release()
    if old_clan != reservation.clan_tag:
        await free_clan(old_clan, expected_thread=str(fresh_match.thread))
    return True


async def occupy_clan(match: Match, player1_index: int, player2_index: int | None = None):
    if not match.thread or not match.participants or len(match.participants) < 2:
        return

    p1_id = match.participants[player1_index].user_id

    if player2_index is None:
        # war-style: use host_clans[player1_index]
        clan_tag = (match.host_clans or [None, None])[player1_index]
        players_str = f"<@{p1_id}>"
    else:
        # non-war: fall back to first host_clan
        clan_tag = (match.host_clans or [None, None])[0]
        p2_id = match.participants[player2_index].user_id
        players_str = f"<@{p1_id}> vs <@{p2_id}>"

    if not clan_tag:
        return

    now = int(datetime.now().timestamp())
    discord_timestamp = ipy.Timestamp.fromtimestamp(now).format(ipy.TimestampStyles.RelativeTime)
    availability_str = f"<#{match.thread}> {players_str} {discord_timestamp}"

    clan = await Clan.find_one(Clan.tag == clan_tag)
    if clan:
        await clan.update({"$set": {"availability_str": availability_str}})


async def _release_resource_clan_locked(
        clan_tag: str,
        expected_thread: str | None,
        expected_availability: str | None,
) -> bool:
    clan = await Clan.find_one({"tag": clan_tag})
    if not clan:
        return False
    if expected_thread and _resource_clan_owner(clan.availability_str) != str(expected_thread):
        return False
    if expected_availability and clan.availability_str != expected_availability:
        return False
    await clan.update({"$set": {"availability_str": "Available"}})
    return True


async def free_clan(
        clan_tag: str,
        *,
        expected_thread: str | None = None,
        expected_availability: str | None = None,
) -> bool:
    if not clan_tag:
        return False

    reservation = current_resource_clan_reservation(clan_tag)
    if reservation:
        try:
            return await _release_resource_clan_locked(clan_tag, expected_thread, expected_availability)
        finally:
            reservation.release()
    lock = RESOURCE_CLAN_LOCKS.setdefault(clan_tag, asyncio.Lock())
    async with lock:
        return await _release_resource_clan_locked(clan_tag, expected_thread, expected_availability)


async def free_match_clans(match: Match) -> None:
    clan_tags = match.host_clans if match.is_war else match.host_clans[:1]
    for clan_tag in dict.fromkeys(clan_tags):
        if clan_tag:
            await free_clan(clan_tag, expected_thread=str(match.thread))


@create_async_task
async def append_helper_task(thread: str, player_id: str, timestamp: int = None):
    if not timestamp:
        timestamp = int(datetime.now().timestamp())

    helper_task = await HelperTask.find_one({"thread": thread, "player_id": player_id})
    if helper_task:
        helper_task.timestamp = timestamp
        await helper_task.save_changes()
    else:
        helper_task = HelperTask(
            thread=thread,
            player_id=player_id,
            timestamp=timestamp
        )
        await helper_task.insert()


@create_async_task
async def remove_helper_task(thread: str, player_id: str):
    helper_task = await HelperTask.find_one({"thread": thread, "player_id": player_id})
    if helper_task:
        await helper_task.delete()


def is_missing_scout_view(match: Match, player_id: str | int):
    player_id = str(player_id)
    participant = next((p for p in match.participants if p.user_id == player_id), None)
    if not participant:
        return None, None

    for i, base in enumerate(participant.bases):
        if not base:
            continue

        if not base.image or base.image == UNAVAILABLE_IMAGE:
            # Get district name and return the missing base link
            district = match.final_districts[i] if i < len(match.final_districts) else None
            if not district and base.link:
                # fallback if final_districts not filled properly
                district = DISTRICT_CODES_REVERSED.get(base.link.split('%')[2], "Unknown")

            return district, base.link

    return None, None


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


async def find_opponent(player: Player, last_opponent: Optional[str], candidates: list[Player], 
                        max_elo_diff: int, tier: int, queue: Queue | None = None) -> Optional[Player]:
    for candidate in candidates:
        candidate_last_opponent_id = await get_last_opponent(candidate)
        if player.user_id == candidate.user_id:
            continue

        if candidate_last_opponent_id and candidate_last_opponent_id == player.user_id and tier != 1:
            continue

        if last_opponent and last_opponent == candidate.user_id and tier != 1:
            continue

        if abs(player.elo - candidate.elo) > max_elo_diff:
            continue

        if queue is not None and await is_player_occupied(str(candidate.user_id)):
            remove_player_from_queue(queue, candidate)
            await queue.save_changes()
            continue

        return candidate
    
    return None


async def find_queue_opponent(player: Player, last_opponent: Optional[str], queue: Queue,
                              max_elo_diff: int) -> Optional[Player]:
    candidates = sorted(getattr(queue, "players", []) or [], key=lambda x: abs(x.elo - player.elo))
    return await find_opponent(player, last_opponent, candidates, max_elo_diff,
                               getattr(queue, "tier", QUEUE_TIER_NORMAL), queue)


async def match_with_best_queue(bot: ipy.Client, player: Player, last_opponent: Optional[str],
                                queue_ranges: list[tuple[Queue, int]]) -> bool:
    matches: list[tuple[int, Queue, Player]] = []

    for queue, max_elo_diff in queue_ranges:
        opponent = await find_queue_opponent(player, last_opponent, queue, max_elo_diff)
        if opponent:
            matches.append((abs(opponent.elo - player.elo), queue, opponent))

    if not matches:
        return False

    _, queue, opponent = min(matches, key=lambda match: match[0])

    async def remove_matched_opponent() -> None:
        remove_player_from_queue(queue, opponent)
        await queue.save_changes()

    selection = queue.district_selections.get(str(opponent.user_id))
    if selection is None:
        await initiate_match(bot, player, opponent, on_inserted=remove_matched_opponent)
    else:
        await initiate_match(bot, player, opponent, on_inserted=remove_matched_opponent,
                             queue_selections=(None, selection))
    return True


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


async def run_queue_matching_trigger(bot: ipy.Client):
    now = datetime.now().timestamp()
    queues = {tier: await get_match_queue(tier) for tier in QUEUE_TIERS}
    await reconcile_queue_memberships(list(queues.values()))

    await promote_tier1_aged_players(queues[QUEUE_TIER_AGED], queues[QUEUE_TIER_MIXED], now)

    for tier in QUEUE_TIERS:
        await match_players(bot, tier, QUEUE_MATCH_RANGES[tier], queue=queues[tier])

    for source_tier, target_tier in QUEUE_TIER_ADVANCEMENT:
        await move_queue_tier(source_tier, target_tier, now)


async def match_players(bot: ipy.Client, tier: int, max_elo_diff: int, *, queue: Queue | None = None):
    if queue is None:
        queue = await get_match_queue(tier)
    normalize_queue_timestamps(queue)
    
    # sort the queue so that players with similar elo get matched
    iterations = math.floor(len(queue.players) / 2)
    
    for _ in range(iterations):
        available_players = list(queue.players)
        if len(available_players) < 2:
            break
            
        player1 = available_players[0]
        if await is_player_occupied(str(player1.user_id)):
            remove_player_from_queue(queue, player1)
            await queue.save_changes()
            continue
        last_opponent_id = await get_last_opponent(player1)
    
        candidates = sorted(available_players, key=lambda x: x.elo)
        player2 = await find_opponent(player1, last_opponent_id, candidates, max_elo_diff, tier, queue)
        
        if not player2:
            continue

        async def remove_matched_pair() -> None:
            remove_player_from_queue(queue, player1)
            remove_player_from_queue(queue, player2)
            await queue.save_changes()

        selections = (queue.district_selections.get(str(player1.user_id)),
                      queue.district_selections.get(str(player2.user_id)))
        if any(selection is not None for selection in selections):
            await initiate_match(bot, player1, player2, on_inserted=remove_matched_pair,
                                 queue_selections=selections)
        else:
            await initiate_match(bot, player1, player2, on_inserted=remove_matched_pair)

    await queue.save_changes()


async def send_warnings(bot: ipy.Client, token):
    match = await Match.find_one(Match.token == token)
    thread = await bot.fetch_channel(match.thread)
    await thread.send(f"<@&{STAFF_ROLE}>\n{IMPORTANT_EMOJI} The upcoming match is missing a host! 1v1 helpers "
                      f"please check if you got time for it.")


async def send_bases(bot: ipy.Client, token: str, player: int, opponent: int, scout: bool = True):
    match = await Match.find_one(Match.token == token)
    p_index = player
    player = await bot.fetch_user(match.participants[player].user_id)
    opponent = await bot.fetch_user(match.participants[opponent].user_id)

    base_embeds = await choose_bases(token, player, opponent)
    if not scout:
        return

    try:
        if (not match.is_war and not match.hosts[0]) or (match.is_war and not match.hosts[p_index]):
            await player.send(f"{IMPORTANT_EMOJI} It is 10 minutes before the match <#{match.thread}> and "
                              f"no host is available. Please reschedule your match, and make sure there is a "
                              f"host for it.")

            return

        await player.send(f"Bases you will attack for the match <#{match.thread}>", embeds=base_embeds)
    except ipy.errors.Forbidden:
        pass


def build_district_confirmation_modal(
        token: str,
        wizard_valley_available: bool = False,
        capital_peak_available: bool = False,
) -> dict:
    district_options = get_district_options(wizard_valley_available, capital_peak_available)
    labels = [
        {
            "type": 18,
            "label": f"Select the {index}{'st' if index == 1 else 'nd' if index == 2 else 'rd'} district",
            "component": {
                "type": 3,
                "custom_id": f"inline_district|{token}|{index - 1}",
                "options": [{"label": district, "value": district} for district in options],
                "required": True,
            },
        }
        for index, options in enumerate(district_options, start=1)
    ]

    return {
        "type": int(ipy.CallbackType.MODAL),
        "data": {
            "title": "Confirm Match",
            "custom_id": f"confirm_occupied_match_modal|{token}",
            "components": labels,
        },
    }


async def initiate_match(
        bot: ipy.Client, player1: Player, player2: Player,
        *, on_inserted: Callable[[], Awaitable[None]] | None = None,
        queue_selections: tuple[QueueDistrictSelection | None, QueueDistrictSelection | None] = (None, None),
):
    user1 = await bot.fetch_user(player1.user_id)
    user2 = await bot.fetch_user(player2.user_id)

    wizard_valley_available = player1.elo < 1200 and player2.elo < 1200
    capital_peak_available = player1.capital_peak_enabled and player2.capital_peak_enabled
    district_options = get_district_options(wizard_valley_available, capital_peak_available)
    p1_districts = [
        district if district in district_options[index] else None
        for index, district in enumerate(player1.district_presets)
    ]
    p2_districts = [
        district if district in district_options[index] else None
        for index, district in enumerate(player2.district_presets)
    ]
    for player, districts, selection in zip((player1, player2), (p1_districts, p2_districts), queue_selections):
        if selection is not None and selection.confirmed:
            continue
        if player.district_presets[0] == "Wizard Valley" and not wizard_valley_available:
            secondary = player.secondary_district_presets[0]
            districts[0] = secondary if secondary in district_options[0] else random.choice(district_options[0])
        if player.district_presets[2] == "Capital Peak" and not capital_peak_available:
            secondary = player.secondary_district_presets[2]
            districts[2] = secondary if secondary in district_options[2] else random.choice(district_options[2])

    token = secrets.token_hex(8)

    p1 = Participant(user_id=str(user1.id), districts=p1_districts, confirmed=all(p1_districts))
    p2 = Participant(user_id=str(user2.id), districts=p2_districts, confirmed=all(p2_districts))

    for participant, selection in zip((p1, p2), queue_selections):
        if selection is None:
            continue
        participant.queue_attempt_id = selection.attempt_id
        if selection.confirmed or not participant.confirmed:
            participant.districts = [
                district if district in options else random.choice(options) if district else None
                for district, options in zip(selection.districts, district_options)
            ]
            participant.confirmed = selection.confirmed and all(participant.districts)
        participant.queue_districts_locked = selection.confirmed

    primary_base_quality, secondary_base_quality = get_base_quality(player1, player2)

    matched_time = datetime.now().timestamp()
    for participant in (p1, p2):
        if participant.confirmed:
            participant.confirmed_at = matched_time
            participant.confirmation_method = "ready"

    # create and insert a match document
    match = Match(
        token=token,
        matched_time=matched_time,
        confirmation_tracking=True,
        base_submission=player1.elo >= BASE_SUBMISSION_THRESHOLD and player2.elo >= BASE_SUBMISSION_THRESHOLD,
        primary_base_quality=primary_base_quality,
        secondary_base_quality=secondary_base_quality,
        wizard_valley_available=wizard_valley_available,
        capital_peak_available=capital_peak_available,
        participants=[p1, p2],
    )
    await match.insert()
    if on_inserted is not None:
        await on_inserted()

    embed = ipy.Embed(
        title="**Match Found**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR,
    )

    district_selects = [
        ipy.StringSelectMenu(
            *(ipy.StringSelectOption(label=district, value=district) for district in options),
            placeholder=f"{index + 1}️⃣ Select the {ORDINAL_REPRESENTATIONS[index]} district",
            custom_id=f"district_select|{token}|{index}",
        )
        for index, options in enumerate(district_options)
    ]

    cancel_button = ipy.Button(
        style=ipy.ButtonStyle.DANGER,
        label="Cancel",
        custom_id=f"match_cancel|{token}",
        emoji=CROSS_EMOJI,
    )

    confirm_button = ipy.Button(
        style=ipy.ButtonStyle.SUCCESS,
        label="Confirm",
        custom_id=f"match_confirm|{token}",
        emoji=TICK_EMOJI,
    )

    view_opponent_button = ipy.Button(
        style=ipy.ButtonStyle.SECONDARY,
        label="Reveal Opponent",
        custom_id=f"reveal_opponent|{token}",
        emoji="🔍",
    )

    submit_bases_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Submit Bases",
        custom_id=f"submit_bases|{token}",
        emoji="📥",
    )

    # send DM to each player
    for participant, user in zip([p1, p2], [user1, user2]):
        message = "A match has been found, please read the information on the embed!"

        # build the last action row based on base_submission
        action_buttons = [cancel_button, view_opponent_button]
        if match.base_submission:
            action_buttons.append(submit_bases_button)

        # copy select menus so they don't share state
        components = [
            ipy.ActionRow(copy.deepcopy(district_selects[0])),
            ipy.ActionRow(copy.deepcopy(district_selects[1])),
            ipy.ActionRow(copy.deepcopy(district_selects[2])),
            ipy.ActionRow(*action_buttons),
        ]

        # Mark preset districts if any
        for i, d in enumerate(participant.districts):
            if d:
                components[i].components[0].placeholder = f"✅ {d} (Preset)"
            if participant.queue_districts_locked:
                components[i].components[0].disabled = True

        if participant.confirmed:
            embed.description = (
                "The bot will automatically confirm for your opponent if they do not confirm or cancel within 12 hours."
            )
            embed.fields = [
                ipy.EmbedField(
                    name="__**Instruction**__",
                    value=(
                        "1. Select your preferred districts for the match.\n"
                        "2. Confirm the match by pressing \"Confirm\".\n"
                        "3. Once both players have confirmed the match, "
                        f"a thread will be made in <#{MATCH_CHANNEL}>."
                    ),
                )
            ]
        else:
            components[3] = ipy.ActionRow(cancel_button, confirm_button)
            embed.description = (
                "The bot will automatically confirm for you if you do not confirm or cancel within 12 hours."
            )
            embed.fields = [
                ipy.EmbedField(
                    name="__**Instruction**__",
                    value=(
                        "1. Select your preferred districts for the match.\n"
                        "2. Confirm the match by pressing \"Confirm\".\n"
                        "3. Once both players have confirmed the match, "
                        f"a thread will be made in <#{MATCH_CHANNEL}>."
                    ),
                )
            ]

        try:
            await user.send(message, embed=embed, components=components)
        except ipy.errors.Forbidden:
            command_channel = await bot.fetch_channel(COMMAND_CHANNEL)
            await command_channel.send(
                f"{user.mention}\n{message}\n\n"
                "*This message is sent here, because you have disabled direct messages.*",
                embed=embed,
                components=components,
            )

    # Auto-confirm if both participants already confirmed
    if p1.confirmed and p2.confirmed:
        await confirm_match(bot, match.token)


async def record_match(guild: ipy.Guild, bot: ipy.Client, player1_id: str, player2_id: str, response: dict[str, str],
                       final_districts: list[str], token: str = None, *, manual_key: str | None = None):
    # fetch players
    player1 = await Player.find_one(Player.user_id == player1_id, fetch_links=True)
    player2 = await Player.find_one(Player.user_id == player2_id, fetch_links=True)

    player1_results = response["player1"].split("\n")
    player1_stars = list(map(get_stars, player1_results))
    player1_total_stars = sum(player1_stars)
    player1_destruction = list(map(get_destruction, player1_results))
    player1_total_destruction = sum(player1_destruction)

    player2_results = response["player2"].split("\n")
    player2_stars = list(map(get_stars, player2_results))
    player2_total_stars = sum(player2_stars)
    player2_destruction = list(map(get_destruction, player2_results))
    player2_total_destruction = sum(player2_destruction)

    player1_elo = player1.elo
    player2_elo = player2.elo

    # check if the match is a challenge
    is_challenge = False
    match = await Match.find_one(Match.token == token)
    if token and not match:
        raise ResultOperationConflict(token, "The match no longer exists. Its result was not recorded.")
    if token and match:
        if is_final_match(match):
            raise ResultOperationConflict(token, "Final results use `/final match result`, without ranked recording.")
        is_challenge = match.is_challenge
        if match.result_operation and match.result_operation.completed:
            await begin_result_operation(match, response)
            return await completed_result_changes(match)

    # calculate player1 elo
    new_p1_elo = calculate_elo(player1_elo, player2_elo, player1_stars, player1_destruction,
                               player2_stars, player2_destruction, is_challenge)
    player1_elo_change = new_p1_elo - player1_elo
    player1_result_emoji = WIN_EMOJI if player1_elo_change >= 0 else LOSS_EMOJI
    player1_elo_change_sign = "+" if player1_elo_change >= 0 else ""

    # calculate player2 elo
    new_p2_elo = calculate_elo(player2_elo, player1_elo, player2_stars, player2_destruction,
                               player1_stars, player1_destruction, is_challenge)
    player2_elo_change = new_p2_elo - player2_elo
    player2_result_emoji = WIN_EMOJI if player2_elo_change >= 0 else LOSS_EMOJI
    player2_elo_change_sign = "+" if player2_elo_change >= 0 else ""

    # generate match data
    ts = int(datetime.now().timestamp())
    
    # Get next match ID
    last_match = await RecordedMatch.find_all().sort("-time").first_or_none()
    if last_match and last_match.id.isdigit():
        match_id = str(int(last_match.id) + 1)
    else:
        match_id = "0"

    p1_stats = MatchPlayerStats(
        stars=player1_total_stars,
        destruction=player1_total_destruction,
        opponent_stars=player2_total_stars,
        opponent_destruction=player2_total_destruction,
        opponent=player2_id,
        elo_change=player1_elo_change,
        won=player1_total_stars > player2_total_stars or (
                player1_total_stars == player2_total_stars and
                player1_total_destruction > player2_total_destruction),
        districts={
            final_districts[0]: MatchDistrictStats(stars=get_stars(player1_results[0]), destruction=get_destruction(player1_results[0])),
            final_districts[1]: MatchDistrictStats(stars=get_stars(player1_results[1]), destruction=get_destruction(player1_results[1])),
            final_districts[2]: MatchDistrictStats(stars=get_stars(player1_results[2]), destruction=get_destruction(player1_results[2])),
        }
    )

    p2_stats = MatchPlayerStats(
        stars=player2_total_stars,
        destruction=player2_total_destruction,
        opponent_stars=player1_total_stars,
        opponent_destruction=player1_total_destruction,
        opponent=player1_id,
        elo_change=player2_elo_change,
        won=player2_total_stars > player1_total_stars or (
                player2_total_stars == player1_total_stars and
                player2_total_destruction > player1_total_destruction),
        districts={
            final_districts[0]: MatchDistrictStats(stars=get_stars(player2_results[0]), destruction=get_destruction(player2_results[0])),
            final_districts[1]: MatchDistrictStats(stars=get_stars(player2_results[1]), destruction=get_destruction(player2_results[1])),
            final_districts[2]: MatchDistrictStats(stars=get_stars(player2_results[2]), destruction=get_destruction(player2_results[2])),
        }
    )

    if match and match.confirmation_tracking:
        for participant in match.participants:
            stats = {player1_id: p1_stats, player2_id: p2_stats}.get(participant.user_id)
            if stats is not None:
                stats.confirmed_at = participant.confirmed_at
                stats.confirmation_method = participant.confirmation_method

    recorded_match = RecordedMatch(
        id=match_id,
        time=ts,
        matched_time=match.matched_time if match and match.confirmation_tracking else None,
        is_challenge=is_challenge,
        players={
            player1_id: p1_stats,
            player2_id: p2_stats
        }
    )
    if token and match:
        await begin_result_operation(match, response)
        recorded_match = await persist_result_record(match, recorded_match)
    elif manual_key is not None:
        recorded_match = await persist_manual_result(recorded_match, manual_key)
    else:
        await recorded_match.insert()

    player1_elo_change = recorded_match.players[player1_id].elo_change
    player2_elo_change = recorded_match.players[player2_id].elo_change
    player1_result_emoji = WIN_EMOJI if player1_elo_change >= 0 else LOSS_EMOJI
    player2_result_emoji = WIN_EMOJI if player2_elo_change >= 0 else LOSS_EMOJI
    player1_elo_change_sign = "+" if player1_elo_change >= 0 else ""
    player2_elo_change_sign = "+" if player2_elo_change >= 0 else ""

    committed_elos: list[int] = []
    for player_doc in [player1, player2]:
        committed_elos.append(await add_recorded_match(player_doc, recorded_match))
        while True:
            player_doc = await Player.find_one(Player.user_id == player_doc.user_id)
            expected_history = [record.to_ref() for record in player_doc.matches]
            player_matches = await RecordedMatch.find(In("_id", [ref.id for ref in expected_history])).to_list()
            stats_update = {
                "wins": get_wins(player_matches, player_doc.user_id),
                "avg_stars": get_avg_stars(player_matches, player_doc.user_id),
                "avg_destruction": get_avg_destruction(player_matches, player_doc.user_id),
                "opponent_avg_stars": get_opponent_avg_stars(player_matches, player_doc.user_id),
                "opponent_avg_destruction": get_opponent_avg_destruction(player_matches, player_doc.user_id),
            }
            result = await Player.get_pymongo_collection().update_one(
                {"user_id": player_doc.user_id, "matches": expected_history},
                {"$set": stats_update},
            )
            if result.matched_count:
                player_doc = player_doc.model_copy(update={**stats_update, "matches": player_matches})
                break

        # update leaderboard cache
        leaderboard_cog = bot.get_ext("LeaderboardCommands")
        if leaderboard_cog:
            await leaderboard_cog.update_player_cache(player_doc)

    new_p1_elo, new_p2_elo = committed_elos
    player1_elo_str = (f"{player1_result_emoji}{new_p1_elo - player1_elo_change} → {new_p1_elo} "
                       f"({player1_elo_change_sign}{player1_elo_change})")
    player2_elo_str = (f"{player2_result_emoji}{new_p2_elo - player2_elo_change} → {new_p2_elo} "
                       f"({player2_elo_change_sign}{player2_elo_change})")

    player1_user = await fetch_member_otherwise_user(guild, bot, player1_id)
    player2_user = await fetch_member_otherwise_user(guild, bot, player2_id)

    play_again_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Play Again",
        custom_id="match_queue",
        emoji=CLASHING_SWORD_EMOJI
    )

    # notify player1
    player1_embed = ipy.Embed(
        title=f"**Match Against {player2_user.display_name}**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )
    player1_embed.description = \
        f"**Your Score:** {player1_total_stars}{STAR_EMOJI} {player1_total_destruction}%\n" \
        f"**Opponent Score:** {player2_total_stars}{STAR_EMOJI} {player2_total_destruction}%\n" \
        f"**Elo:** {player1_elo_str}"
    try:
        await player1_user.send(embed=player1_embed, components=play_again_button)
    except ipy.errors.Forbidden:
        pass

    # notify player2
    player2_embed = ipy.Embed(
        title=f"**Match Against {player1_user.display_name}**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )
    player2_embed.description = \
        f"**Your Score:** {player2_total_stars}{STAR_EMOJI} {player2_total_destruction}%\n" \
        f"**Opponent Score:** {player1_total_stars}{STAR_EMOJI} {player1_total_destruction}%\n" \
        f"**Elo:** {player2_elo_str}"
    try:
        await player2_user.send(embed=player2_embed, components=play_again_button)
    except ipy.errors.Forbidden:
        pass

    if manual_key is not None:
        await complete_manual_result(recorded_match)
        await invite_eligible_players(bot, (player1_id, player2_id))

    await fetch_user_match_id_choices(bot, guild, player1_id)
    await fetch_user_match_id_choices(bot, guild, player2_id)
    return (f"{player1_result_emoji}{player1_elo_change_sign}{player1_elo_change}",
            f"{player2_result_emoji}{player2_elo_change_sign}{player2_elo_change}")


async def cancel_match(bot: ipy.Client, token):
    async with QUEUE_OPERATION_LOCK:
        match = await Match.find_one(Match.token == token)
        if not match or any(participant.confirmed for participant in match.participants):
            return
        await remove_matched_queue_attempts(match)
        if not await delete_unreserved_match(match, pre_thread=True):
            return
    player1 = await bot.fetch_user(match.participants[0].user_id)
    player2 = await bot.fetch_user(match.participants[1].user_id)

    try:
        await player1.send(f"{IMPORTANT_EMOJI} Your match against {player2.mention} has been canceled. This is "
                           f"either due to both of you not confirming for more than 12 hours or the match failed to "
                           f"initiate due to some unknown bug.")
    except ipy.errors.Forbidden:
        pass

    try:
        await player2.send(f"{IMPORTANT_EMOJI} Your match against {player1.mention} has been canceled. This is "
                           f"either due to both of you not confirming for more than 12 hours or the match failed to "
                           f"initiate due to some unknown bug.")
    except ipy.errors.Forbidden:
        pass


# per-token locks to prevent concurrent confirm_match calls on the same match
_confirm_match_locks: dict[str, asyncio.Lock] = {}


class MatchChannelUnavailable(RuntimeError):
    """A channel lookup returned no channel without confirming its deletion."""


async def confirm_match(bot: ipy.Client, token: str) -> str:
    async with _confirm_match_locks.setdefault(token, asyncio.Lock()):
        match = await Match.find_one(Match.token == token)
        if not match:
            raise ValueError(f"No match found with token={token}")

        if match.thread:
            if (any(match.times) or any(match.old_msgs) or match.is_autowin
                    or any(any(participant.result) for participant in match.participants)):
                return str(match.thread)
            thread = await bot.fetch_channel(match.thread)
            if thread is None:
                raise MatchChannelUnavailable(f"Match {token} thread {match.thread} is unavailable")
            msg = await find_match_thread_announcement(thread, match, bot.user.id)
            if msg is not None:
                if match.thread_message_id != int(msg.id) and not await attach_thread_message(match, int(msg.id)):
                    raise ThreadMessageBindingError(token)
                if (is_final_match(match) and match.base_submission and not match.final_superseded
                        and match.final_result_id is None and not any(
                            component.custom_id == f"submit_bases|{token}"
                            for row in msg.components for component in row.components)):
                    await msg.edit(components=[ipy.ActionRow(ipy.Button(
                        style=ipy.ButtonStyle.GRAY, label="Submit Bases", custom_id=f"submit_bases|{token}",
                        emoji=ipy.PartialEmoji(name="📥"))), *msg.components])
                if f"<@&{STAFF_ROLE}>" not in msg.content:
                    await msg.edit(content=f"{msg.content}\n<@&{STAFF_ROLE}>")
                if not msg.pinned:
                    await msg.pin()
                return str(match.thread)
        else:
            channel = await bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                             else MATCH_CHANNEL)
            if channel is None:
                raise MatchChannelUnavailable(f"Match {token} parent channel is unavailable")

        # fetch players from participants
        p1, p2 = match.participants
        player1 = await bot.fetch_user(int(p1.user_id))
        player2 = await bot.fetch_user(int(p2.user_id))

        if not match.thread:
            if is_final_match(match):
                claimed = await match.get_pymongo_collection().update_one(
                    {"_id": match.id, "thread": None, "final_thread_creation_claimed": False},
                    {"$set": {"final_thread_creation_claimed": True}},
                )
                if not claimed.matched_count:
                    raise ThreadMessageBindingError(token)
                match.final_thread_creation_claimed = True
            # create private thread
            thread_name = f"🟡 {player1} vs. {player2}".replace("@", "")
            thread = await channel.create_private_thread(
                name=thread_name,
                invitable=False,
                auto_archive_duration=ipy.AutoArchiveDuration.ONE_WEEK,
            )

            # randomly assign districts from participants
            seed = random.choice([
                [0, 0, 1], [0, 1, 1], [0, 1, 0],
                [1, 0, 0], [1, 0, 1], [1, 1, 0],
            ])
            matched_players = [p1, p2]

            district1 = matched_players[seed[0]].districts[0]
            district2 = matched_players[seed[1]].districts[1]
            district3 = matched_players[seed[2]].districts[2]

            if not is_final_match(match):
                match.final_districts = [district1, district2, district3]
            match.thread = str(thread.id)

            # only load preset bases when base submission is enabled
            if match.base_submission:
                for participant in match.participants:

                    player = await Player.find_one(Player.user_id == participant.user_id, fetch_links=True)

                    # for each assigned district, check if a valid preset base exists
                    new_bases = []
                    for district in match.final_districts:
                        base = player.base_presets.get(district)
                        if (
                                base
                                and base.expiration_date
                                and base.expiration_date >= datetime.now().timestamp()
                        ):
                            new_bases.append(
                                BaseLink(
                                    link=base.link,
                                    image=base.image_url,
                                    expiration_date=base.expiration_date,
                                )
                            )
                        else:
                            new_bases.append(None)

                    participant.bases = new_bases

            # save updated match
            await match.save_changes()

        embed = ipy.Embed(
            title=f"**{player1} vs. {player2}**",
            description=(
                f"- Ping <@&{STAFF_ROLE}> to start the match, and keep an eye on the chat for the next 10 minutes."
                + ("\n- Submit bases (</match base submit:1288780187042971700>) 10 minutes prior to the match."
                   if match.base_submission else "")
            ),
            fields=[
                ipy.EmbedField(
                    name="**Assigned Districts**",
                    value="\n".join(f"{index}. {DISTRICT_EMOJI} {district}"
                                    for index, district in enumerate(match.final_districts, 1)),
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Scheduling**",
                    value="Use [HammerTime](https://old.hammertime.cyou/en-GB) to "
                          "generate a Discord timestamp and send in chat.",
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Live Matches**",
                    value="By default the players attack independently. To attack together, "
                          "please inform each other and the staff!",
                    inline=False,
                ),
                ipy.EmbedField(
                    name="**Rules**",
                    value="1. 2 attacks per district.\n"
                          "2. 3 miners maximum per attack.\n"
                          + ("3. The host manages match timing." if is_final_match(match)
                             else "3. Finish the attacks in 30 minutes."),
                    inline=False,
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        if not match.base_submission:
            notes = [f"- Base submission is disabled for matches between players under {BASE_SUBMISSION_THRESHOLD} Elo."]
        else:
            names = {p.user_id: mention_user(player) for p, player in zip(match.participants, (player1, player2))}
            notes = [f"- {names[p.user_id]} has preset bases." for p in match.participants if any(p.bases)]
            notes.extend(f"- {names[p.user_id]} wish to submit bases." for p in match.participants if p.submit_bases)

        if notes:
            embed.add_field(name="**Notes**", value="\n".join(notes))

        submit_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Result",
            custom_id=f"submit_button|{token}",
            emoji=ipy.PartialEmoji(name="📥"),
        )

        controls = [submit_button]
        if is_final_match(match) and match.base_submission:
            controls.insert(0, ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Bases",
                custom_id=f"submit_bases|{token}", emoji=ipy.PartialEmoji(name="📥")))

        # Send a match thread message
        msg = await thread.send(
            f"{player1.mention}\n{player2.mention}",
            embed=embed,
            components=[ipy.ActionRow(*controls)],
            nonce=f"match|{token}", enforce_nonce=True,
        )
        if not await attach_thread_message(match, int(msg.id)):
            raise ThreadMessageBindingError(token)
        await msg.edit(content=f"{msg.content}\n<@&{STAFF_ROLE}>")  # avoids ping
        await msg.pin()

        return str(thread.id)


def get_previously_recorded_match_id(
    recorded_matches: dict, player_id: str,
    opponent_id: str, players: dict, match: Match,
) -> str | None:

    # locate participants
    p_player = next((p for p in match.participants if p.user_id == player_id), None)
    p_oppo = next((p for p in match.participants if p.user_id == opponent_id), None)

    player_matches = players[player_id]["matches"]
    player_matches_reversed = list(reversed(player_matches))

    for i in range(5):
        if i >= len(player_matches_reversed):
            return None

        previous_match_id = player_matches_reversed[i]
        previous_match = recorded_matches[previous_match_id]

        if previous_match[player_id]["opponent"] != opponent_id:
            continue

        district_keys = list(previous_match[player_id]["districts"].keys())
        if district_keys != list(match.final_districts):
            continue

        player_res = p_player.result
        opponent_res = p_oppo.result

        player_stars = list(map(get_stars, player_res))
        player_total_stars = sum(player_stars)
        if previous_match[player_id]["stars"] != player_total_stars:
            continue

        player_destruction = list(map(get_destruction, player_res))
        player_total_destruction = sum(player_destruction)
        if previous_match[player_id]["destruction"] != player_total_destruction:
            continue

        opponent_stars = list(map(get_stars, opponent_res))
        opponent_total_stars = sum(opponent_stars)
        if previous_match[opponent_id]["stars"] != opponent_total_stars:
            continue

        opponent_destruction = list(map(get_destruction, opponent_res))
        opponent_total_destruction = sum(opponent_destruction)
        if previous_match[opponent_id]["destruction"] != opponent_total_destruction:
            continue

        return previous_match_id


async def record_base_stats(player_results: dict, match: Match):
    if is_final_match(match):
        raise ResultOperationConflict(match.token, "Final base scores belong to the dedicated Final result.")
    operation = match.result_operation
    for side, participant in enumerate(match.participants):
        opponent = next((p for p in match.participants if p.user_id != participant.user_id), None)
        if not opponent:
            continue

        opponent_result = player_results.get(opponent.user_id, []) or []

        for j in range(3):
            base = participant.bases[j] if j < len(participant.bases) else None
            district = match.final_districts[j] if j < len(match.final_districts) else None

            if not base or not district:
                continue
            if j >= len(opponent_result) or not opponent_result[j]:
                continue

            # filter out outliers
            stars = get_stars(opponent_result[j])
            destruction = get_destruction(opponent_result[j])
            if (stars == 0 and destruction == 0) or (destruction == 100 and stars < 3) or (destruction < 50 and stars > 1):
                continue

            # record attack result
            now = operation.created_at if operation else datetime.now().timestamp()
            new_record = [opponent.user_id, stars, destruction, now]
            if operation:
                receipt = f"{operation.key}:base:{side}:{j}"
                await BaseLayout.get_pymongo_collection().update_one(
                    {"link": base.link, "operation_receipts": {"$ne": receipt}},
                    {
                        "$push": {"records": new_record},
                        "$max": {"expiration_date": now + 28 * 86400},
                        "$addToSet": {"operation_receipts": receipt},
                    },
                )
                continue
            await BaseLayout.find_one(
                BaseLayout.link == base.link
            ).update(
                {
                    "$push": {"records": new_record},
                    "$max": {"expiration_date": now + 28 * 86400}
                }
            )


async def is_player_occupied(player_id: str) -> PlayerOccupied | None:
    matches = await Match.find(
        {"participants.user_id": str(player_id)}
    ).to_list()

    for match in matches:
        if match.is_autowin:
            continue
        
        participant = next((p for p in match.participants if p.user_id == str(player_id)))
        opponent = next(p for p in match.participants if p.user_id != str(player_id))
        # unconfirmed challenge where this player created it
        if not match.thread and match.is_challenge:
            if match.participants[0].user_id == str(player_id):
                return PlayerOccupied(f"You have an unconfirmed challenge request against <@{opponent.user_id}>.")
            if match.participants[1].user_id == str(player_id):
                return PlayerOccupied(f"<@{opponent.user_id}> has challenged to you, please accept or reject it.")

        # unconfirmed normal match (district selection stage)
        if not match.thread and not match.is_challenge:
            if participant.confirmed:
                return PlayerOccupied(f"You have an upcoming match against <@{opponent.user_id}>.")
            else:
                return PlayerOccupied(
                    "You have an unconfirmed match, please check your DM or confirm it now.",
                    ipy.Button(
                        style=ipy.ButtonStyle.GRAY,
                        label="Confirm Match",
                        custom_id=f"confirm_occupied_match|{match.token}"
                    ),
                )

        # regular matches finish together; war matches finish per player
        finished_player = match.participants.index(participant) if match.is_war else None
        if not is_match_finished(match, player=finished_player):
            return PlayerOccupied(f"You have an unfinished match in <#{match.thread}>.")

    return None


async def queue_player(
        bot: ipy.Client,
        author: ipy.Member | ipy.User,
        priority: bool = False,
        *, district_picker: bool = False,
) -> QueueOutcome:
    async with QUEUE_OPERATION_LOCK:
        if isinstance(author, ipy.Member) and author.has_role(ELO_BLACKLIST_ROLE):
            return QueueOutcome(QueueOutcomeStatus.BLACKLISTED)

        if isinstance(author, ipy.User):
            guild = await bot.fetch_guild(MAIN_GUILD)
            member_obj = await guild.fetch_member(str(author.id))
            if member_obj.has_role(ELO_BLACKLIST_ROLE):
                return QueueOutcome(QueueOutcomeStatus.BLACKLISTED)

        if (await ServerState.current()).queue_disabled:
            return QueueOutcome(QueueOutcomeStatus.DISABLED)

        player = await Player.find_one(Player.user_id == str(author.id))
        if not player:
            return QueueOutcome(QueueOutcomeStatus.UNREGISTERED)

        queues = {tier: await get_match_queue(tier) for tier in QUEUE_TIERS}
        await reconcile_queue_memberships(list(queues.values()))
        now = datetime.now().timestamp()
        for queue in queues.values():
            if normalize_queue_timestamps(queue, now):
                await queue.save_changes()

        await promote_tier1_aged_players(queues[QUEUE_TIER_AGED], queues[QUEUE_TIER_MIXED], now)

        for queue in queues.values():
            if player.user_id in [queued_player.user_id for queued_player in queue.players]:
                return QueueOutcome(QueueOutcomeStatus.ALREADY_QUEUED)

        player_occupied = await is_player_occupied(str(author.id))
        if player_occupied:
            return QueueOutcome(QueueOutcomeStatus.OCCUPIED, player_occupied)

        last_opponent_id = await get_last_opponent(player)

        tier0_queue = queues[QUEUE_TIER_AGED]
        tier1_queue = queues[QUEUE_TIER_MIXED]
        tier2_queue = queues[QUEUE_TIER_PRIORITY]
        tier3_queue = queues[QUEUE_TIER_NORMAL]

        if await match_with_best_queue(bot, player, last_opponent_id, [(tier0_queue, 250)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        if await match_with_best_queue(bot, player, last_opponent_id, [(tier1_queue, 200)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        stored_priority = bool(player.priority)
        requested_priority = False
        if priority:
            closest_player = await get_closest_elo_player(player)
            closest_elo_diff = abs(closest_player.elo - player.elo) if closest_player else None
            requested_priority = closest_elo_diff is not None and closest_elo_diff > 100

        use_priority_queue = stored_priority or requested_priority

        if use_priority_queue:
            if await match_with_best_queue(
                bot,
                player,
                last_opponent_id,
                [(tier2_queue, 150), (tier3_queue, 150)],
            ):
                if stored_priority:
                    player.priority = False
                    await player.save_changes()

                return QueueOutcome(QueueOutcomeStatus.MATCHED)
        elif await match_with_best_queue(bot, player, last_opponent_id, [(tier2_queue, 150)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        if int(bot.user.id) != BOT_ID:
            add_player_to_queue(tier1_queue, player, now)
            selection = None
            if district_picker:
                selection = start_district_selection(
                    player, get_district_options(player.elo < 1200, player.capital_peak_enabled),
                )
                tier1_queue.district_selections[str(player.user_id)] = selection
            await tier1_queue.save_changes()
            return QueueOutcome(QueueOutcomeStatus.BETA_QUEUED, selection=selection)

        if not use_priority_queue and await match_with_best_queue(bot, player, last_opponent_id, [(tier3_queue, 200)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        target_queue = tier2_queue if use_priority_queue else tier3_queue
        add_player_to_queue(target_queue, player, now)
        selection = None
        if district_picker:
            selection = start_district_selection(
                player, get_district_options(player.elo < 1200, player.capital_peak_enabled),
            )
            target_queue.district_selections[str(player.user_id)] = selection
        await target_queue.save_changes()

        if use_priority_queue:
            if stored_priority:
                player.priority = False
                await player.save_changes()

        if not use_priority_queue and len(tier3_queue.players) >= 4:
            await run_queue_matching_trigger(bot)
            refreshed_queues = [await get_match_queue(tier) for tier in QUEUE_TIERS]
            player_still_in_queue = any(
                queued_player.user_id == str(author.id)
                for queue in refreshed_queues
                for queued_player in queue.players
            )
            if not player_still_in_queue:
                return QueueOutcome(QueueOutcomeStatus.MATCHED)

        return QueueOutcome(QueueOutcomeStatus.QUEUED, selection=selection)


async def autoqueue_completed_match_participants(
        bot: ipy.Client,
        token: str,
        participant_ids: tuple[str, ...],
) -> None:
    now = int(datetime.now().timestamp())
    for participant_id in participant_ids:
        try:
            player = await Player.find_one(Player.user_id == participant_id)
            if not player or not is_autoqueue_active(player.auto_queue, now):
                continue

            guild = await bot.fetch_guild(MAIN_GUILD)
            member = await guild.fetch_member(participant_id)
            await queue_player(bot, member, priority=False)
        # This is the terminal automation boundary: a participant-specific Discord,
        # database, or queue-core failure is reported without undoing finalization.
        except Exception:  # noqa: BROAD_EXCEPT_OK
            print(f"Terminal autoqueue failed for match {token}, participant {participant_id}.", file=sys.stderr)
            traceback.print_exc()


async def choose_bases(token: str, player: ipy.User, opponent: ipy.User,
                       return_match_data: bool = False) -> list[ipy.Embed] | Match:
    # determine the quality of the bases
    player_doc = await Player.find_one(Player.user_id == str(player.id))
    opponent_doc = await Player.find_one(Player.user_id == str(opponent.id))
    match = await Match.find_one(Match.token == token)
    primary_quality, secondary_quality = get_base_quality(player_doc, opponent_doc, match)

    opponent_index = next(i for i, participant in enumerate(match.participants)
                          if participant.user_id == str(opponent.id))
    match = await ensure_secondary_base_index(match, opponent_index, primary_quality, secondary_quality)
    seed = match.participants[opponent_index].secondary_base_index
    embeds = []
    index = -1
    now = datetime.now().timestamp()
    opponent_participant = next(p for p in match.participants if p.user_id == str(opponent.id))
    player_participant = next(p for p in match.participants if p.user_id == str(player.id))
    for base in opponent_participant.bases:
        index += 1
        embed = ipy.Embed(
            title=f"**{match.final_districts[index]}**",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        # determine the base quality in case the players have different base quality
        if index == seed:
            base_quality = secondary_quality
        else:
            base_quality = primary_quality

        if not base:
            bases_for_opponent = [b.link for b in player_participant.bases if b]

            # find bases the player has just attacked
            on_going_bases = []
            on_going_matches = await Match.find(
                {"participants.user_id": str(player.id)}
            ).to_list()
            for m in on_going_matches:
                opp = next(p for p in m.participants if p.user_id != str(player.id))
                for base_data in opp.bases:
                    if base_data:
                        on_going_bases.append(base_data.link)

            # filter bases built the player themselves
            # filter bases identical to opponent's bases if it is not a war match
            # filter bases that are expired
            # filter bases on quality
            # filter bases that has just been attacked
            base_quality = district_base_quality(match.final_districts[index], base_quality)
            bases_filtered = await BaseLayout.find(
                BaseLayout.district == match.final_districts[index],
                BaseLayout.builder_id != str(player.id),
                BaseLayout.quality == base_quality,
                NotIn(BaseLayout.link, bases_for_opponent),
                NotIn(BaseLayout.link, on_going_bases),
                BaseLayout.expiration_date > now
            ).to_list()

            # no available base for the player
            if not bases_filtered:
                embed.set_image(UNAVAILABLE_IMAGE)
                embed.description = f"{IMPORTANT_EMOJI} Base missing.\n"
                continue

            final_hits = {}
            final_usage = {}
            if is_final_match(match):
                from database_models.final_models import FinalMatchResult
                history = await FinalMatchResult.find({"snapshot.contestants": str(player.id), "snapshot.outcome": "Played"}).to_list()
                seen_attempts = set()
                for result in history:
                    attempt_key = (result.final_id, result.node_id, result.contestant_revision, result.attempt)
                    if attempt_key in seen_attempts:
                        continue
                    seen_attempts.add(attempt_key)
                    for owner, links in result.bases.items():
                        if owner == str(player.id):
                            continue
                        for link in filter(None, links):
                            final_hits[link] = max(final_hits.get(link, 0), result.created_at)
                            final_usage[link] = final_usage.get(link, 0) + 1

            def latest_player_attack_timestamp(base_layout: BaseLayout) -> int | float:
                latest_timestamp: int | float = final_hits.get(base_layout.link, 0)
                for record in base_layout.records:
                    if not isinstance(record, (list, tuple)) or len(record) < 4:
                        continue

                    timestamp = record[3]
                    if (
                            str(record[0]) == str(player.id)
                            and isinstance(timestamp, (int, float))
                            and not isinstance(timestamp, bool)
                            and timestamp > 0
                    ):
                        latest_timestamp = max(latest_timestamp, timestamp)

                return latest_timestamp

            # only bases tied for the player's oldest last hit are eligible; unseen bases have timestamp zero.
            bases_with_last_hit = [(base, latest_player_attack_timestamp(base)) for base in bases_filtered]
            oldest_last_hit = min(timestamp for _, timestamp in bases_with_last_hit)
            oldest_bases = [base for base, timestamp in bases_with_last_hit if timestamp == oldest_last_hit]

            # favor the least-used bases within the eligible pool.
            finalists = sorted(oldest_bases, key=lambda base: len(base.records) + final_usage.get(base.link, 0))[:3]

            # relative weights give 50/25/25 with three finalists and normalize for smaller pools.
            chosen_base = random.choices(finalists, weights=[2, 1, 1][:len(finalists)], k=1)[0]

            # set the bases for the opponent
            embed.set_image(chosen_base.image_url)
            opponent_participant.bases[index] = BaseLink(
                link=chosen_base.link,
                image=chosen_base.image_url
            )
        else:
            embed.set_image(base.image)

        embeds.append(embed)

    await match.save_changes()

    if return_match_data:
        return match

    return embeds


async def choose_tip():
    tips: list = await read_json("tips.json", "assets/")
    facts: list = await read_json("facts.json", "assets/")
    count = len(tips) + len(facts)
    index = await ServerState.advance_tip_index(count)
    facts_before = index * len(facts) // count

    if (index + 1) * len(facts) // count > facts_before:
        return f"💡 **ELO FACT:** \"{facts[facts_before]}\""
    return f"💡 **TIP:** \"{tips[index - facts_before]}\""


async def _mark_result_publication_incomplete(message: ipy.Message) -> None:
    pending_embed = ipy.Embed(
        title="**Result Publication Incomplete**",
        description="Results are recorded. This message is pending durable tracking and publication.",
        color=COLOR,
    )
    try:
        await edit_channel_message(message, embed=pending_embed, components=[])
    except ipy.errors.HTTPException:
        logging.exception("Unable to mark result message %s incomplete", message.id)


class MatchCommands(ipy.Extension):
    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self.helper_api_base_url = HELPER_API_BASE_URL
        self.helper_api_key = HELPER_API_KEY
        self._pending_match_thread_renames: dict[str, asyncio.Task[None]] = {}
        self._hosting_queue_embed_update_lock = asyncio.Lock()
        self._match_publication_repair_lock = anyio.Lock()

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client

    base = ipy.SlashCommand(name="match", description="", dm_permission=True)

    @base.subcommand(sub_cmd_name="dequeue", sub_cmd_description="Stop queuing for a match based on your league")
    async def match_dequeue(self, ctx: ipy.SlashContext):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)

            return

        player_removed = False
        async with QUEUE_OPERATION_LOCK:
            for tier in QUEUE_TIERS:
                queue = await Queue.find_one(Queue.tier == tier, fetch_links=True)
                if queue and remove_player_from_queue(queue, str(ctx.author.id)):
                    await queue.save_changes()
                    player_removed = True

        if player_removed:
            await ctx.send(f"{SUCCESS_EMOJI} You have been removed from the match-making queue.", ephemeral=True)
        else:
            await ctx.send(f"{ERROR_EMOJI} You are not currently in the match-making queue.", ephemeral=True)

    @base.subcommand(sub_cmd_name="autoqueue", sub_cmd_description="Automatically queue after completed matches")
    @ipy.slash_option(
        name="enabled",
        description="Enable automatic queueing after completed matches",
        opt_type=ipy.OptionType.BOOLEAN,
        required=True,
    )
    @ipy.slash_option(
        name="duration",
        description="How long autoqueue should stay enabled",
        opt_type=ipy.OptionType.STRING,
        required=False,
        choices=[
            ipy.SlashCommandChoice(name="Forever", value="Forever"),
            ipy.SlashCommandChoice(name="7 Days", value="7 Days"),
            ipy.SlashCommandChoice(name="3 Days", value="3 Days"),
            ipy.SlashCommandChoice(name="1 Day", value="1 Day"),
        ],
    )
    async def match_autoqueue(self, ctx: ipy.SlashContext, enabled: bool, duration: str | None = None):
        player = await Player.find_one(Player.user_id == str(ctx.author.id))
        if not player:
            await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                           f"please register using the command `/player register`.", ephemeral=True)
            return

        expiry = autoqueue_expiry(duration) if enabled and duration else None
        if enabled and expiry is None:
            await ctx.send(f"{ERROR_EMOJI} Please select how long autoqueue should stay enabled.", ephemeral=True)
            return

        player.auto_queue = expiry if enabled else "0"
        await player.save_changes()
        await ctx.send(f"{SUCCESS_EMOJI} Autoqueue preference updated.", ephemeral=True)

    @base.subcommand(sub_cmd_name="queue", sub_cmd_description="Start queuing for a match based on your league")
    @ipy.slash_option(
        name="priority",
        description="Try priority matching if the closest Elo player's Elo is more than 100 away",
        opt_type=ipy.OptionType.BOOLEAN,
        required=False
    )
    async def match_queue(self, ctx: ipy.SlashContext | ipy.ComponentContext, priority: bool = False):
        await ctx.defer(ephemeral=True)
        outcome = await queue_player(self.bot, ctx.author, priority, district_picker=True)
        match outcome.status:
            case QueueOutcomeStatus.BLACKLISTED:
                await ctx.send(f"{ERROR_EMOJI} You are currently banned from playing Elo. If you are unsure of why, "
                               f"please open a support ticket in <#{INFO_CHANNEL}>.", ephemeral=True)
            case QueueOutcomeStatus.DISABLED:
                await ctx.send(f"{ERROR_EMOJI} Match-making is currently disabled due to season reset.",
                               ephemeral=True)
            case QueueOutcomeStatus.UNREGISTERED:
                await ctx.send(f"{ERROR_EMOJI} You are not registered in the elo system, "
                               f"please register using the command `/player register`.", ephemeral=True)
            case QueueOutcomeStatus.ALREADY_QUEUED:
                await ctx.send(f"{ERROR_EMOJI} You are already in the match-making queue.", ephemeral=True)
            case QueueOutcomeStatus.OCCUPIED:
                if outcome.occupied:
                    await ctx.send(
                        f"{ERROR_EMOJI} {outcome.occupied.message}",
                        components=outcome.occupied.component,
                        ephemeral=True,
                    )
            case QueueOutcomeStatus.MATCHED:
                await ctx.send(f"{SUCCESS_EMOJI} A match has been found, please check your DM for "
                               f"further information!", ephemeral=True)
            case QueueOutcomeStatus.BETA_QUEUED:
                await self._send_queue_district_picker(
                    ctx, outcome.selection,
                    f"{SUCCESS_EMOJI} For beta testing, you are added to the mixed queue for instant matches.",
                )
            case QueueOutcomeStatus.QUEUED:
                await self._send_queue_district_picker(
                    ctx, outcome.selection,
                    f"{SUCCESS_EMOJI} You are now added to the match-making queue. "
                    f"You will be informed via DM about your match in around 3 hours. "
                    f"If you would like to stop matching for an opponent, please run the command `/match dequeue`.",
                )
            case unreachable:
                assert_never(unreachable)

    async def _send_queue_district_picker(
            self, ctx: ipy.SlashContext | ipy.ComponentContext,
            selection: QueueDistrictSelection, content: str,
    ) -> None:
        user_id = str(ctx.author.id)
        async with QUEUE_OPERATION_LOCK:
            match = await find_attempt_match(user_id, selection.attempt_id)
            if match is not None:
                await ctx.send(f"{SUCCESS_EMOJI} A match has been found, please check your DM for "
                               f"further information!", ephemeral=True)
                return
            queue = await find_attempt_queue(user_id, selection.attempt_id, QUEUE_TIERS)
            if queue is None:
                await ctx.send(f"{ERROR_EMOJI} This queue entry is no longer active.", ephemeral=True)
                return
            selection = queue.district_selections[user_id]
            player = next(player for player in queue.players if str(player.user_id) == user_id)
            options = get_district_options(player.elo < 1200, player.capital_peak_enabled)
            description = "Optionally choose your districts before matching your opponent, but you will not get " \
                          "time to cancel once matched."
            if any(selection.districts):
                description += " Your district preset is loaded, feel free to update them if needed."
            embed = ipy.Embed(title="**Match-making Queue**", description=description, color=COLOR,
                              footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE))
            await ctx.send(content, embed=embed, components=queue_district_components(user_id, selection, options),
                           ephemeral=True)

    @ipy.component_callback(re.compile(r"^queued_(?:district|confirm|cancel)\|\d+\|\w+(?:\|[0-2])?$"))
    async def queued_district_action(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        action, user_id, attempt_id, *indexes = ctx.custom_id.split("|")
        if user_id != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return
        if action == "queued_district" and (not indexes or len(ctx.values) != 1):
            await ctx.send(f"{ERROR_EMOJI} That district is not available for this queue entry.", ephemeral=True)
            return
        cancel_token = None
        saved_state = "Any saved queue or match changes remain saved."
        try:
            async with QUEUE_OPERATION_LOCK:
                match = await find_attempt_match(user_id, attempt_id)
                if match is not None:
                    participant = next(p for p in match.participants if p.user_id == user_id)
                    if match.thread:
                        await ctx.message.edit(components=[], context=ctx)
                        await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
                        return
                    match action:
                        case "queued_cancel":
                            cancel_token = match.token
                        case "queued_district":
                            if participant.confirmed:
                                await ctx.message.edit(components=[], context=ctx)
                                await ctx.send(f"{ERROR_EMOJI} You have already confirmed this match.", ephemeral=True)
                                return
                            await self._select_match_district(ctx, match, indexes[0])
                        case "queued_confirm":
                            if not all(participant.districts):
                                await ctx.send(f"{ERROR_EMOJI} Please select all three districts.", ephemeral=True)
                                return
                            participant.queue_districts_locked = True
                            opponent = next(p for p in match.participants if p.user_id != user_id)
                            await self._confirm_match_participant(ctx, match, participant, opponent, match.token)
                            saved_state = "Your match confirmation is saved."
                            await ctx.message.edit(components=[], context=ctx)
                else:
                    queue = await find_attempt_queue(user_id, attempt_id, QUEUE_TIERS)
                    if queue is None:
                        await ctx.message.edit(components=[], context=ctx)
                        await ctx.send(f"{ERROR_EMOJI} This queue entry is no longer active.", ephemeral=True)
                        return
                    selection = queue.district_selections[user_id]
                    player = next(player for player in queue.players if str(player.user_id) == user_id)
                    options = get_district_options(player.elo < 1200, player.capital_peak_enabled)
                    match action:
                        case "queued_cancel":
                            for tier in QUEUE_TIERS:
                                current = await find_attempt_queue(user_id, attempt_id, [tier])
                                if current is not None:
                                    remove_player_from_queue(current, user_id)
                                    await current.save_changes()
                            saved_state = "You have been removed from the match-making queue."
                            await ctx.message.edit(components=[], context=ctx)
                            await ctx.send(f"{SUCCESS_EMOJI} You have been removed from the match-making queue.",
                                           ephemeral=True)
                        case "queued_district" | "queued_confirm":
                            if action == "queued_district":
                                index = int(indexes[0])
                                if len(ctx.values) != 1 or ctx.values[0] not in options[index]:
                                    await ctx.send(f"{ERROR_EMOJI} That district is not available for this queue entry.",
                                                   ephemeral=True)
                                    return
                                selection.districts[index] = ctx.values[0]
                                selection.confirmed = False
                                await queue.save_changes()
                                saved_state = "Your updated district choices are saved and need confirmation."
                                await ctx.send(f"{SUCCESS_EMOJI} You have selected **{ctx.values[0].lower()}**. "
                                               "Press Confirm again.",
                                               ephemeral=True)
                            else:
                                if any(district not in group for district, group in zip(selection.districts, options)):
                                    await ctx.send(f"{ERROR_EMOJI} Please select all three available districts.",
                                                   ephemeral=True)
                                    return
                                selection.confirmed = True
                                await queue.save_changes()
                                saved_state = "Your districts are saved."
                                await ctx.message.edit(
                                    components=queue_district_components(user_id, selection, options), context=ctx,
                                )
                                await ctx.send(f"{SUCCESS_EMOJI} Districts saved. "
                                               "You can change them until an opponent is found.",
                                               ephemeral=True)
        except PyMongoError:
            logging.exception("Queue district action failed for user %s, attempt %s", user_id, attempt_id)
            await ctx.send(f"{ERROR_EMOJI} The action could not be saved. Check your queue or match before retrying.",
                           ephemeral=True)
            return
        except ipy.errors.HTTPException:
            logging.exception("Queue district publication failed for user %s, attempt %s", user_id, attempt_id)
            await ctx.send(f"{ERROR_EMOJI} {saved_state} The display could not be updated.",
                           ephemeral=True)
            return
        if cancel_token is not None:
            await self._cancel_match(ctx, cancel_token)

    @ipy.Task.create(ipy.IntervalTrigger(hours=3))
    async def create_matches(self):
        async with QUEUE_OPERATION_LOCK:
            await run_queue_matching_trigger(self.bot)

        await ServerState.update_fields(last_matching=datetime.now().timestamp())

    @ipy.component_callback(re.compile(r"^district_select\|\w+\|\d+$"))
    async def district_select(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if match is not None and any(p.queue_attempt_id for p in match.participants):
            async with QUEUE_OPERATION_LOCK:
                match = await Match.find_one(Match.token == token)
                await self._select_match_district(ctx, match, index)
        else:
            await self._select_match_district(ctx, match, index)

    async def _select_match_district(self, ctx: ipy.ComponentContext, match: Match | None, index: str) -> None:
        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)

            return

        if match.thread:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} The match is already in process, is too late to select districts now.",
                           ephemeral=True)

            return

        district_index = int(index)
        district_options = get_district_options(
            match.wizard_valley_available,
            match.capital_peak_available,
        )
        selected_district = ctx.values[0]
        if district_index >= len(district_options) or selected_district not in district_options[district_index]:
            await ctx.send(f"{ERROR_EMOJI} That district is not available for this match.", ephemeral=True)
            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        if participant.queue_districts_locked:
            await ctx.send(f"{ERROR_EMOJI} Your districts are already locked for this match.", ephemeral=True)
            return
        participant.districts[district_index] = selected_district
        await match.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have selected **{selected_district.lower()}**", ephemeral=True)

    @ipy.component_callback(re.compile(r"^confirm_occupied_match\|\w+$"))
    async def confirm_occupied_match_button(self, ctx: ipy.ComponentContext):
        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send("Final districts are fixed. A manager can resume its thread with `/final round start`.", ephemeral=True)
            return

        await ctx.send_modal(build_district_confirmation_modal(
            token,
            match.wizard_valley_available,
            match.capital_peak_available,
        ))

    @ipy.modal_callback(re.compile(r"^confirm_occupied_match_modal\|\w+$"))
    async def confirm_occupied_match_modal(self, ctx: ipy.ModalContext, **response):
        async with QUEUE_OPERATION_LOCK:
            await self._confirm_occupied_match_modal(ctx, response)

    async def _confirm_occupied_match_modal(self, ctx: ipy.ModalContext, response) -> None:
        await ctx.defer(ephemeral=True)

        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send("Final districts are fixed. A manager can resume its thread with `/final round start`.", ephemeral=True)
            return

        selected_districts = [
            response[f"inline_district|{token}|{index}"][0]
            for index in range(3)
        ]
        district_options = get_district_options(
            match.wizard_valley_available,
            match.capital_peak_available,
        )
        if any(district not in options for district, options in zip(selected_districts, district_options)):
            await ctx.send(f"{ERROR_EMOJI} One or more districts are not available for this match.", ephemeral=True)
            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))
        participant.districts = selected_districts
        await self._confirm_match_participant(ctx, match, participant, opponent, token)

    @ipy.component_callback(re.compile(r"^match_confirm\|\w+$"))
    async def match_confirm_button(self, ctx: ipy.ComponentContext):
        async with QUEUE_OPERATION_LOCK:
            await self._match_confirm_button(ctx)

    async def _match_confirm_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, token = ctx.custom_id.split("|")
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is **canceled**.", ephemeral=True)

            return

        participant = next(p for p in match.participants if p.user_id == str(ctx.author.id))
        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))
        if not participant.districts[0]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 1st select menu.")

            return

        if not participant.districts[1]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 2nd select menu.")

            return

        if not participant.districts[2]:
            await ctx.send(f"{ERROR_EMOJI} Please select a district for the 3rd select menu.")

            return

        await self._confirm_match_participant(ctx, match, participant, opponent, token, update_message=True)

    async def _confirm_match_participant(
            self, ctx, match: Match, participant: Participant, opponent: Participant,
            token: str, update_message: bool = False):
        if participant.confirmed:
            await ctx.send(f"{ERROR_EMOJI} You have already confirmed this match.", ephemeral=True)
            return

        # confirm this participant and persist
        participant.confirmed = True
        if match.confirmation_tracking:
            participant.confirmed_at = datetime.now().timestamp()
            participant.confirmation_method = "manual"
        await match.save_changes()

        if update_message:
            submit_bases_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Submit Bases",
                emoji="📥",
                custom_id=f"submit_bases|{token}",
            )

            ctx.message.embeds[0].title = "**Match Confirmed**"
            ctx.message.embeds[0].description = (
                "If you wish to submit your own bases instead of using helpers' bases, "
                "please click on the \"Submit Bases\" button!"
            )
            guild = await member_guild(ctx)
            member = await guild.fetch_member(opponent.user_id)
            ctx.message.embeds[0].add_field(name="**Opponent**", value=mention_user(member, opponent.user_id))
            await ctx.message.edit(
                content="", components=submit_bases_button, embeds=match_message_embeds(ctx.message.embeds),
            )

        # if opponent not confirmed yet → wait
        if not all(p.confirmed for p in match.participants):
            await ctx.send(f"{SUCCESS_EMOJI} The match is **confirmed**, now waiting for your opponent to confirm!")
            return

        # both confirmed → create thread
        try:
            thread_id = await confirm_match(self.bot, token)
        except (ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError, MatchChannelUnavailable):
            logging.exception("Match %s confirmed but its thread publication is incomplete", token)
            await ctx.send(
                f"{IMPORTANT_EMOJI} The match is confirmed. Its thread publication is incomplete and will be retried.",
                ephemeral=True,
            )
            return
        await ctx.send(
            f"{SUCCESS_EMOJI} The match is **confirmed**, please determine the match "
            f"timing with your opponent in <#{thread_id}>",
            ephemeral=True,
        )

    @ipy.component_callback(re.compile(r"^delete_thread\|\w+$"))
    async def delete_thread_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        user_roles = {int(role.id) for role in ctx.author.roles}
        if not set(LVL2_ROLES).intersection(user_roles):
            await ctx.send(f"{ERROR_EMOJI} You are not allowed to delete the thread.", ephemeral=True)

            return

        cancel_button = ipy.Button(
            style=ipy.ButtonStyle.DANGER,
            label="Cancel",
            custom_id="cancel",
            emoji=CROSS_EMOJI
        )
        confirm_button = ipy.Button(
            style=ipy.ButtonStyle.SUCCESS,
            label="Confirm",
            custom_id="confirm",
            emoji=TICK_EMOJI
        )

        button_actionrow = ipy.ActionRow(cancel_button, confirm_button)

        msg = await ctx.send("Are you sure that you would like to **delete** the thread? If the match is not "
                             "finished, deleting it will invalidate the match.\n\n"
                             "*Note: you cannot undo this action.*",
                             components=button_actionrow, ephemeral=True)

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(components=button_actionrow, messages=int(msg.id), timeout=180)).ctx
        except asyncio.TimeoutError:
            await ctx.delete(msg)

            return

        if res.custom_id == "confirm":
            match = await Match.find_one(Match.thread == str(ctx.channel.id))
            lock_key = match.token if match else f"thread:{ctx.channel.id}"
            lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(lock_key, anyio.Lock())
            async with lock:
                match = await Match.find_one(Match.thread == str(ctx.channel.id))
                if match and has_pending_match_operation(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} A result or auto-win submission is unfinished. "
                        "Complete it before deleting this thread.", ephemeral=True,
                    )
                    return
                if match and has_reserved_match_slots(match):
                    await ctx.send(
                        f"{ERROR_EMOJI} This match still has reserved bases. "
                        "Finish the match or use Undo before deleting this thread.", ephemeral=True,
                    )
                    return
                await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

                await ctx.send(f"{LOADING_EMOJI} The thread will be deleted in 5 seconds.", ephemeral=True)
                await asyncio.sleep(5)

                async with BASE_STORE_RESERVATION_LOCK:
                    match = await Match.find_one(Match.thread == str(ctx.channel.id))
                    if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                        await ctx.send(
                            f"{ERROR_EMOJI} This match now has reserved bases or an unfinished submission. "
                            "Finish the match, or use Undo for its reservation, before deleting this thread.",
                            ephemeral=True,
                        )
                        return
                    await ctx.channel.delete(reason=f"Deleted by: {ctx.author} ({ctx.author.id})")
                    if match and not await delete_unreserved_match(match):
                        logging.warning("Match %s changed before deleted-thread cleanup could finish", match.token)
                        return
                if match:
                    await free_match_clans(match)
                    for participant in match.participants:
                        await remove_helper_task(match.thread, participant.user_id)

        if res.custom_id == "cancel":
            await res.edit_origin(components=ipy.utils.misc_utils.disable_components(button_actionrow))

            await ctx.send(f"{SUCCESS_EMOJI} The action has been canceled.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^reveal_opponent\|\w+$"))
    async def reveal_opponent_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.",
                           ephemeral=True)

            return

        _, token = ctx.custom_id.split("|", 1)
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match has been canceled.", ephemeral=True)

            return

        opponent = next(p for p in match.participants if p.user_id != str(ctx.author.id))

        submit_bases_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Submit Bases",
            emoji="📥",
            custom_id=f"submit_bases|{token}",
        )

        guild = await member_guild(ctx)
        member = await guild.fetch_member(opponent.user_id)
        ctx.message.embeds[0].add_field(name="**Opponent**", value=mention_user(member, opponent.user_id))
        await ctx.message.edit(components=submit_bases_button, embeds=match_message_embeds(ctx.message.embeds))

        await ctx.send(f"{SUCCESS_EMOJI} Your opponent is now revealed, and you cannot cancel the match anymore.",
                       ephemeral=True)

    @ipy.component_callback(re.compile(r"^submit_bases\|\w+(?:\|[01]\|\d+\|\d+)?$"))
    async def submit_bases_button(self, ctx: ipy.ComponentContext):
        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not ctx.message.contains_mention(ctx.author):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        parts = ctx.custom_id.split("|")
        token = parts[1]
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} It seems that this match has been canceled.", ephemeral=True)
            return

        if not match.base_submission:
            await ctx.send(f"{ERROR_EMOJI} Base submission is not enabled for this match.", ephemeral=True)
            return

        # figure out which side the author is on
        author_id = str(ctx.author.id)
        if not match.participants or len(match.participants) < 2:
            await ctx.send(f"{ERROR_EMOJI} Invalid match data.", ephemeral=True)
            return

        if is_final_match(match) and (
            author_id not in [participant.user_id for participant in match.participants]
            or ctx.guild_id != match.routing_guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE)
            or match.final_superseded or match.final_result_id is not None
            or len(parts) > 2 and (parts[3] != author_id or int(parts[4]) != match.final_base_page_revision)
        ):
            await ctx.send(f"{ERROR_EMOJI} This base view is unavailable or outdated. Open the current match's controls.",
                           ephemeral=True)
            return

        if is_final_match(match):
            await ctx.send(f"Use `/match base submit match:{match.final_node_id} base_link:<link>` for each base.",
                           ephemeral=True)
            return

        if author_id == match.participants[0].user_id:
            player_index, opp_index = 0, 1
        else:
            player_index, opp_index = 1, 0

        # 10-minute cutoff:
        # - non-war: shared scheduled time (we store in times[0]/times[1])
        # - war:     opponent's scheduled time at their index
        now = int(datetime.now().timestamp())
        times = match.times

        # pick the "non-war" scheduled time (either side; earlier code sets both equal)
        non_war_ts = times[0]
        war_ts = times[opp_index]

        too_late = (
                (not match.is_war and non_war_ts is not None and (non_war_ts - now) < 600) or
                (match.is_war and war_ts is not None and (war_ts - now) < 600)
        )
        if too_late:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(
                f"{ERROR_EMOJI} It is too late to submit now, you can only submit bases 10 minutes before the match.",
                ephemeral=True
            )
            return

        # require opponent to confirm districts first
        # (in this model, final_districts must be fully set)
        if not all(match.final_districts):
            await ctx.send(
                f"{ERROR_EMOJI} Must wait for your opponent to confirm districts first. "
                f"However, we will let the helpers know that you wish to submit bases!",
                ephemeral=True
            )
            # mark intent
            match.participants[player_index].submit_bases = True
            await match.save_changes()
            return

        participant = match.participants[player_index]
        modal = ipy.Modal(title="Base Submission", custom_id=f"submit_bases_modal|{token}")
        for i, district in enumerate(match.final_districts):
            base_data = participant.bases[i]
            if base_data and await BaseLayout.find_one(
                BaseLayout.link == base_data.link,
                BaseLayout.quality != "Private",
            ):
                continue
            modal.add_components(
                ipy.ParagraphText(
                    label=district,
                    value=(base_data.link if base_data else None),
                    placeholder="Put the base link here and make sure it is not expired",
                    max_length=200,
                    custom_id=district,
                    required=False
                )
            )

        if not modal.components:
            await ctx.send(
                f"{ERROR_EMOJI} All your bases are helper bases, so there are no bases you can submit here.",
                ephemeral=True,
            )
            return

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^submit_bases_modal\|\w+(?:\|[01]\|\d+\|\d+)?$"))
    async def submit_bases_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        parts = ctx.custom_id.split("|")
        token = parts[1]

        # load match and base data
        match: Match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Match not found or canceled.", ephemeral=True)
            return

        if is_final_match(match):
            await ctx.send(f"Use `/match base submit match:{match.final_node_id} base_link:<link>` for each base.",
                           ephemeral=True)
            return

        author_id = str(ctx.author.id)
        participant = next((p for p in match.participants if p.user_id == author_id), None)
        if not participant:
            await ctx.send(f"{ERROR_EMOJI} You are not part of this match.", ephemeral=True)
            return

        # validate and update submitted bases
        for index, expected_district in enumerate(match.final_districts):
            if expected_district not in response:
                continue
            base_link = response[expected_district]
            recorded_base = participant.bases[index]
            if recorded_base and recorded_base.link == base_link:
                continue
            if not base_link:
                participant.bases[index] = None
                continue

            # validation checks
            if not validators.url(base_link):
                await ctx.send(f"{ERROR_EMOJI} Invalid URL for the {ORDINAL_REPRESENTATIONS[index]} base.",
                               ephemeral=True)
                return

            if (not base_link.startswith("https://link.clashofclans.com")
                    or "OpenLayout&id" not in base_link.split("=")
                    or base_link.split("%")[1] != "3ACC"):
                await ctx.send(f"{ERROR_EMOJI} The {ORDINAL_REPRESENTATIONS[index]} base link does not look like "
                               f"a Clash of Clans link.", ephemeral=True)
                return

            district = DISTRICT_CODES_REVERSED.get(base_link.split('%')[2])
            if district != expected_district:
                await ctx.send(f"{ERROR_EMOJI} Expected district for the {ORDINAL_REPRESENTATIONS[index]} base is "
                               f"**{expected_district}**, but your link is for **{district}**.", ephemeral=True)
                return

            helper_base = await BaseLayout.find_one(
                BaseLayout.link == base_link,
                BaseLayout.quality != "Private",
            )
            if helper_base:
                await ctx.send(
                    f"{ERROR_EMOJI} The submitted base for **{expected_district}** is a helper's base. "
                    f"You may still use it, but please generate a different link.",
                    ephemeral=True,
                )
                return

            # save base
            participant.bases[index] = BaseLink(link=base_link, image=UNAVAILABLE_IMAGE)

        # persist match update
        await match.save_changes()

        # update pinned match embed
        thread = await self.bot.fetch_channel(match.thread)
        match_msg = await find_match_thread_announcement(thread, match, self.bot.user.id)
        if match_msg is None:
            await ctx.send(
                f"{ERROR_EMOJI} Your bases were saved, but the match message could not be updated. "
                "Please ask a host to notify the helpers.",
                ephemeral=True,
            )
            return
        if not match_msg.pinned:
            await match_msg.pin()

        embed = match_msg.embeds[0]
        note = f"- {mention_user(ctx.author)} has submitted bases."
        if len(embed.fields) < 5:
            embed.add_field(name="**Notes**", value=note)
        elif not re.search(
                rf"(?:<@!?{ctx.author.id}>|users/{ctx.author.id}>\)(?:\*\*)?) has submitted bases\.",
                embed.fields[4].value,
        ):
            embed.fields[4].value += f"\n{note}"

        await match_msg.edit(embeds=match_message_embeds(match_msg.embeds))

        await ctx.send(f"{SUCCESS_EMOJI} Your bases are submitted!", components=[], ephemeral=True)

    @ipy.component_callback(re.compile(r"^match_cancel\|\w+$"))
    async def match_cancel_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        if ctx.channel and int(ctx.channel.id) == COMMAND_CHANNEL and not str(ctx.author.id) in ctx.message.content:
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with other user's components.", ephemeral=True)
            return

        _, token = ctx.custom_id.split("|")

        await self._cancel_match(ctx, token)

    async def _cancel_match(self, ctx: ipy.ComponentContext, token: str) -> None:
        # DB/JSON fetches
        match = await Match.find_one(Match.token == token)
        
        author_id = str(ctx.author.id)
        author_player = await Player.find_one(Player.user_id == author_id)

        # cooldown: once per 7 days
        last_cancel_ts = author_player.last_cancel if author_player else None
        if last_cancel_ts:
            last_cancel = datetime.fromtimestamp(last_cancel_ts)
            if (datetime.now() - last_cancel).days <= 7:
                await ctx.send(f"{ERROR_EMOJI} You can only cancel a match once every week.", ephemeral=True)
                return

        # match must exist and not have started
        if not match:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components), context=ctx)
            await ctx.send(f"{ERROR_EMOJI} It seems that this match is invalidated.", ephemeral=True)
            return

        if match.thread:
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return

        # confirm/cancel UI
        cancel_btn = ipy.Button(style=ipy.ButtonStyle.DANGER, label="Cancel", custom_id="cancel", emoji=CROSS_EMOJI)
        confirm_btn = ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Confirm", custom_id="confirm", emoji=TICK_EMOJI)
        row = ipy.ActionRow(cancel_btn, confirm_btn)

        msg = await ctx.send(
            "Are you sure that you would like to **cancel** the match? "
            "You may only perform this action once in a week.\n\n"
            "*Note: you cannot undo this action.*",
            components=row, ephemeral=True
        )

        try:
            res: ipy.ComponentContext = (
                await self.bot.wait_for_component(components=row, messages=int(msg.id), timeout=180)
            ).ctx
        except asyncio.TimeoutError:
            await ctx.delete(ctx.message)
            return

        if res.custom_id == "confirm":
            await res.defer(ephemeral=True)
            async with QUEUE_OPERATION_LOCK:
                current_match = await Match.find_one(Match.token == token)
                deleted = False
                if current_match is not None and current_match.id == match.id:
                    await remove_matched_queue_attempts(match)
                    deleted = await delete_unreserved_match(match, pre_thread=True)
            if not deleted:
                await res.send(
                    f"{ERROR_EMOJI} The match changed while cancellation was being confirmed. It was not canceled.",
                    ephemeral=True,
                )
                return

            # figure out players (assumes exactly two participants)
            p1_id = match.participants[0].user_id if match.participants else None
            p2_id = match.participants[1].user_id if len(match.participants) > 1 else None

            forfeited_id = author_id
            innocent_id = p2_id if p1_id == forfeited_id else p1_id

            try:
                if innocent_id:
                    innocent_player = await Player.find_one(Player.user_id == innocent_id)
                    if innocent_player:
                        innocent_player.priority = True
                        await innocent_player.save_changes()

                if author_player:
                    author_player.last_cancel = datetime.now().timestamp()
                    await author_player.save_changes()
            except PyMongoError:
                logging.exception("Canceled match %s has incomplete player bookkeeping", token)
                await res.send(
                    f"{ERROR_EMOJI} The match was removed, but saving cancellation cooldown or opponent priority failed. "
                    "Please contact staff.", ephemeral=True,
                )
                return

            # DM notifications (best-effort)
            forfeited_user = await self.bot.fetch_user(forfeited_id)
            innocent_user = await self.bot.fetch_user(innocent_id)

            if innocent_user:
                try:
                    await innocent_user.send(
                        f"{IMPORTANT_EMOJI} Unfortunately, "
                        f"{forfeited_user.mention if forfeited_user else 'the opponent'} "
                        f"has decided to **cancel** the match against you. Please find a new match. "
                        f"If this happens repeatedly, please report it to staff!"
                    )
                except ipy.errors.Forbidden:
                    pass

            if forfeited_user:
                try:
                    await forfeited_user.send(
                        f"{IMPORTANT_EMOJI} The match is **canceled**. "
                        f"Next time please make sure your schedule is free when finding a match!"
                    )
                except ipy.errors.Forbidden:
                    pass

            # disable buttons in both places
            for actionrow in ctx.message.components:
                for component in actionrow.components:
                    component.disabled = True
            await ctx.message.edit(components=ctx.message.components, context=ctx)
            await ctx.edit(msg.id, components=utils.misc_utils.disable_components(row))

            await res.send(
                f"{SUCCESS_EMOJI} Match successfully canceled. "
                f"You may only cancel once per week—please ensure your schedule is clear next time."
            )

        if res.custom_id == "cancel":
            await ctx.edit(msg.id, components=utils.misc_utils.disable_components(row))
            await res.send(f"{SUCCESS_EMOJI} The action has been canceled.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^submit_button\|\w+$"))
    async def result_submit_button(self, ctx: ipy.ComponentContext):
        if (not ctx.author.has_role(STAFF_ROLE) and not ctx.author.has_role(SEN_ROLE) and
                not ctx.author.has_role(MODERATOR_ROLE)):
            await ctx.send(f"{ERROR_EMOJI} Only staffs are allowed to submit results.", ephemeral=True)
            return

        _, token = ctx.custom_id.split("|")

        match: Match | None = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread.",
                           ephemeral=True)
            return

        if is_final_match(match):
            from common.finals.result_ui import show_source_result_form
            from common.finals.types import FinalError
            try:
                await show_source_result_form(ctx, match)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        # prevent staff who are also players (unless they're the bot owner)
        participant_ids = [p.user_id for p in match.participants]
        if str(ctx.author.id) in participant_ids and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot submit results as one of the players.", ephemeral=True)
            return

        if "🔴" in ctx.channel.name:
            await ctx.send(f"{ERROR_EMOJI} The match has already ended, you cannot submit again.", ephemeral=True)
            try:
                for actionrow in ctx.message.components:
                    for component in actionrow.components:
                        if component.custom_id.startswith("submit_button"):
                            component.disabled = True
                            break
                await ctx.message.edit(components=ctx.message.components)
            except ipy.errors.HTTPException:
                pass
            return

        p1, p2 = match.participants[0], match.participants[1]
        player1 = await self.bot.fetch_user(p1.user_id)
        player2 = await self.bot.fetch_user(p2.user_id)

        # pre-fill results if all three entries exist (same behavior as before)
        value1 = "\n".join(p1.result) if p1.result and all(p1.result) else None
        value2 = "\n".join(p2.result) if p2.result and all(p2.result) else None

        modal = ipy.Modal(
            ipy.ParagraphText(
                label=f"{player1}'s Result",
                placeholder="Format: [stars].[destruction]\n"
                            "E.g, 2 stars 83% -> 2.83\n"
                            "Separate results with line breaks",
                max_length=100,
                value=value1,
                custom_id="player1"
            ),
            ipy.ParagraphText(
                label=f"{player2}'s Result",
                placeholder="Format: [stars].[destruction]\n"
                            "E.g, 2 stars 83% -> 2.83\n"
                            "Separate results with line breaks",
                max_length=100,
                value=value2,
                custom_id="player2"
            ),
            title="Result Submission",
            custom_id=f"result_modal|{token}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^result_modal\|\w+$"))
    async def result_submit_modal(self, ctx: ipy.ModalContext, **response):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of the first result is invalid.", ephemeral=True)

            return

        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of the second result is invalid.", ephemeral=True)

            return

        await self._process_match_results(ctx, response, data_generation=data_generation)

    async def _process_match_results(
            self, ctx: ipy.ModalContext, response: dict, *, data_generation: int | None = None,
    ):
        _, token = ctx.custom_id.split("|")

        return await self._finalize_match_results(token, response=response, ctx=ctx, data_generation=data_generation)

    async def _finalize_match_results(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
            *,
            data_generation: int | None = None,
    ) -> bool:
        return await result_store.finalize_match_results(
            self,
            result_store.FinalizeMatchResultsRequest(token=token, response=response, ctx=ctx,
                                                    data_generation=data_generation),
        )

    async def _finalize_match_results_locked(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
    ) -> bool:
        match = await Match.find_one(Match.token == token)
        if match is None or not match.thread:
            return await self._publish_match_results_locked(token, response, ctx)
        async with match_message_lock(match.thread):
            return await self._publish_match_results_locked(token, response, ctx)

    async def _publish_match_results_locked(
            self,
            token: str,
            response: dict[str, str] | None = None,
            ctx: ipy.BaseContext | None = None,
    ) -> bool:
        async def send_interaction_message(message: str) -> None:
            if ctx is not None:
                await ctx.send(message, ephemeral=True)

        mongo_match: Match | None = await Match.find_one(Match.token == token)
        if mongo_match is not None and is_final_match(mongo_match):
            from beanie import PydanticObjectId
            from database_models.season_models import EloFinal
            from common.finals.results import FinalResultSubmission, automatic_operation_key, submit_result
            from common.finals.types import FinalError
            from pydantic import ValidationError
            try:
                final = await EloFinal.get(PydanticObjectId(mongo_match.final_id))
                if final is None:
                    raise FinalError("The Final is missing. A manager must recover this match.")
                node = final.nodes[mongo_match.final_node_id]
                scores = {participant.user_id: (response[f"player{side + 1}"].splitlines() if response
                                               else list(participant.result))
                          for side, participant in enumerate(mongo_match.participants)}
                result = await submit_result(FinalResultSubmission(
                    final_id=final.id, node_id=node.id, actor_id=str(self.bot.user.id),
                    operation_key=automatic_operation_key(mongo_match, scores), expected_version=node.result_version,
                    contestant_revision=mongo_match.final_contestant_revision, attempt=mongo_match.final_attempt,
                    scores=scores,
                ))
                from common.finals.recovery import recover_final
                async def release_final(source):
                    await clear_match_base_stores(source, reset_stored_flags=True, bot=self.bot, reason="finished")
                    await free_match_clans(source)
            except (FinalError, ValidationError) as error:
                await send_interaction_message(f"{ERROR_EMOJI} " + (str(error) if isinstance(error, FinalError)
                    else "Both players need seven valid stars.destruction results."))
                return False
            try:
                pending = await recover_final(final, bot=self.bot, release_completed=release_final)
            except (FinalError, PyMongoError) as error:
                pending = [f"Recovery is pending: {error}. Use `/final recover`."]
            await send_interaction_message("Final result saved." + (" Updates remain pending: " + " ".join(pending) if pending else ""))
            return True
        if not mongo_match or not mongo_match.thread or len(mongo_match.participants) < 2:
            await send_interaction_message(
                f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread."
            )
            return False

        if mongo_match.result_operation and mongo_match.result_operation.cancelled:
            await send_interaction_message(f"{ERROR_EMOJI} {result_input_error(mongo_match)}")
            return False
        if token in self._pending_match_thread_renames:
            await send_interaction_message(
                f"{ERROR_EMOJI} The match has already ended, you cannot submit again."
            )
            return False

        match_thread = await self.bot.fetch_channel(int(mongo_match.thread))
        if (mongo_match.result_operation and mongo_match.result_operation.published) or (
                "🔴" in match_thread.name and not mongo_match.result_operation
        ):
            await send_interaction_message(
                f"{ERROR_EMOJI} The match has already ended, you cannot submit again."
            )
            return False

        p1 = mongo_match.participants[0]
        p2 = mongo_match.participants[1]
        player1_id = p1.user_id
        player2_id = p2.user_id
        player_ids = [player1_id, player2_id]

        if response is None:
            if any(
                    len(participant.result) != 3
                    or not all(isinstance(result, str) for result in participant.result)
                    for participant in (p1, p2)
            ):
                await send_interaction_message(
                    f"{ERROR_EMOJI} Both players must have complete valid results before the match can be submitted."
                )
                return False
            response = {
                "player1": "\n".join(p1.result),
                "player2": "\n".join(p2.result),
            }

        if not all(is_valid_complete_match_result(response[key]) for key in ("player1", "player2")):
            await send_interaction_message(
                f"{ERROR_EMOJI} Both players must have complete valid results before the match can be submitted."
            )
            return False

        try:
            await begin_result_operation(mongo_match, response)
        except ResultOperationConflict as error:
            await send_interaction_message(f"{ERROR_EMOJI} {error.message}")
            return False
        guild = await self.bot.fetch_guild(MAIN_GUILD)
        if mongo_match.result_operation.completed:
            player1_elo_change_str, player2_elo_change_str = await completed_result_changes(mongo_match)
            await invite_eligible_players(self.bot, (player1_id, player2_id), pending_only=True)
        else:
            # prepare commonly used fields
            final_districts = mongo_match.final_districts

            # record who hosted - using Helper model
            is_war = mongo_match.is_war
            hosts = (mongo_match.hosts or [None, None])
            times = (mongo_match.times or [None, None])
            single_host = hosts[0] if hosts else None
            single_time = int(mongo_match.matched_time or times[0] or 0)

            if not is_war and single_time:
                if single_host:
                    history_entry = HelperHistory(
                        time=single_time, player1=player1_id, player2=player2_id
                    )
                    await award_helper_once(single_host, f"result:{mongo_match.id}:host:live", 3, history_entry)
            else:
                for i in range(2):
                    host_id = hosts[i] if i < len(hosts) else None
                    host_time = times[i] if i < len(times) else None
                    if host_id and host_time:
                        history_entry = HelperHistory(
                            time=int(host_time), player1=player_ids[i], player2=None
                        )
                        await award_helper_once(host_id, f"result:{mongo_match.id}:host:{i}", 0.5, history_entry)

                    submitters = (mongo_match.submitters or [0, 0])
                    submitter_id = submitters[i] if i < len(submitters) else None
                    if submitter_id:
                        await award_helper_once(submitter_id, f"result:{mongo_match.id}:submitter:{i}", 0.5)

            # record the performance of the bases
            player_results = {
                player1_id: response["player1"].split("\n"),
                player2_id: response["player2"].split("\n"),
            }

            # record the base stats
            await record_base_stats(player_results, mongo_match)

            # record the result (unchanged)
            player1_elo_change_str, player2_elo_change_str = await record_match(
                guild, self.bot, player1_id, player2_id, response, final_districts, token
            )
            mongo_match = await Match.find_one(Match.token == token)
            await complete_result_operation(mongo_match)
            await invite_eligible_players(
                self.bot, (player1_id, player2_id),
                pending_only=mongo_match.result_operation.previous_record is not None,
            )

        await forget_replaced_match_choice(self.bot, mongo_match)
        await clear_match_base_stores(mongo_match, reset_stored_flags=True, bot=self.bot, reason="finished")
        await free_match_clans(mongo_match)

        interaction_message = getattr(ctx, "message", None) if ctx is not None else None
        if interaction_message is not None:
            try:
                for actionrow in interaction_message.components:
                    for component in actionrow.components:
                        if component.custom_id.startswith("submit_button"):
                            component.disabled = True
                            break
                await edit_channel_message(interaction_message, components=interaction_message.components)
            except ipy.errors.HTTPException:
                pass

        recorded_match = mongo_match.result_operation.record

        # build summary text from recorded_match
        match_info = ""
        p1_stats = recorded_match.players[player1_id]
        p2_stats = recorded_match.players[player2_id]
        
        district_keys = list(p1_stats.districts.keys())
        for district in district_keys:
            match_info += (
                f"{DISTRICT_EMOJI} **{district}**\n"
                f"{p1_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p1_stats.districts[district].destruction}% - "
                f"{p2_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p2_stats.districts[district].destruction}%\n\n"
            )

        match_info += (
            f"**Final Score**\n"
            f"{p1_stats.stars}{STAR_EMOJI} {p1_stats.destruction}% - "
            f"{p2_stats.stars}{STAR_EMOJI} {p2_stats.destruction}%"
        )

        members = await asyncio.gather(guild.fetch_member(player1_id), guild.fetch_member(player2_id))
        finished_embed = ipy.Embed(
            title="**Match has Ended**",
            description=(
                f"{mention_user(members[0], player1_id)} ({player1_elo_change_str}) vs. "
                f"{mention_user(members[1], player2_id)} ({player2_elo_change_str})\n\n{match_info}"
            ),
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        try:
            await self._publish_result_embeds_locked(mongo_match, match_thread, finished_embed)

            for old_msg_id in dict.fromkeys(mongo_match.old_msgs):
                if old_msg_id:
                    try:
                        old_msg = await match_thread.fetch_message(old_msg_id)
                        if old_msg:
                            await mark_match_message_ended(old_msg)
                    except ipy.errors.NotFound:
                        pass
        except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
            logging.exception("Result publication incomplete for match %s", token)
            await send_interaction_message(
                f"{IMPORTANT_EMOJI} The results are recorded, but the match messages could not all be updated. "
                "Publication is incomplete and will be retried."
            )
            return False

        await remove_helper_task(mongo_match.thread, mongo_match.participants[0].user_id)
        await remove_helper_task(mongo_match.thread, mongo_match.participants[1].user_id)

        await fetch_user_match_id_choices(self.bot, guild, player1_id)
        await fetch_user_match_id_choices(self.bot, guild, player2_id)
        await autoqueue_completed_match_participants(self.bot, token, (player1_id, player2_id))
        await publish_result_operation(mongo_match)
        rename_task = schedule_channel_name_edit(match_thread, "🔴")
        self._pending_match_thread_renames[token] = rename_task
        rename_task.add_done_callback(lambda _: self._pending_match_thread_renames.pop(token, None))
        await send_interaction_message(
            f"{SUCCESS_EMOJI} The match results are submitted and recorded.\n\n"
            f"{IMPORTANT_EMOJI} *Please kick the players out of the clan!*"
        )
        return True

    @base.subcommand(sub_cmd_name="submit", sub_cmd_description="Submit a match result manually")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @ipy.slash_option(
        name="player1",
        description="Player 1 of the match",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player2",
        description="Player 2 of the match",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="player1_result",
        description="Player 1's result",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="player2_result",
        description="Player 2's result",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="district1",
        description="First district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Barbarian Camp", value="Barbarian Camp"),
            ipy.SlashCommandChoice(name="Skeleton Park", value="Skeleton Park"),
            ipy.SlashCommandChoice(name="Wizard Valley", value="Wizard Valley"),
        ],
        required=True
    )
    @ipy.slash_option(
        name="district2",
        description="Second district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Balloon Lagoon", value="Balloon Lagoon"),
            ipy.SlashCommandChoice(name="Builder's Workshop", value="Builder's Workshop"),
            ipy.SlashCommandChoice(name="Golem Quarry", value="Golem Quarry")
        ],
        required=True
    )
    @ipy.slash_option(
        name="district3",
        description="Third district assigned",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Dragon Cliffs", value="Dragon Cliffs"),
            ipy.SlashCommandChoice(name="Goblin Mines", value="Goblin Mines"),
            ipy.SlashCommandChoice(name="Capital Peak", value="Capital Peak"),
        ],
        required=True
    )
    async def match_submit(self, ctx: ipy.SlashContext, player1: ipy.User, player2: ipy.User, player1_result: str,
                           player2_result: str, district1: str, district2: str, district3: str):
        if getattr(getattr(ctx.channel, "parent_channel", None), "id", None) == FINAL_CHANNEL:
            await ctx.send(f"{ERROR_EMOJI} Use Submit Result or `/final match result` for all seven Final districts.",
                           ephemeral=True)
            return
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        player_docs: list[Player] = []
        for player in [player1, player2]:
            player_doc = await Player.find_one(Player.user_id == str(player.id))
            if not player_doc:
                await ctx.send(f"{ERROR_EMOJI} {player.mention} is not registered in the elo system.",
                               ephemeral=True)

                return
            player_docs.append(player_doc)

        player1 = str(player1.id)
        player2 = str(player2.id)
        response = {
            "player1": player1_result.replace("/", "\n"),
            "player2": player2_result.replace("/", "\n"),
        }
        districts = [district1, district2, district3]
        district_options = get_district_options(
            all(player.elo < 1200 for player in player_docs),
            all(player.capital_peak_enabled for player in player_docs),
        )
        if any(district not in options for district, options in zip(districts, district_options)):
            await ctx.send(f"{ERROR_EMOJI} One or more districts are not available for these players.", ephemeral=True)
            return

        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player1's result is invalid.", ephemeral=True)

            return

        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player2's result is invalid.", ephemeral=True)

            return

        key = manual_result_key(str(ctx.author.id), [player1, player2], response, districts)
        lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(key, anyio.Lock())
        async with lock:
            try:
                async with PLAYER_DATA_BOUNDARY.write(data_generation):
                    try:
                        await record_match(ctx.guild, self.bot, player1, player2, response, districts, manual_key=key)
                    except ManualResultConfirmationUnknown as error:
                        logging.exception("Unable to confirm a completed manual result")
                        await ctx.send(
                            f"{ERROR_EMOJI} The match data was recorded as ID `{error.record_id}`, but I could not "
                            "confirm its final status. Check that record before submitting another match.",
                            ephemeral=True,
                        )
                        return
                    except ResultOperationConflict as error:
                        await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                        return
                    except (PyMongoError, ipy.errors.HTTPException) as error:
                        logging.exception("Manual result submission did not finish: %s", type(error).__name__)
                        await ctx.send(
                            f"{ERROR_EMOJI} The submission did not finish. Retry with the same players, scores, "
                            "and districts to continue it safely.",
                            ephemeral=True,
                        )
                        return
                    await ctx.send(
                        f"{SUCCESS_EMOJI} The match between <@{player1}> and <@{player2}> is submitted.",
                        ephemeral=True,
                    )
            except PlayerDataReset as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @base.subcommand(sub_cmd_name="resubmit", sub_cmd_description="Resubmit a match result")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    async def match_resubmit(self, ctx: ipy.SlashContext):
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True,
            )
            return

        if is_final_match(match):
            from common.finals.result_ui import show_source_result_form
            from common.finals.types import FinalError
            try:
                await show_source_result_form(ctx, match, correction=True)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return

        if "🔴" not in ctx.channel.name:
            await ctx.send(f"{ERROR_EMOJI} The match is not yet finished, so why are you using this command?",
                           ephemeral=True)

            return

        if match.is_autowin:
            await ctx.send(f"{ERROR_EMOJI} An auto-win has already been issued to this match!",
                           ephemeral=True)

            return

        if str(ctx.author.id) in match and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)

            return

        p1_id = match.participants[0].user_id
        p2_id = match.participants[1].user_id
        player1 = await self.bot.fetch_user(p1_id)
        player2 = await self.bot.fetch_user(p2_id)

        value1 = "\n".join(match.participants[0].result) if all(match.participants[0].result) else None
        value2 = "\n".join(match.participants[1].result) if all(match.participants[1].result) else None

        modal = ipy.Modal(
            ipy.ParagraphText(
                label=f"{player1}'s Result",
                max_length=100,
                value=value1,
                custom_id="player1"
            ),
            ipy.ParagraphText(
                label=f"{player2}'s Result",
                max_length=100,
                value=value2,
                custom_id="player2"
            ),
            title="Result Submission",
            custom_id=f"resubmit_modal|{match.token}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^resubmit_modal\|\w+$"))
    async def resubmit_modal(self, ctx: ipy.ModalContext, **response):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        _, token = ctx.custom_id.split("|")
        lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(token, anyio.Lock())
        try:
            async with lock, PLAYER_DATA_BOUNDARY.write(data_generation):
                await self._resubmit_modal_locked(ctx, token, response)
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    async def _resubmit_modal_locked(self, ctx: ipy.ModalContext, token: str, response: dict[str, str]) -> None:
        match = await Match.find_one(Match.token == token)
        if not match:
            await ctx.send(f"{ERROR_EMOJI} It seems like the match is invalid, you may delete the thread.",
                           ephemeral=True)
            return
        if is_final_match(match):
            await ctx.send(f"{ERROR_EMOJI} Open `/match resubmit` again to use the current Final correction form.",
                           ephemeral=True)
            return
        if match.result_operation and match.result_operation.cancelled:
            await ctx.send(f"{ERROR_EMOJI} {result_input_error(match)}", ephemeral=True)
            return

        # validate formats
        if not is_valid_complete_match_result(response["player1"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player1's result is invalid.", ephemeral=True)
            return
        if not is_valid_complete_match_result(response["player2"]):
            await ctx.send(f"{ERROR_EMOJI} The formatting of player2's result is invalid.", ephemeral=True)
            return

        # DB/JSON fetches
        p1, p2 = match.participants[0], match.participants[1]
        player1_id, player2_id = p1.user_id, p2.user_id
        
        player1_doc = await Player.find_one(Player.user_id == player1_id, fetch_links=True)
        player2_doc = await Player.find_one(Player.user_id == player2_id, fetch_links=True)
        
        # Find previously recorded match
        previous_match = None
        if match.result_operation and not match.result_operation.completed and match.result_operation.previous_record:
            previous_match = RecordedMatch.model_validate(match.result_operation.previous_record.model_dump())
        elif match.recorded_match_id:
            previous_match = await RecordedMatch.find_one(RecordedMatch.id == match.recorded_match_id)
        if not previous_match and player1_doc and player1_doc.matches:
            # Check recent matches (last 5)
            sorted_matches = sorted(player1_doc.matches, key=lambda m: int(m.id))
            for m in reversed(sorted_matches[-5:]):
                # Check if opponent matches
                if player2_id not in m.players:
                    continue
                
                # Check districts
                p1_stats = m.players[player1_id]
                if list(p1_stats.districts.keys()) != match.final_districts:
                    continue
                
                # Check results (stars/destruction) against the OLD results in match.participants
                # p1.result is list of strings "stars.destruction"
                if not p1.result or not p2.result:
                    continue
                    
                p1_res_stars = sum(map(get_stars, p1.result))
                p1_res_dest = sum(map(get_destruction, p1.result))
                
                if p1_stats.stars != p1_res_stars or p1_stats.destruction != p1_res_dest:
                    continue
                    
                # If we are here, it's likely the match
                previous_match = m
                break
        
        if not previous_match:
            await ctx.send(f"{ERROR_EMOJI} Could not find the previous match record to revert.", ephemeral=True)
            return

        try:
            await begin_result_operation(match, response, previous_match)
        except ResultOperationConflict as error:
            await ctx.send(f"{ERROR_EMOJI} {error.message}", ephemeral=True)
            return
        if match.result_operation.completed:
            player1_elo_change_str, player2_elo_change_str = await completed_result_changes(match)
        else:
            await rollback_result_operation(match)
            player1_elo_change_str, player2_elo_change_str = await record_match(
                ctx.guild, self.bot, player1_id, player2_id, response, match.final_districts, token
            )
            match = await Match.find_one(Match.token == token)
            p1, p2 = match.participants[:2]
            player_results = {
                player1_id: p1.result,
                player2_id: p2.result,
            }
            await record_base_stats(player_results, match)
            await complete_result_operation(match)

        await invite_eligible_players(self.bot, (player1_id, player2_id), pending_only=True)
        await forget_replaced_match_choice(self.bot, match)
        await clear_match_base_stores(match, reset_stored_flags=True, bot=self.bot, reason="finished")
        await free_match_clans(match)

        # update match result embed
        new_match = match.result_operation.record
        
        match_info = ""
        p1_new_stats = new_match.players[player1_id]
        p2_new_stats = new_match.players[player2_id]
        
        district_keys = list(p1_new_stats.districts.keys())
        for district in district_keys:
            match_info += (
                f"{DISTRICT_EMOJI} **{district}**\n"
                f"{p1_new_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p1_new_stats.districts[district].destruction}% - "
                f"{p2_new_stats.districts[district].stars}{STAR_EMOJI} "
                f"{p2_new_stats.districts[district].destruction}%\n\n"
            )

        match_info += (
            f"**Final Score**\n"
            f"{p1_new_stats.stars}{STAR_EMOJI} {p1_new_stats.destruction}% - "
            f"{p2_new_stats.stars}{STAR_EMOJI} {p2_new_stats.destruction}%"
        )

        guild = await member_guild(ctx)
        members = await asyncio.gather(guild.fetch_member(player1_id), guild.fetch_member(player2_id))
        finished_embed = ipy.Embed(
            title="**Match has Ended**",
            description=f"{mention_user(members[0], player1_id)} ({player1_elo_change_str}) vs. "
                        f"{mention_user(members[1], player2_id)} ({player2_elo_change_str})\n\n"
                        f"{match_info}",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=RED_COLOR,
        )

        try:
            async with match_message_lock(match.thread):
                match_thread = await self.bot.fetch_channel(int(match.thread))
                await self._publish_result_embeds_locked(match, match_thread, finished_embed)
                await publish_result_operation(match)
        except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
            logging.exception("Corrected result publication incomplete for match %s", token)
            await ctx.send(
                f"{IMPORTANT_EMOJI} The corrected results are recorded, but the match messages could not all "
                "be updated. Publication is incomplete and will be retried.", ephemeral=True,
            )
            return
        await ctx.send(
            f"{SUCCESS_EMOJI} The match results are resubmitted, please use `/player matches ...` "
            f"and `/player profile` commands to double check if everything was done correctly.",
            ephemeral=True,
        )

    async def _publish_result_embeds_locked(
            self, match: Match, match_thread: ipy.GuildPublicThread, finished_embed: ipy.Embed,
    ) -> None:
        operation = match.result_operation
        if not operation or not operation.completed:
            raise ResultOperationConflict(match.token)
        for target in ("summary", "public"):
            channel = match_thread if target == "summary" else await self.bot.fetch_channel(MATCH_CHANNEL)
            message_id = operation.summary_message_id if target == "summary" else operation.public_message_id
            embed = copy.deepcopy(finished_embed)
            embed.color = COLOR if target == "summary" else RED_COLOR
            message = None
            if target == "public" and not message_id and not match.is_war and match.msg:
                try:
                    message = await channel.fetch_message(int(match.msg))
                except ipy.errors.NotFound:
                    pass
                else:
                    await bind_result_message(match, int(message.id), target="public")
                    message_id = int(message.id)
            if message_id:
                try:
                    if message is None:
                        message = await channel.fetch_message(int(message_id))
                    if target == "public" and not match.is_war:
                        await edit_channel_message(message, embed=embed, components=[])
                    else:
                        await edit_channel_message(message, embed=embed)
                except ipy.errors.HTTPException:
                    if message is not None:
                        await _mark_result_publication_incomplete(message)
                    raise
                continue
            content = (
                f"<@{match.participants[0].user_id}> and <@{match.participants[1].user_id}>, "
                "please double check if the match results are correct."
            ) if target == "summary" else await choose_tip()
            components: list[ipy.ActionRow] = []
            if target == "summary":
                delete_button = ipy.Button(
                    style=ipy.ButtonStyle.DANGER, label="Delete Thread",
                    custom_id=f"delete_thread|{match.token}", emoji=CROSS_EMOJI,
                )
                components = [ipy.ActionRow(delete_button)]
            with anyio.CancelScope(shield=True):
                message = await channel.send(content, embed=embed, components=components)
                try:
                    await bind_result_message(match, int(message.id), target=target)
                except (PyMongoError, ResultOperationConflict):
                    await _mark_result_publication_incomplete(message)
                    raise

    @base.subcommand(sub_cmd_name="autowin", sub_cmd_description="Auto-win for a selected player")
    @ipy.max_concurrency(ipy.Buckets.CHANNEL, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="winning_user",
        description="The user that will get an auto-win",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @ipy.slash_option(
        name="reason",
        description="The reason for the auto-win",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    async def match_autowin(self, ctx: ipy.SlashContext, winning_user: ipy.User, reason: str):
        data_generation = PLAYER_DATA_BOUNDARY.generation
        await ctx.defer(ephemeral=True)

        try:
            async with PLAYER_DATA_BOUNDARY.write(data_generation):
                # fetch the match by current thread channel
                match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
                if not match:
                    await ctx.send(
                        f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                        ephemeral=True
                    )
                    return

                if match.autowin_completed or (match.is_autowin and match.autowin_winner_id is None):
                    await ctx.send(f"{ERROR_EMOJI} An auto-win has already been given to this match already.", ephemeral=True)
                    return

                if is_final_match(match):
                    await ctx.send(f"{ERROR_EMOJI} Use `/final match forfeit` to enter zero scores and the advancing player.",
                                   ephemeral=True)
                    return

                p1: Participant = match.participants[0]
                p2: Participant = match.participants[1]
                player1_id, player2_id = p1.user_id, p2.user_id
                match_token = match.token
                participant_ids = {player1_id, player2_id}

                if str(winning_user.id) not in participant_ids:
                    await ctx.send(f"{ERROR_EMOJI} The winning player is not part of this match.", ephemeral=True)
                    return

                match = await claim_autowin(match, str(winning_user.id), str(ctx.author.id), reason)
                if match.result_operation and not match.result_operation.published:
                    await ctx.send(
                        f"{ERROR_EMOJI} Results are already being recorded for this match. The auto-win was not applied.",
                        ephemeral=True,
                    )
                    return
                if match.autowin_winner_id is None:
                    await ctx.send(
                        f"{ERROR_EMOJI} The match changed before the auto-win was recorded. Please retry.", ephemeral=True,
                    )
                    return
                if match.autowin_winner_id != str(winning_user.id):
                    await ctx.send(
                        f"{ERROR_EMOJI} This match already has an auto-win decision for <@{match.autowin_winner_id}>. "
                        "Retry with that winner to finish the interrupted operation.",
                        ephemeral=True,
                    )
                    return
                guild = await member_guild(ctx)
                actor = ctx.author if match.autowin_actor_id == str(ctx.author.id) else (
                    await guild.fetch_member(match.autowin_actor_id)
                )
                decision_reason = f"{mention_user(actor, match.autowin_actor_id)}: {match.autowin_reason}"

                # determine losing user
                losing_id = player2_id if str(winning_user.id) == player1_id else player1_id
                losing_user = await self.bot.fetch_user(int(losing_id)) if str(
                    losing_id).isdigit() else await self.bot.fetch_user(losing_id)

                winning_elo = await apply_autowin_player(str(winning_user.id), match, 5)
                losing_elo = await apply_autowin_player(losing_id, match, -35)
                await mark_autowin(match)
                for player, participant in enumerate(match.participants):
                    if participant.base_store is not None or participant.opponent_bases_stored:
                        if not await base_store_publication.mark_store_reserved(
                                self.bot, match, player,
                        ):
                            await ctx.send(
                                f"{ERROR_EMOJI} The auto-win rating changes are saved, but the stored-base message "
                                "could not be updated. Its slot remains reserved. Please retry, or use **Undo** "
                                "before retrying.",
                                ephemeral=True,
                            )
                            return
                reservation_notice = (
                    "\n\nStored-base slots remain reserved until you use **Undo**, the raid weekend ends, "
                    "or this thread is deleted."
                    if has_reserved_match_slots(match) else ""
                )
                await free_match_clans(match)

                hosts = match.hosts
                times = match.times
                single_host = hosts[0] if hosts else None
                single_time = int(match.matched_time or times[0] or 0)
                order_ids = [player1_id, player2_id]
                if not match.is_war:
                    if single_host and single_time:
                        await award_helper_once(
                            single_host, f"autowin:{match.id}:host:0", 3,
                            HelperHistory(time=single_time, player1=player1_id, player2=player2_id),
                        )
                else:
                    for i in range(2):
                        host_id = hosts[i] if i < len(hosts) else None
                        host_time = times[i] if i < len(times) else None
                        if host_id and host_time:
                            await award_helper_once(
                                host_id, f"autowin:{match.id}:host:{i}", 0.5,
                                HelperHistory(time=int(host_time), player1=order_ids[i]),
                            )
                        submitters = match.submitters or [None, None]
                        submitter_id = submitters[i] if i < len(submitters) else None
                        if submitter_id:
                            await award_helper_once(submitter_id, f"autowin:{match.id}:submitter:{i}", 0.5)

                await remove_helper_task(match.thread, player1_id)
                await remove_helper_task(match.thread, player2_id)

                # DMs to players
                winning_embed = ipy.Embed(
                    title=f"**Auto-win Against {losing_user}**",
                    description=f"**Elo Change:** {WIN_EMOJI} {winning_elo - 5} → "
                                f"{winning_elo}\n\n"
                                f"*Note: auto-wins do not affect your winrate.*",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                try:
                    await winning_user.send(embed=winning_embed)
                except ipy.errors.Forbidden:
                    pass

                losing_embed = ipy.Embed(
                    title=f"**Penalty in Match Against {losing_user}**",
                    description=f"**Elo Change:** {LOSS_EMOJI} {losing_elo + 35} → "
                                f"{losing_elo}\n\n"
                                f"*Note: penalties do not affect your winrate.*",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                try:
                    await losing_user.send(embed=losing_embed)
                except ipy.errors.Forbidden:
                    pass

                match_embed = ipy.Embed(
                    title="**An Auto-Win Has Been Issued**",
                    description=f"{mention_user(winning_user)} is given an auto-win, "
                                f"and {mention_user(losing_user)} is penalized after "
                                f"careful considerations.{reservation_notice}",
                    fields=[ipy.EmbedField(name="**Reason**", value=decision_reason)],
                    footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                    color=COLOR
                )
                delete_button = ipy.Button(
                    style=ipy.ButtonStyle.DANGER,
                    label="Delete Thread",
                    custom_id=f"delete_thread|{match_token}",
                    emoji=CROSS_EMOJI,
                )
                await ctx.channel.send(
                    embed=match_embed,
                    components=[ipy.ActionRow(delete_button)],
                )

                await autoqueue_completed_match_participants(self.bot, match_token, (player1_id, player2_id))
                await mark_autowin(match, completed=True)
                await ctx.send(
                    f"{SUCCESS_EMOJI} An auto-win has been given to {winning_user.mention}, "
                    f"and {losing_user.mention} is penalized.{reservation_notice}",
                    ephemeral=True,
                )
                schedule_channel_name_edit(ctx.channel, "🔴")
        except PlayerDataReset as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)

    @base.subcommand(sub_cmd_name="list", sub_cmd_description="List the upcoming matches")
    @ipy.slash_option(
        name="hidden",
        description="Make the bot response hidden or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_list(self, ctx: ipy.SlashContext, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)

        guild = await member_guild(ctx)
        matches: list[Match] = await Match.find_all().to_list()
        # sort by scheduled time (fall back to 0 if missing)
        matches_sorted = sorted(matches, key=lambda m: m.matched_time or 0)

        now = int(datetime.now().timestamp())
        description = ""

        for match in matches_sorted:
            # skip if scheduled time already passed
            if not match.matched_time or match.matched_time < now:
                continue

            if len(match.participants) < 2:
                continue
            player1 = match.participants[0].user_id
            player2 = match.participants[1].user_id

            timestamp = match.matched_time
            long_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.LongDateTime)
            relative_ts = ipy.Timestamp.fromtimestamp(timestamp).format(ipy.TimestampStyles.RelativeTime)

            members = await asyncio.gather(guild.fetch_member(player1), guild.fetch_member(player2))
            description += (f"{mention_user(members[0], player1)} vs. {mention_user(members[1], player2)}\n"
                            f"{long_ts}\n{relative_ts}\n")

            # host_clans field replaces old "hostClan"
            if match.host_clans and match.host_clans[0]:
                clan = await self.coc_client.get_clan(match.host_clans[0])
                description += f"[{clan.name} ({clan.tag})]({clan.share_link})\n\n"
            else:
                description += "\n"

        embed = ipy.Embed(
            title="**Upcoming Elo Matches**",
            description=description if description else "No upcoming matches.",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        await ctx.send(embed=embed, hidden=hidden)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def base_notify(self):
        matches: list[Match] = await Match.find_all().to_list()
        ts = int(datetime.now().timestamp())

        for match in matches:
            token = match.token

            if match.is_war:
                # per-player times
                for player in range(2):
                    if match.times and player < len(match.times):
                        t = match.times[player]
                    else:
                        t = None

                    if t and (t - 600 - ts) < 600:
                        opponent = 0 if player else 1
                        date = datetime.fromtimestamp(t - 600)
                        task = ipy.Task(
                            functools.partial(send_bases, self.bot, token, player, opponent),
                            ipy.DateTrigger(date)
                        )
                        task.start()
            else:
                # single scheduled time
                t = match.matched_time
                if t and (t - 600 - ts) < 600:
                    date = datetime.fromtimestamp(t - 600)

                    task1 = ipy.Task(
                        functools.partial(send_bases, self.bot, token, 0, 1),
                        ipy.DateTrigger(date)
                    )
                    task1.start()

                    task2 = ipy.Task(
                        functools.partial(send_bases, self.bot, token, 1, 0),
                        ipy.DateTrigger(date)
                    )
                    task2.start()

    @ipy.Task.create(ipy.IntervalTrigger(minutes=20))
    async def host_notify(self):
        ts = int(datetime.now().timestamp())
        matches = await Match.find_all().to_list()

        for match in matches:
            if not match.thread:
                continue

            if match.is_war:
                # check both slots
                for i in range(2):
                    if match.times[i] and (match.times[i] - 3600 - ts) < 1200 and not match.hosts[i]:
                        date = datetime.fromtimestamp(match.times[i] - 3600)
                        task = ipy.Task(
                            functools.partial(send_warnings, self.bot, match.token),
                            ipy.DateTrigger(date),
                        )
                        task.start()
            else:
                # non-war: check single time/host
                if match.times[0] and (match.times[0] - 3600 - ts) < 1200 and not match.hosts[0]:
                    date = datetime.fromtimestamp(match.times[0] - 3600)
                    task = ipy.Task(
                        functools.partial(send_warnings, self.bot, match.token),
                        ipy.DateTrigger(date),
                    )
                    task.start()

    base_group = base.group(name="base", description="")

    async def _prepare_base_store(
            self,
            request: BaseStorePreparationRequest,
    ) -> BaseStorePreparationOutcome:
        return await base_store_storage.prepare_base_store(self, request)

    async def _base_store_sync_slots(self, clan_tag: str | None = None) -> dict[str, list[str]]:
        return await sync_base_store_slots(clan_tag, bot=self.bot)

    async def _base_store_choose_bases(self, match: Match, player: int) -> None:
        opponent = 1 - player
        player_obj = await self.bot.fetch_user(match.participants[player].user_id)
        opponent_obj = await self.bot.fetch_user(match.participants[opponent].user_id)
        await choose_bases(match.token, player_obj, opponent_obj, True)

    async def _base_store_clear_participant(
            self,
            match: Match,
            player: int,
            reset_stored_flag: bool = False,
            *,
            reason: BaseStoreReleaseReason,
    ) -> bool:
        return await clear_participant_base_store(
            match, player, reset_stored_flag=reset_stored_flag, bot=self.bot, reason=reason,
        )

    async def _base_store_try_reserve_side(
            self,
            token: str,
            player: int,
    ) -> ResourceMatchSideReservation | None:
        return await try_reserve_resource_match_side(token, player)

    async def _base_store_try_reserve_clan(self, clan_tag: str) -> ResourceClanReservation | None:
        return await try_reserve_resource_clan(clan_tag)

    async def _base_store_is_clan_available(self, clan_tag: str) -> bool:
        return await is_clan_available(clan_tag)

    async def _base_store_confirm_pending(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
    ) -> bool:
        return await confirm_pending_opponent_bases(
            self.bot,
            ctx,
            PendingBaseConfirmation(match=state.match, player=state.player),
        )

    async def _base_store_start_local(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
            clan: coc.Clan,
    ) -> ipy.Message | None:
        return await war_start.start_war(
            self.bot,
            ctx,
            war_start.WarStart(
                match=state.match,
                player=state.player,
                host=self.bot.user,
                clan=clan,
                coc_client=self.coc_client,
                reply=war_start.StoredWarStartReply(
                    slot=state.slot,
                    slot_instruction="",
                    helper_hosted=True,
                ),
                stored_start=StoredStartState.capture_stores(state.match, (state.player,)),
            ),
        )

    @base_group.subcommand(sub_cmd_name="store", sub_cmd_description="Store bases in a clan for raid weekend")
    @ipy.slash_option(
        name="player",
        description="The player to store bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used to store the bases",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="slot",
        description="The slot in which the bases are stored",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Slot 1", value="Slot 1"),
            ipy.SlashCommandChoice(name="Slot 2", value="Slot 2"),
        ],
        required=True
    )
    async def match_base_store(self, ctx: ipy.SlashContext, player: int, clan: str, slot: str):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # disallow storing bases if the invoker is one of the players (unless a bot owner)
        is_player = any(str(ctx.author.id) == p.user_id for p in match.participants[:2])
        if is_player and str(ctx.author.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True)
            return

        # resolve player/opponent indices
        p_idx = 0 if player == 0 else 1
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]
        o: Participant = match.participants[o_idx]
        player_id, opponent_id = p.user_id, o.user_id

        if p.base_store is not None:
            reserved_clan = await self.coc_client.get_clan(p.base_store.clan_tag)
            presentation = BaseStorePresentationRequest(
                match=match,
                player=p_idx,
                helper_id=str(ctx.author.id),
                clan=reserved_clan,
                bases_stored=False,
                failure_reason="A stored-slot reservation already exists.",
            )
            if await base_store_publication.present_retained_base_store(ctx, presentation):
                await ctx.send(
                    f"{ERROR_EMOJI} The bases for <@{player_id}> already reserve a slot. "
                    "Its Undo control has been restored.",
                    ephemeral=True,
                )
            return
        if p.opponent_bases_stored:
            await ctx.send(f"{ERROR_EMOJI} The bases for <@{player_id}> are already stored.", ephemeral=True)
            return

        finished_player = p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        if slot not in BASE_STORE_SLOTS:
            await ctx.send(f"{ERROR_EMOJI} `{slot}` is not a valid base-store slot.", ephemeral=True)
            return

        clan_obj = await self.coc_client.get_clan(clan)
        preparation = await self._prepare_base_store(BaseStorePreparationRequest(
            thread_id=str(ctx.channel.id),
            player=p_idx,
            clan=clan_obj,
            slot=slot,
        ))
        match preparation:
            case BaseStorePreparationFailure(message=message):
                await ctx.send(message, ephemeral=True)
                return
            case PreparedBaseStore(match=prepared_match):
                match = prepared_match
            case unreachable:
                assert_never(unreachable)

        presentation = BaseStorePresentationRequest(
            match=match,
            player=player,
            helper_id=str(ctx.author.id),
            clan=clan_obj,
            bases_stored=False,
        )

        if not await base_store_publication.publish_base_store_message(
                ctx, presentation, base_store_presentation.build_manual_store_components(presentation, slot),
        ):
            return
        await ctx.send(
            f"Please store the bases in the clan and slot you have selected.\n\n"
            f"**<@{opponent_id}>'s Bases**\n{base_store_presentation.format_opponent_bases(presentation)}",
            suppress_embeds=True,
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="autostore", sub_cmd_description="Store bases with Helper for raid weekend")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.slash_option(
        name="player",
        description="The player to store bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    async def match_base_autostore(self, ctx: ipy.SlashContext, player: int):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        p_idx = 0 if player == 0 else 1
        is_player = any(str(ctx.author.id) == p.user_id for p in match.participants[:2])
        if is_player and str(ctx.author.id) != str(self.bot.owner.id) and (
            str(ctx.author.id) == match.participants[p_idx].user_id
            or not any(ctx.author.has_role(role) for role in LVL2_ROLES)
        ):
            await ctx.send(f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True)
            return

        await base_store_storage.store_self_bases(
            self,
            ctx,
            SelfBaseStoreRequest(match=match, player=p_idx, helper_id=str(ctx.author.id)),
        )

    @ipy.component_callback(re.compile(r"undo_base_storage\|\w+\|\d+(?:\|\w+)?$"))
    async def undo_base_storage_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        _, token, player, *reservation = ctx.custom_id.split("|")

        # fetch match by token (replaces matches.json)
        match: Match | None = await Match.find_one(Match.token == token)
        if not match:
            failure = f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return

        idx = int(player)
        if idx >= len(match.participants):
            await ctx.send(f"{ERROR_EMOJI} Invalid player index.", ephemeral=True)
            return

        request = StoredBaseActionRequest(
            token, idx, reservation[0] if reservation else None, int(ctx.message.id),
        )
        if not stored_base_matches(match, request):
            failure = f"{ERROR_EMOJI} This message no longer has an active stored-base reservation."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return
        if not await bind_legacy_store_message(match, idx, int(ctx.message.id)):
            failure = f"{ERROR_EMOJI} The reservation changed; removal could not be confirmed."
            await base_store_publication.mark_store_unavailable(ctx.message, context=ctx)
            await ctx.send(failure, ephemeral=True)
            return

        # unset the stored flag for the selected player
        if not await clear_participant_base_store(
                match, idx, reset_stored_flag=True, bot=self.bot, reason="undone",
        ):
            await ctx.send(f"{ERROR_EMOJI} The reservation changed; removal could not be confirmed.", ephemeral=True)
            return

        await base_store_publication.mark_store_status(
            ctx.message, "**Base Storage Undone**", context=ctx,
        )
        await ctx.send(
            f"{SUCCESS_EMOJI} This action has been undone, please make sure that the bases are removed from the clans.",
            ephemeral=True
        )

    @ipy.component_callback(re.compile(r"start_match\|\d+\|[^|]+\|[^|]+(?:\|\w+)?$"))
    async def start_match_button(self, ctx: ipy.ComponentContext):
        _, player, clan, slot, *reservation = ctx.custom_id.split("|")
        await self._start_war(
            ctx, int(player), clan, host=ctx.author, slot=slot,
            stored_reservation_id=reservation[0] if reservation else None,
        )

    @base_group.subcommand(sub_cmd_name="remove-all", sub_cmd_description="Remove all bases for a match")
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="override_code",
        description="An override code for privileged removal",
        opt_type=ipy.OptionType.STRING,
        min_length=16,
        max_length=16,
        required=False
    )
    async def match_base_remove_all(self, ctx: ipy.SlashContext, player: int, override_code: str = None):
        await ctx.defer(ephemeral=True)

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission check: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants[:2]]
        host_ids = (match.hosts or [None, None])
        is_player = str(ctx.author.id) in participant_ids
        is_host = str(ctx.author.id) in [h for h in host_ids if h]
        if not (is_player or is_host or str(ctx.author.id) == str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to remove the bases.",
                           ephemeral=True)
            return

        # resolve the effective target before checking the opponent's result
        p_idx = 0 if player == 0 else 1
        if is_player:
            p_idx = participant_ids.index(str(ctx.author.id))
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]  # the player whose bases will be removed
        o: Participant = match.participants[o_idx]  # opponent

        started_host = match.hosts[o_idx] if match.is_war else match.hosts[0]
        if started_host:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove bases after the match has started.", ephemeral=True)
            return

        # prevent removal if opponent's result is fully submitted (match finished)
        has_override = await OverrideCode.is_valid("match", override_code)
        if is_match_finished(match, player=o_idx) and not has_override:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove a base once the match has been finished.", ephemeral=True)
            return

        if has_override and not await OverrideCode.consume("match", override_code):
            await ctx.send(
                f"{ERROR_EMOJI} The override code has expired or was already used.",
                ephemeral=True,
            )
            return

        for i in range(len(match.final_districts)):
            base = p.bases[i] if i < len(p.bases) else None
            if not base:
                continue

            district = match.final_districts[i] if i < len(match.final_districts) else None
            if not district:
                continue

            p.bases[i] = None

        # persist match changes
        await match.save_changes()

        # resolve player mention for confirmation
        player_mention = f"<@{p.user_id}>"
        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully removed all bases submitted by {player_mention}!",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="remove", sub_cmd_description="Remove bases for a match")
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The base is for which district?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="override_code",
        description="An override code for privileged removal",
        opt_type=ipy.OptionType.STRING,
        min_length=16,
        max_length=16,
        required=False
    )
    async def match_base_remove(self, ctx: ipy.SlashContext, player: int, district: str, override_code: str = None):
        await ctx.defer(ephemeral=True)

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants[:2]]
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in participant_ids
                and str(ctx.author.id) not in host_ids
                and str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to remove the bases.",
                           ephemeral=True)
            return

        # district must be assigned in this match
        if district not in (match.final_districts or []):
            await ctx.send(f"{ERROR_EMOJI} **{district}** is not one of the assigned districts!", ephemeral=True)
            return
        district_index = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        # resolve initial target (by slash option): p = selected player, o = opponent
        p_idx = 0 if player == 0 else 1
        o_idx = 1 - p_idx
        p: Participant = match.participants[p_idx]
        o: Participant = match.participants[o_idx]

        # if invoker is a player, force-removal on their own bases
        has_override = await OverrideCode.is_valid("match", override_code)
        if (str(ctx.author.id) in participant_ids and str(ctx.author.id) != str(self.bot.owner.id) and
                not has_override):
            p_idx = participant_ids.index(str(ctx.author.id))
            o_idx = 1 - p_idx
            p, o = match.participants[p_idx], match.participants[o_idx]

        started_host = match.hosts[o_idx] if match.is_war else match.hosts[0]
        if started_host:
            await ctx.send(f"{ERROR_EMOJI} You cannot remove bases after the match has started.", ephemeral=True)
            return

        # block if opponent has a result recorded for this district (match finished for that slot)
        if (((getattr(match, "is_autowin", False) is True) or
             (o.result and len(o.result) > district_index and o.result[district_index])) and
                not has_override):
            await ctx.send(f"{ERROR_EMOJI} You cannot remove a base once the match has been finished.", ephemeral=True)
            return

        # ensure target has a base at this district
        if not (p.bases and len(p.bases) > district_index and p.bases[district_index]):
            await ctx.send(f"{ERROR_EMOJI} <@{p.user_id}> does not have any bases for `{district}`.", ephemeral=True)
            return

        if has_override and not await OverrideCode.consume("match", override_code):
            await ctx.send(
                f"{ERROR_EMOJI} The override code has expired or was already used.",
                ephemeral=True,
            )
            return

        # remove base and persist to Mongo
        p.bases[district_index] = None
        await match.save_changes()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have successfully removed <@{p.user_id}>'s base for **{district}**",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="set-image", sub_cmd_description="Set the scout view of a base")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to set base scout view for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The district of the base",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="The scout view of the base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=True
    )
    async def match_base_set_image(self, ctx: ipy.SlashContext, player: int, district: str, base_ss: ipy.Attachment):
        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        # only host(s) or bot owner may set scout views
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in host_ids) and (str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host is allowed to set scout views.", ephemeral=True)
            return

        # district must be assigned for this match
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not chosen for this match, you cannot submit a base for it.",
                ephemeral=True)
            return

        # resolve participant and district index
        p_idx = 0 if player == 0 else 1
        p: Participant = match.participants[p_idx]
        district_index = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        finished_player = 1 - p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # base must exist to set image
        base = p.bases[district_index] if (p.bases and len(p.bases) > district_index) else None
        if not base:
            await ctx.send(f"{ERROR_EMOJI} No base has been submitted by <@{p.user_id}> for **{district}**.",
                           ephemeral=True)
            return

        # download -> re-upload to owner -> delete temp
        token = secrets.token_hex(8)
        downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
        if not downloaded:
            await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
            return

        file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
        await delete_file(f"temp/{token}.webp")

        # save image URL on the BaseLink
        base.image = file_msg.attachments[0].url
        await match.save_changes()

        await ctx.send(f"{SUCCESS_EMOJI} You have set <@{p.user_id}>'s base scout view for **{district}**",
                       ephemeral=True)

    @base_group.subcommand(sub_cmd_name="set", sub_cmd_description="Set bases for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to set bases for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    async def match_base_set(self, ctx: ipy.SlashContext, player: int, base_link: str, base_ss: ipy.Attachment = None):
        await ctx.defer(ephemeral=True)

        # validate link
        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        if (not base_link.startswith("https://link.clashofclans.com")
                or "OpenLayout&id" not in base_link.split("=")
                or base_link.split("%")[1] != "3ACC"):
            await ctx.send(
                f"{ERROR_EMOJI} The link you have provided does not look like a Clash of Clans base link.",
                ephemeral=True
            )
            return

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # disallow hosts command if the invoker is a player; they must use "match base submit"
        if any(str(ctx.author.id) == p.user_id for p in match.participants[:2]):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot use the command as one of the players. Please use "
                f"{self.bot.mention_command(name='match base submit', scope=int(ctx.guild.id))} instead.",
                ephemeral=True
            )
            return

        # only a host or bot owner can set bases
        host_ids = [h for h in (match.hosts or []) if h]
        if (str(ctx.author.id) not in host_ids) and (str(ctx.author.id) != str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host is allowed to set bases.", ephemeral=True)
            return

        # for non-war matches, ensure a time is scheduled
        if (not match.is_war) and not match.matched_time:
            await ctx.send(f"{ERROR_EMOJI} Please schedule a time for the match first.", ephemeral=True)
            return

        # determine district from a link; ensure it’s part of this match
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not chosen for this match, you cannot submit a base for it.",
                ephemeral=True
            )
            return

        # resolve participant and district index
        p_idx = 0 if player == 0 else 1
        p: Participant = match.participants[p_idx]
        d_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        finished_player = 1 - p_idx if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # prevent overwriting an existing base
        if p.bases and len(p.bases) > d_idx and p.bases[d_idx]:
            await ctx.send(
                f"{ERROR_EMOJI} A base has already been set for `{district}`. If you wish to set another one, "
                f"please remove the existing one using `/match base remove`.",
                ephemeral=True
            )
            return

        # handle optional screenshot upload
        base_image_url = UNAVAILABLE_IMAGE
        if base_ss:
            token = secrets.token_hex(8)
            downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
            if not downloaded:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
                return

            file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
            await delete_file(f"temp/{token}.webp")
            base_image_url = file_msg.attachments[0].url

        # write into the document and persist
        p.bases[d_idx] = BaseLink(link=base_link, image=base_image_url)
        await match.save_changes()

        await ctx.send(
            f"{SUCCESS_EMOJI} You have set <@{p.user_id}>'s base for **{district}**",
            ephemeral=True
        )

    @base_group.subcommand(sub_cmd_name="submit", sub_cmd_description="Submit bases for your match")
    @ipy.slash_option(
        name="base_link",
        description="The base link",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @ipy.slash_option(
        name="base_ss",
        description="Screenshot of your first base",
        opt_type=ipy.OptionType.ATTACHMENT,
        required=False
    )
    @ipy.slash_option(name="match", description="Final match, e.g. QF1 (optional in its thread)",
                      opt_type=ipy.OptionType.STRING, autocomplete=True)
    async def match_base_submit(self, ctx: ipy.SlashContext, base_link: str, base_ss: ipy.Attachment = None,
                                match: str = None):
        await ctx.defer(ephemeral=True)

        target_node = match

        # validate link
        if not validators.url(base_link):
            await ctx.send(f"{ERROR_EMOJI} The link you have provided is invalid.", ephemeral=True)
            return

        link_parts = base_link.split("%")
        if (not base_link.startswith("https://link.clashofclans.com/")
                or "OpenLayout&id" not in base_link.split("=")
                or len(link_parts) < 3 or link_parts[1] != "3ACC"
                or link_parts[2] not in DISTRICT_CODES_REVERSED):
            await ctx.send(
                f"{ERROR_EMOJI} The link you have provided does not look like a Clash of Clans base link.",
                ephemeral=True
            )
            return

        try:
            match = await self._base_command_match(ctx, target_node)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if not match.base_submission:
            await ctx.send(f"{ERROR_EMOJI} Base submission is not enabled for this match.", ephemeral=True)
            return

        participant_ids = [p.user_id for p in match.participants]
        author_id = str(ctx.author.id)

        if is_final_match(match) and (ctx.guild_id != match.routing_guild_id
                or ctx.author.has_role(ELO_BLACKLIST_ROLE) or match.final_superseded or match.final_result_id):
            await ctx.send(f"{ERROR_EMOJI} This match is unavailable or finished.", ephemeral=True)
            return

        if author_id not in participant_ids:
            await ctx.send(
                f"{ERROR_EMOJI} Only the players participating the match can submit bases. "
                f"Please ask a host to use `/match base set` if you need help.",
                ephemeral=True
            )
            return

        # determine opponent index
        if author_id == participant_ids[0]:
            opp_index = 1
            author_index = 0
        else:
            opp_index = 0
            author_index = 1

        finished_player = opp_index if match.is_war else None
        if is_match_finished(match, player=finished_player):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # time gate: allow submission only up to 10 minutes before match
        now_ts = int(datetime.now().timestamp())
        match_time = None
        if match.times:
            match_time = match.times[opp_index] if match.is_war else match.times[0]
        if match_time and (match_time - now_ts) < 600:
            await ctx.send(
                f"{ERROR_EMOJI} It is too late to submit now, you can only submit bases 10 minutes before the match.",
                ephemeral=True
            )
            return

        # district from a link; must be one of the assigned districts
        district = DISTRICT_CODES_REVERSED[base_link.split('%')[2]]
        if district not in (match.final_districts or []):
            await ctx.send(
                f"{ERROR_EMOJI} The link is for **{district}**, which is not chosen for this match.",
                ephemeral=True
            )
            return

        d_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]

        # disallow helper bases
        is_helper_base = await BaseLayout.find_one(BaseLayout.link == base_link, BaseLayout.quality != "Private")
        if is_helper_base:
            await ctx.send(
                f"{ERROR_EMOJI} The link is a helper's base. You may still use this base, but please "
                f"generate a different link for it.",
                ephemeral=True
            )
            return

        # prevent duplicate submission for this district
        author_participant = match.participants[author_index]
        previous_base = author_participant.bases[d_idx]
        if is_final_match(match) and previous_base and await BaseLayout.find_one(
                BaseLayout.link == previous_base.link, BaseLayout.quality != "Private"):
            await ctx.send(f"{ERROR_EMOJI} The **{district}** base is a Helper base and cannot be replaced.", ephemeral=True)
            return
        if not is_final_match(match) and author_participant.bases and len(author_participant.bases) > d_idx and author_participant.bases[d_idx]:
            await ctx.send(
                f"{ERROR_EMOJI} A base has already been submitted for `{district}`. If you wish to submit another one, "
                f"please remove the existing one using `/match base remove`.",
                ephemeral=True
            )
            return

        # optional screenshot upload
        base_image_url = UNAVAILABLE_IMAGE
        if base_ss:
            token = secrets.token_hex(8)
            downloaded = await download_file(base_ss.url, f"temp/{token}.webp")
            if not downloaded:
                await ctx.send(f"{ERROR_EMOJI} Something went wrong when downloading the image.", ephemeral=True)
                return

            file_msg = await self.bot.owner.send(file=ipy.File(f"temp/{token}.webp"), silent=True)
            await delete_file(f"temp/{token}.webp")
            base_image_url = file_msg.attachments[0].url

        # persist on the document (no list-size enforcing per your note)
        author_participant.bases[d_idx] = BaseLink(link=base_link, image=base_image_url)
        if is_final_match(match):
            saved = await match.get_pymongo_collection().update_one({
                "_id": match.id, "final_base_page_revision": match.final_base_page_revision,
                "final_superseded": False, "final_result_id": None,
                f"participants.{author_index}.user_id": author_id,
                "times": match.times,
            }, {"$set": {f"participants.{author_index}.bases.{d_idx}": author_participant.bases[d_idx].model_dump()},
                "$inc": {"final_base_page_revision": 1}})
            if not saved.matched_count:
                await ctx.send(f"{ERROR_EMOJI} The match changed. Your base was not saved.", ephemeral=True)
                return
        else:
            await match.save_changes()

        # edit the pinned match message to inform helpers (unchanged behavior)
        try:
            thread = await self.bot.fetch_channel(match.thread) if target_node else ctx.channel
            match_msg = await find_match_thread_announcement(thread, match, self.bot.user.id)
        except ipy.errors.HTTPException:
            await ctx.send(f"{ERROR_EMOJI} Base saved. The match message could not be updated; notify the host.",
                           ephemeral=True)
            return
        if match_msg is None:
            await ctx.send(
                f"{ERROR_EMOJI} Your base was saved, but the match message could not be updated. "
                "Please ask a host to notify the helpers.",
                ephemeral=True,
            )
            return
        try:
            if not match_msg.pinned:
                await match_msg.pin()
            note = f"- {mention_user(ctx.author)} has submitted bases."
            if len(match_msg.embeds[0].fields) < 5:
                match_msg.embeds[0].add_field(name="**Notes**", value=note)
            elif not re.search(
                    rf"(?:<@!?{ctx.author.id}>|users/{ctx.author.id}>\)(?:\*\*)?) has submitted bases\.",
                    match_msg.embeds[0].fields[4].value,
            ):
                match_msg.embeds[0].fields[4].value += f"\n{note}"
            await match_msg.edit(embeds=match_message_embeds(match_msg.embeds))
        except ipy.errors.HTTPException:
            await ctx.send(f"{ERROR_EMOJI} Base saved. The match message could not be updated; notify the host.",
                           ephemeral=True)
            return

        await ctx.send(
            f"{SUCCESS_EMOJI} Base submitted for **{district}**.",
            ephemeral=True
        )

    async def _base_command_match(self, ctx, node_id=None):
        if node_id is None:
            return await Match.find_one(Match.thread == str(ctx.channel.id))
        from common.finals.lifecycle import resolve_final
        final = await resolve_final()
        if ctx.guild_id != final.guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE):
            raise FinalError("Use this command in the Final's server while eligible.")
        node = final.nodes.get(node_id.upper())
        if node is None or node.match_id is None:
            raise FinalError("Choose a prepared match from `/final match list`.")
        return await Match.get(node.match_id)

    @base_group.subcommand(sub_cmd_name="view", sub_cmd_description="View the bases of the match")
    @ipy.check(ipy.has_any_role(*LVL3_ROLES))
    @ipy.slash_option(
        name="player",
        description="A player of the match",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ]
    )
    @ipy.slash_option(name="match", description="Final match, e.g. QF1 (optional in its thread)",
                      opt_type=ipy.OptionType.STRING, autocomplete=True)
    async def match_base_view(self, ctx: ipy.SlashContext, player: int = 0, match: str = None):
        await ctx.defer(ephemeral=True)

        try:
            match = await self._base_command_match(ctx, match)
        except FinalError as error:
            await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
            return
        if not match:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        # permission: only players, hosts, or bot owner
        participant_ids = [p.user_id for p in match.participants]
        viewer_is_player = str(ctx.author.id) in participant_ids
        opponent = 0 if player == 1 else 1
        host_id = match.hosts[opponent] if match.is_war else match.hosts[0]
        viewer_is_host = str(ctx.author.id) == host_id
        if not (viewer_is_player or viewer_is_host or str(ctx.author.id) == str(self.bot.owner.id)):
            await ctx.send(f"{ERROR_EMOJI} Only the host and the players are allowed to view the bases.",
                           ephemeral=True)
            return

        # if the viewer is a player, force 'player' to their own index (keeps legacy behavior)
        if viewer_is_player and str(ctx.author.id) != str(self.bot.owner.id):
            player = participant_ids.index(str(ctx.author.id))

        # resolve which participant to display
        p_idx = 0 if player == 0 else 1
        participant: Participant = match.participants[p_idx]

        # for mentions
        player_user = await self.bot.fetch_user(participant.user_id)

        # helper bases data (unchanged JSON file)
        current_base_links = [b.link for b in participant.bases if b]
        helper_docs = await BaseLayout.find(
            In(BaseLayout.link, current_base_links),
            BaseLayout.quality != "Private"
        ).to_list()
        helper_links = {doc.link for doc in helper_docs}

        embeds = []
        index = -1
        for base in participant.bases:
            index += 1
            district = match.final_districts[index] if index < len(match.final_districts) else "Unknown"
            embed = ipy.Embed(
                title=f"**{district}**",
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=COLOR
            )

            if not base:
                embed.set_image(UNAVAILABLE_IMAGE)
                embed.description = f"{IMPORTANT_EMOJI} Base missing.\n"
            else:
                if (base.link in helper_links
                        and viewer_is_player
                        and not viewer_is_host
                        and str(ctx.author.id) != str(self.bot.owner.id)):
                    embed.set_image(UNAVAILABLE_IMAGE)
                    embed.description = "This is a helpers' base"
                else:
                    embed.set_image(base.image)
                    embed.description = base.link

            embeds.append(embed)

        await ctx.send(f"Here are {player_user.mention}'s Bases", embeds=embeds, ephemeral=True)

    war_group = base.group(name="war", description="")

    async def _start_war(self, ctx: ipy.SlashContext | ipy.ComponentContext, player: int,
                         clan: str, host: ipy.User = None, scout: bool = False, slot: str = None,
                         automatic: bool = False, district: str | None = None,
                         stored_reservation_id: str | None = None):
        await ctx.defer(ephemeral=True)

        if not host:
            host = ctx.author

        match: Match | None = await Match.find_one(Match.thread == str(ctx.channel.id))
        if not match:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match, player=player):
            player_id = match.participants[player].user_id
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        if automatic and district is not None:
            if not match.is_war:
                await ctx.send(f"{ERROR_EMOJI} This is not a war-style match.", ephemeral=True)
                return
            host_clan_tag = started_host_clan_tag(match, player)
            if host_clan_tag is None:
                await ctx.send(
                    f"{ERROR_EMOJI} You can only select a district after this side of the match has started.",
                    ephemeral=True,
                )
                return
            if match_district_index(match, district) is None:
                await ctx.send(
                    f"{ERROR_EMOJI} **{district}** is not one of this match's districts.",
                    ephemeral=True,
                )
                return
            if clan != host_clan_tag:
                await ctx.send(
                    f"{ERROR_EMOJI} This side of the match is hosted in **{host_clan_tag}**. "
                    "Select that clan for a district rerun.",
                    ephemeral=True,
                )
                return
            clan_obj = await self.coc_client.get_clan(host_clan_tag)
            side_reservation = await try_reserve_resource_match_side(str(match.token), player)
            if side_reservation is None:
                await ctx.send(
                    f"{ERROR_EMOJI} A Helper job is already running for this side of the match.",
                    ephemeral=True,
                )
                return
            try:
                await helper_host.host_war_district_automatically(
                    self,
                    ctx,
                    helper_host.AutomaticWarDistrictHost(
                        match=match,
                        player=player,
                        clan=clan_obj,
                        district=district,
                        requested_by=str(ctx.author.id),
                    ),
                )
            finally:
                side_reservation.release()
            return

        stored_clan, stored_slot = get_participant_base_store(match.participants[player])
        if isinstance(ctx, ipy.ComponentContext) and ctx.custom_id.startswith("start_match|"):
            request = StoredBaseActionRequest(match.token, player, stored_reservation_id, int(ctx.message.id))
            store = match.participants[player].base_store
            if (
                    not stored_base_matches(match, request) or (clan, slot) != (stored_clan, stored_slot)
                    or (store is not None and store.ready is False)
            ):
                failure = f"{ERROR_EMOJI} This message no longer has an active stored-base reservation."
                await base_store_publication.mark_store_unavailable(ctx.message)
                await ctx.send(failure, ephemeral=True)
                return
            if not await bind_legacy_store_message(match, player, int(ctx.message.id)):
                failure = f"{ERROR_EMOJI} The stored-base reservation changed; the match was not started."
                await base_store_publication.mark_store_unavailable(ctx.message)
                await ctx.send(failure, ephemeral=True)
                return
        if stored_clan and not isinstance(ctx, ipy.SlashContext):
            clan = stored_clan
            slot = stored_slot

        # prevent re-init if already set up as a live (non-war) match with a time
        if not match.is_war and match.times[0]:
            await ctx.send(f"{ERROR_EMOJI} A live match is already initiated.", ephemeral=True)
            return

        if automatic and hosting_started(match, player):
            await ctx.send(f"{ERROR_EMOJI} The match has already started.", ephemeral=True)
            return

        if isinstance(ctx, ipy.SlashContext):
            participant = match.participants[player]
            store = held_store(participant)
            if (participant.base_store is not None or participant.opponent_bases_stored) and (
                    store is None or (store.reservation_id is not None and store.message_id is None)
            ):
                await ctx.send(
                    f"{ERROR_EMOJI} Base storage is incomplete. Wait for **Bases Stored**, "
                    "or use its Undo control before storing again.", ephemeral=True,
                )
                return

        if not await confirm_pending_opponent_bases(
                self.bot,
                ctx,
                PendingBaseConfirmation(match=match, player=player),
        ):
            return

        opponent = 0 if player else 1
        opponent_id = match.participants[opponent].user_id

        # optional: warn if opponent’s bases are missing scout image(s)
        if scout:
            district, link = is_missing_scout_view(match, opponent_id)
            if district:
                await ctx.send(
                    f"{ERROR_EMOJI} The opponent's (Player {opponent + 1}) submitted [base]({link}) for **{district}** "
                    f"is missing a scout view. Please manually set the scout view by running "
                    f"{self.bot.mention_command('match base set-image')}!",
                    ephemeral=True
                )

        stored_start = None
        if is_raid_weekend() and isinstance(ctx, ipy.SlashContext):
            store = held_store(match.participants[player])
            if store is None and int(self.bot.user.id) == BOT_ID:
                await ctx.send(
                    f"{ERROR_EMOJI} You can only start a match with ready stored bases during the raid weekend.",
                    ephemeral=True,
                )
                return
            if store is not None:
                if clan != store.clan_tag:
                    await ctx.send(
                        f"{ERROR_EMOJI} This player's bases are stored in **{store.clan_tag}**, **{store.slot}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                slot = store.slot
                stored_start = StoredStartState.capture_stores(match, (player,))

        player_id = match.participants[player].user_id
        if (
            not automatic
            and player_id == str(host.id)
            and str(host.id) != str(self.bot.owner.id)
        ):
            await ctx.send(f"{ERROR_EMOJI} You cannot host your own matches.", ephemeral=True)
            return

        clan_obj = await self.coc_client.get_clan(clan)
        side_reservation = await try_reserve_resource_match_side(str(match.token), player)
        if not side_reservation:
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        reservation = await try_reserve_resource_clan(clan_obj.tag)
        if not reservation:
            side_reservation.release()
            await ctx.send(
                f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        try:
            if isinstance(ctx, ipy.ComponentContext) and ctx.custom_id.startswith("start_match|"):
                refreshed_match = await Match.find_one(match.base_store_filter(player))
                if not refreshed_match:
                    failure = f"{ERROR_EMOJI} The stored-base reservation changed; the match was not started."
                    await base_store_publication.mark_store_unavailable(ctx.message)
                    await ctx.send(failure, ephemeral=True)
                    return
                match = refreshed_match
            if not await is_clan_available(clan_obj.tag):
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
                return
            if not automatic and stored_start is not None:
                slot_instruction = ""
            elif not automatic:
                available_slots, slot_owners = await get_start_base_store_slot_availability(
                    clan_obj.tag, match.thread, bot=self.bot,
                )
                if not available_slots:
                    await ctx.send(
                        construct_occupied_slots_error(clan_obj.tag, clan_obj.name, slot_owners),
                        ephemeral=True,
                    )
                    return
                slot_instruction = construct_start_slot_instruction(available_slots, clan_obj.name)

            if automatic:
                await helper_host.host_war_automatically(
                    self,
                    ctx,
                    helper_host.AutomaticWarHost(
                        match=match,
                        player=player,
                        clan=clan_obj,
                        host=host,
                        scout=scout,
                        clan_reservation=reservation,
                        side_reservation=side_reservation,
                        stored_start=stored_start,
                    ),
                )
                return

            match ctx:
                case ipy.SlashContext() if stored_start is not None:
                    assert slot is not None
                    reply = war_start.StoredWarStartReply(slot=slot, slot_instruction=slot_instruction)
                case ipy.SlashContext():
                    reply = war_start.SlashWarStartReply(slot_instruction=slot_instruction)
                case ipy.ComponentContext():
                    assert slot
                    reply = war_start.StoredWarStartReply(slot=slot, slot_instruction=slot_instruction)
                case unreachable:
                    assert_never(unreachable)

            started_message = await war_start.start_war(
                self.bot,
                ctx,
                war_start.WarStart(
                    match=match,
                    player=player,
                    host=host,
                    clan=clan_obj,
                    coc_client=self.coc_client,
                    reply=reply,
                    scout=scout,
                    stored_start=stored_start,
                ),
            )
            if not started_message:
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
        finally:
            reservation.release()
            side_reservation.release()

    @war_group.subcommand(sub_cmd_name="start", sub_cmd_description="Start a war-style match instantly")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_war_start(self, ctx: ipy.SlashContext | ipy.ComponentContext, player: int,
                              clan: str, scout: bool = False, slot: str = None):
        await self._start_war(ctx, player, clan, scout=scout, slot=slot)

    @war_group.subcommand(sub_cmd_name="autostart", sub_cmd_description="Use Helper to host a war-style match")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    @ipy.slash_option(
        name="district",
        description="Rerun one district after this side starts; leave blank to host all",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=False,
    )
    async def match_war_autostart(self, ctx: ipy.SlashContext, player: int,
                                  clan: str, scout: bool = False,
                                  district: str | None = None):
        await self._start_war(
            ctx,
            player,
            clan,
            scout=scout,
            automatic=True,
            district=district,
        )

    @ipy.component_callback(re.compile(r"^host_clan_select\|\w+\|\d+$"))
    @coordinated_match_message
    async def host_clan_select(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        match = await Match.find_one(Match.token == token)

        if match is None:
            await ctx.message.edit(
                components=utils.misc_utils.disable_components(*ctx.message.components)
            )
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.",
                ephemeral=True,
            )
            return

        if not await check_match_control(ctx, match, index):
            return
        expected = MatchMessageState.capture(match, index)

        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot set a clan for a match that has ended.",
                ephemeral=True,
            )
            return

        # authorization: unchanged logic, read from the document
        if match.hosts[index] and str(ctx.author.id) != match.hosts[index]:
            await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
            return

        # fetch clan and update only the changed field
        clan = await self.coc_client.get_clan(ctx.values[0])
        reservation = await try_reserve_resource_clan(clan.tag)
        if not reservation or not await claim_resource_clan(
                reservation,
                match,
                index,
                allow_current_owner=True,
                expected_control=expected,
        ):
            await ctx.send(
                f"{ERROR_EMOJI} **{clan.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        # update the message embed as before
        ctx.message.embeds[0].fields[2].value = f"[{clan.name} ({clan.tag})]({clan.share_link})"
        embed = copy.deepcopy(ctx.message.embeds[0])
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, index), embed=embed, existing_message=ctx.message,
        )

        await ctx.send(
            f"{SUCCESS_EMOJI} You have set [{clan.name} ({clan.tag})]({clan.share_link}) as the host clan!",
            ephemeral=True,
        )
    @war_group.subcommand(sub_cmd_name="schedule", sub_cmd_description="Schedule a match for a specific player")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to schedule match for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="timestamp",
        description="A Discord timestamp or a Unix timestamp",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @coordinated_match_message
    async def match_war_schedule(self, ctx: ipy.SlashContext, player: int, timestamp: str):
        await ctx.defer(ephemeral=True)

        ts = extract_integer(timestamp)
        if not ts:
            await ctx.send(f"{ERROR_EMOJI} The timestamp you provided is not valid.", ephemeral=True)
            return

        # find the match by the current thread/channel id
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match, player=player):
            player_id = match.participants[player].user_id
            await ctx.send(f"{ERROR_EMOJI} The match has already finished for <@{player_id}>.", ephemeral=True)
            return

        # prevent scheduling if a live (non-war) match already initiated (mirrors old logic)
        if (not match.is_war) and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A live match is already initiated.", ephemeral=True)
            return

        if ts > 32536799999:
            await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
            return

        # determine embed title (Scheduled/Rescheduled)
        prev_ts = match.times[player]
        title = "**Match Scheduled**"
        if prev_ts and prev_ts != ts:
            title = "**Match Rescheduled**"

        # pick the player user for mention (from participants; fallback to command invoker)
        player_obj = await self.bot.fetch_user(match.participants[player].user_id)
        clan_tag = match.host_clans[player]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        embed = ipy.Embed(
            title=title,
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**", value="Not assigned"),
                ipy.EmbedField(name="**Host Clan**",
                               value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned")
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        host_clan_select = ipy.StringSelectMenu(
            *HOST_CLAN_OPTIONS,
            placeholder="🛡️ Set the host clan",
            custom_id=f"host_clan_select|{match.token}|{player}"
        )

        host_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Host",
            custom_id=f"match_host|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="🙋")
        )

        store_button = ipy.Button(
            style=ipy.ButtonStyle.GRAY,
            label="Store Result",
            custom_id=f"match_result_store|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="💾")
        )

        abort_button = ipy.Button(
            style=ipy.ButtonStyle.RED,
            label="Abort",
            custom_id=f"match_abort|{match.token}|{player}",
            emoji=ipy.PartialEmoji(name="🛑")
        )

        if is_final_match(match):
            try:
                match = await schedule_final_match(match, int(ts), player=player)
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
        else:
            await match.set({
                "is_war": True,
                f"hosts.{player}": None,
                f"times.{player}": int(ts),
            })
        match.is_war = True
        match.hosts[player] = None
        match.times[player] = int(ts)
        await append_helper_task(match.thread, match.participants[player].user_id, ts)
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, player), content=player_obj.mention, embed=embed,
            components=[ipy.ActionRow(host_clan_select), ipy.ActionRow(host_button, store_button, abort_button)],
        )

        await ctx.send(f"{SUCCESS_EMOJI} You have successfully scheduled a match for {player_obj.mention}!",
                       ephemeral=True)

        # if both players have scheduled times, flip the channel name style
        if all(match.times):
            schedule_channel_name_edit(ctx.channel, "🔵")

    @ipy.component_callback(re.compile(r"^match_abort\|\w+\|\w+$"))
    async def abort_match_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        # load match by token from Mongo
        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(f"{ERROR_EMOJI} You cannot abort a match that has ended.")
            return

        # authorization check against a host list
        if match.hosts and match.hosts[index] and str(ctx.author.id) != match.hosts[index]:
            await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
            return

        abort = war_start.WarAbort(match=match, player=index, message=ctx.message)
        aborted = await war_start.abort_war_start(ctx, abort)
        if not aborted:
            await ctx.send(
                f"{ERROR_EMOJI} The match changed before it could be aborted safely. Please contact an operator.",
                ephemeral=True,
            )
            return

        await ctx.send(f"{SUCCESS_EMOJI} The match has been aborted.", ephemeral=True)

    @ipy.component_callback(re.compile(r"^match_host\|\w+\|\w+$"))
    @coordinated_match_message
    async def host_match_button(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        # fetch the match by token
        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if not await check_match_control(ctx, match, index):
            return

        # role/permission check (unchanged)
        user_roles = {int(role.id) for role in ctx.author.roles}
        if not user_roles.intersection({STAFF_ROLE, SEN_ROLE, MODERATOR_ROLE, ADMIN_ROLE}):
            await ctx.send(f"{ERROR_EMOJI} You do not have permission to host this match.", ephemeral=True)
            return

        # disallow hosting own match
        player_id = match.participants[index].user_id
        if player_id and str(player_id) == str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot host your own matches.", ephemeral=True)
            return

        # prevent hosting ended match (keep old UX)
        if (is_match_finished(match, player=index)
                or ctx.message.embeds[0].title == "**Match Ended**"):
            await ctx.send(f"{ERROR_EMOJI} You cannot host a match that has ended.", ephemeral=True)
            return

        if not await update_match_control(
                match, MatchMessageState.capture(match, index), {f"hosts.{index}": str(ctx.author.id)},
        ):
            await reject_outdated_match_control(ctx)
            return
        match.hosts[index] = str(ctx.author.id)

        # update the embed field “Host”
        ctx.message.embeds[0].fields[1].value = mention_user(ctx.author, mention_type="Helper")
        embed = copy.deepcopy(ctx.message.embeds[0])
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, index), embed=embed, existing_message=ctx.message,
        )

        await ctx.send(f"{SUCCESS_EMOJI} You have set yourself as the host of this match!", ephemeral=True)

        # if both hosts chosen, flip the channel to green
        if all(match.hosts):
            schedule_channel_name_edit(ctx.channel, "🟢")

    @ipy.component_callback(re.compile(r"^match_result_store\|\w+\|\w+$"))
    async def result_store_button(self, ctx: ipy.ComponentContext):
        _, token, index = ctx.custom_id.split("|")
        index = int(index)

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.message.edit(components=utils.misc_utils.disable_components(*ctx.message.components))
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, the match has probably ended.")
            return

        if not await check_match_control(ctx, match, index):
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return

        # identify whether the clicker is one of the two players
        user_ids = [p.user_id for p in (match.participants or [])[:2]]
        is_player = str(ctx.author.id) in {str(uid) for uid in user_ids if uid}

        # if the clicker is a player, they can only submit if they are also the host for this side.
        current_host = match.hosts[index]
        if is_player and str(ctx.author.id) != str(current_host):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)
            return

        participant = match.participants[index]
        player = await self.bot.fetch_user(participant.user_id)

        # prepare labels and default values
        districts = match.final_districts
        results = participant.result

        if is_final_match(match):
            await ctx.send_modal(ipy.Modal(ipy.ParagraphText(
                label="Seven district results", custom_id="scores", max_length=100,
                value="\n".join(results) if all(results) else None,
                placeholder="Seven lines in district order: stars.destruction",
            ), title="Store Final Result", custom_id=f"store_modal|{token}|{index}"))
            return

        # short text expects strings; use empty string for none
        val1 = results[0] or ""
        val2 = results[1] or ""
        val3 = results[2] or ""

        label1 = districts[0] or "District 1"
        label2 = districts[1] or "District 2"
        label3 = districts[2] or "District 3"

        title = f"{player} Result Submission" if player else "Result Submission"
        if len(title) > 45:
            prefix = str(player)[:45 - len(" Result Submission")].rstrip()
            title = f"{prefix} Result Submission" if prefix else "Result Submission"

        modal = ipy.Modal(
            ipy.ShortText(
                label=label1,
                placeholder="Format: [stars].[destruction]",
                value=val1,
                max_length=100,
                custom_id="result1",
                required=False,
            ),
            ipy.ShortText(
                label=label2,
                placeholder="Format: [stars].[destruction]",
                value=val2,
                max_length=100,
                custom_id="result2",
                required=False,
            ),
            ipy.ShortText(
                label=label3,
                placeholder="Format: [stars].[destruction]",
                value=val3,
                max_length=100,
                custom_id="result3",
                required=False,
            ),
            title=title,
            custom_id=f"store_modal|{token}|{index}",
        )

        await ctx.send_modal(modal)

    @ipy.modal_callback(re.compile(r"^store_modal\|\w+\|\w+$"))
    @coordinated_match_message
    async def result_store_modal(self, ctx: ipy.ModalContext, **response):
        await ctx.defer(ephemeral=True)

        _, token, index = ctx.custom_id.split("|")
        index = int(index)
        opponent_index = 0 if index else 1

        match = await Match.find_one(Match.token == token)
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match.", ephemeral=True)
            return

        if not await check_match_control(ctx, match, index):
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return
        expected = MatchMessageState.capture(match, index)

        if is_final_match(match):
            if (ctx.guild_id != match.routing_guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE)
                    or not any(ctx.author.has_role(role) for role in LVL2_ROLES)):
                await ctx.send(f"{ERROR_EMOJI} A staff member in the Final's server must store these results.",
                               ephemeral=True)
                return
            scores = response.get("scores", "").splitlines()
            if len(scores) != 7 or not all(is_valid_match_result_entry(score) for score in scores):
                await ctx.send(f"{ERROR_EMOJI} Enter seven valid stars.destruction lines.", ephemeral=True)
                return
            response = {f"result{position + 1}": score for position, score in enumerate(scores)}

        # resolve players from the participant list
        if (str(ctx.author.id) in {participant.user_id for participant in match.participants}
                and str(ctx.author.id) != str(match.hosts[index])):
            await ctx.send(f"{ERROR_EMOJI} You cannot use submit results as one of the players.", ephemeral=True)
            return
        player_id = match.participants[index].user_id
        player = await self.bot.fetch_user(int(player_id))

        # validate and stage updates to participant results
        updates = {}
        count = 0
        for result in response.values():
            if result and not is_valid_match_result_entry(result):
                await ctx.send(
                    f"{ERROR_EMOJI} The formatting of the {ORDINAL_REPRESENTATIONS[count]} "
                    f"result is incorrect. E.g., 2 stars 88% should be submitted as `2.88`.",
                    ephemeral=True,
                )
                return

            value = "" if not result else result
            updates[f"participants.{index}.result.{count}"] = value
            count += 1

        updated_results = list(match.participants[index].result)
        for position, value in enumerate(response.values()):
            updated_results[position] = value or ""
        if all(updated_results):
            updates[f"submitters.{index}"] = str(ctx.author.id)
        if updates:
            write = await match.get_pymongo_collection().update_one(
                {**expected.binding_filter(), "result_operation": None,
                 "final_result_id": None, "final_result_operation": None, "final_superseded": {"$ne": True},
                 "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
                 f"participants.{index}.result": list(match.participants[index].result)},
                {"$set": updates},
            )
            if write.matched_count != 1:
                fresh = await Match.find_one(Match.token == token)
                if fresh and (reason := result_input_error(fresh)):
                    await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
                else:
                    await reject_outdated_match_control(ctx)
                return
        match.participants[index].result = updated_results
        if all(updated_results):
            match.submitters[index] = str(ctx.author.id)
            await invite_eligible_players(self.bot, (player_id,))

        # reuse in-memory for checks/DM composition
        player_results = match.participants[index].result
        opponent_results = match.participants[opponent_index].result

        if all(player_results) and all(opponent_results):
            submit_button = ipy.Button(
                style=ipy.ButtonStyle.GRAY,
                label="Submit Result",
                custom_id=f"submit_button|{token}",
                emoji=ipy.PartialEmoji(name="📥"),
            )

            await ctx.send(
                f"{SUCCESS_EMOJI} You have stored the match result for {player.mention}.\n\n"
                f"{IMPORTANT_EMOJI} *Both players' matches are recorded, you may submit them now!*",
                components=submit_button,
                ephemeral=True,
            )
        else:
            await ctx.send(
                f"{SUCCESS_EMOJI} You have stored the match result for {player.mention}.\n\n"
                f"{IMPORTANT_EMOJI} *Please kick the player out of the clan once the attacks are done!*",
                ephemeral=True,
            )

        if all(player_results):
            already_ended = ctx.message.embeds[0].title == "**Match Ended**"
            await mark_match_message_ended(ctx.message)
            if already_ended:
                return

            # ensure submitters exists and set the submitter for this side
            await result_store_ui.notify_stored_result(
                result_store_ui.StoredResultDmRequest(
                    match=match,
                    player=index,
                    user=player,
                )
            )

            await self._finish_stored_result_side(match, index)

    async def _finish_stored_result_side(self, match: Match, player: int) -> None:
        old_message_id = match.old_msgs[player]
        if old_message_id:
            try:
                async with match_message_lock(match.thread):
                    current = await Match.find_one({"token": match.token})
                    if (current and current.id == match.id and current.thread == match.thread
                            and current.old_msgs[player] == old_message_id
                            and current.participants[player].user_id == match.participants[player].user_id
                            and current.participants[player].result == match.participants[player].result):
                        channel = await self.bot.fetch_channel(int(match.thread))
                        message = await channel.fetch_message(old_message_id)
                        await mark_match_message_ended(message)
            except ipy.errors.NotFound:
                pass
            except (ipy.errors.HTTPException, PyMongoError):
                logging.exception(
                    "Stored result message %s remains pending recovery for match %s", old_message_id, match.token,
                )
        await free_clan(match.host_clans[player], expected_thread=str(match.thread))
        await remove_helper_task(match.thread, match.participants[player].user_id)
        await autoqueue_completed_match_participants(self.bot, match.token, (match.participants[player].user_id,))

    async def store_match_result_automatically(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
            *,
            max_allowed_attacks: HelperScoreAttackLimit = 2,
    ) -> None:
        await result_store.store_match_result_automatically(
            self,
            ctx,
            result_store.AutomaticResultStoreRequest(
                match=match,
                player=player,
                max_allowed_attacks=max_allowed_attacks,
            ),
        )

    async def store_self_bases(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
    ) -> None:
        await base_store_storage.store_self_bases(
            self,
            ctx,
            SelfBaseStoreRequest(match=match, player=player),
        )

    async def _resolve_stored_base_control(
            self,
            token: str,
            player: int,
    ) -> StoredBaseControlOutcome:
        return await base_store_storage.resolve_stored_base_control(
            StoredBaseActionRequest(token=token, player=player)
        )

    @ipy.component_callback(re.compile(r"helper_stored_start\|\w+\|\d+(?:\|\w+)?$"))
    async def helper_stored_start_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        _, token, player_text, *reservation = ctx.custom_id.split("|")
        await base_store_storage.start_stored_bases(
            self,
            ctx,
            StoredBaseActionRequest(token, int(player_text), reservation[0] if reservation else None, int(ctx.message.id)),
        )

    @ipy.component_callback(re.compile(r"helper_stored_undo\|\w+\|\d+(?:\|\w+)?$"))
    async def helper_stored_undo_button(self, ctx: ipy.ComponentContext) -> None:
        await ctx.defer(ephemeral=True)
        _, token, player_text, *reservation = ctx.custom_id.split("|")
        await base_store_storage.undo_stored_bases(
            self,
            ctx,
            StoredBaseActionRequest(token, int(player_text), reservation[0] if reservation else None, int(ctx.message.id)),
        )

    async def store_self_result(
            self,
            ctx: ipy.SlashContext,
            match: Match,
            player: int,
    ) -> None:
        await result_store.store_match_result_automatically(
            self,
            ctx,
            result_store.AutomaticResultStoreRequest(
                match=match,
                player=player,
                expected_started_at=match.times[player],
                include_response_result=True,
                submitter_id=str(self.bot.user.id),
            ),
        )

    async def store_self_live_bases(self, ctx: ipy.SlashContext, match: Match) -> None:
        await live_base_service.store_self_live_bases(self, ctx, match)

    async def store_self_live_result(self, ctx: ipy.SlashContext, match: Match) -> None:
        await live_result_store.store_live_match_results(self, ctx, match)

    set_group = base.group(name="set", description="")

    @set_group.subcommand(sub_cmd_name="host", sub_cmd_description="Set a host for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="user",
        description="A 1v1 helper",
        opt_type=ipy.OptionType.USER,
        required=True
    )
    @coordinated_match_message
    async def match_set_host(self, ctx: ipy.SlashContext, user: ipy.Member):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        if match.is_war:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # require a scheduled time (use the first non-empty timestamp)
        ts = next((t for t in (match.times or []) if t), None)
        if not ts:
            await ctx.send(
                f"{ERROR_EMOJI} Please schedule a time using the command `/match schedule` before setting the host!",
                ephemeral=True
            )
            return

        # host must be a 1v1 helper
        if STAFF_ROLE not in [int(r.id) for r in user.roles]:
            await ctx.send(
                f"{ERROR_EMOJI} {user.mention} is not a 1v1 helper and cannot be selected as the host.",
                ephemeral=True
            )
            return

        # selected host cannot be one of the players
        player_ids = [p.user_id for p in (match.participants or [])[:2]]
        if str(user.id) in {str(pid) for pid in player_ids if pid}:
            await ctx.send(
                f"{ERROR_EMOJI} {user.mention} is a player in the match and cannot be a host!",
                ephemeral=True
            )
            return

        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, 0).binding_filter(), "result_operation": None,
             "participants.0.result": list(match.participants[0].result),
             "participants.1.result": list(match.participants[1].result),
             "is_autowin": {"$ne": True}},
            {"$set": {"hosts.0": str(user.id), "hosts.1": str(user.id)}},
        )
        if write.matched_count != 1:
            await ctx.send(f"{ERROR_EMOJI} The match changed. Check the current host before retrying.",
                           ephemeral=True)
            return
        match.hosts = [str(user.id), str(user.id)]

        # resolve host clan from whichever side is set
        clan_tag = match.host_clans[0]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        # send/edit the thread message (pin)
        thread_embed = ipy.Embed(
            title="**Match Scheduled**",
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**", value=mention_user(user, mention_type="Helper")),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        old_msg = None
        if match.old_msgs[0]:
            old_msg = await ctx.channel.fetch_message(int(match.old_msgs[0]))

        if old_msg:
            if old_msg.embeds and old_msg.embeds[0].title in (
                    "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
            ):
                thread_embed.title = old_msg.embeds[0].title
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=thread_embed, existing_message=old_msg,
            )
        else:
            await publish_match_message(ctx, match, MatchMessageState.capture(match, 0), embed=thread_embed)

        # send/edit the public announcement in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        public_msg = None
        if match.msg:
            public_msg = await channel.fetch_message(int(match.msg))

        if public_msg:
            public_msg.embeds[0].description = (
                "Spectate in the host clan with a TH5 or lower account. "
                f"Host: {mention_user(user, mention_type='Helper')}."
            )
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_msg.embeds[0],
                existing_message=public_msg, public_channel=channel,
            )
        else:
            p1 = await self.bot.fetch_user(int(match.participants[0].user_id))
            p2 = await self.bot.fetch_user(int(match.participants[1].user_id))

            public_embed = ipy.Embed(
                title=thread_embed.title,
                description=(
                    "Spectate in the host clan with a TH5 or lower account. "
                    f"Host: {mention_user(user, mention_type='Helper')}."
                ),
                fields=[
                    ipy.EmbedField(name="**Players**", value=f"{mention_user(p1)} vs. {mention_user(p2)}"),
                    ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                    ipy.EmbedField(
                        name="**Host Clan**",
                        value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                    ),
                ],
                footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
                color=GREEN_COLOR,
            )

            tip = await choose_tip()
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                content=tip, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully set the host of the match!", ephemeral=True)
        schedule_channel_name_edit(ctx.channel, "🟢")

    @set_group.subcommand(sub_cmd_name="clan", sub_cmd_description="Set a host clan for the match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @coordinated_match_message
    async def match_set_clan(self, ctx: ipy.SlashContext, clan: str):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # original logic: disallow when "war" (live match) flag is on
        if match.is_war:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # require a scheduled time (use the first non-empty timestamp)
        ts = next((t for t in (match.times or []) if t), None)
        if not ts:
            await ctx.send(
                f"{ERROR_EMOJI} Please schedule a time using the command `/match schedule` "
                f"before setting the host clan!",
                ephemeral=True
            )
            return

        # fetch clan and update host clan in DB
        coc_clan: coc.Clan = await self.coc_client.get_clan(clan)
        reservation = await try_reserve_resource_clan(coc_clan.tag)
        if not reservation or not await claim_resource_clan(
                reservation,
                match,
                0,
                player2_index=1,
                allow_current_owner=True,
        ):
            await ctx.send(
                f"{ERROR_EMOJI} **{coc_clan.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        # resolve the host user if already set on either side
        host_id = None
        if match.hosts:
            host_id = next((h for h in match.hosts if h), None)
        host = await self.bot.fetch_user(int(host_id)) if host_id else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        # thread message (pin/edit)
        embed = ipy.Embed(
            title="**Match Scheduled**",
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**",
                               value=mention_user(host, mention_type="Helper") if host else "Not assigned"),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{coc_clan.name} ({coc_clan.tag})]({coc_clan.share_link})"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        old_msg = None
        if match.old_msgs[0]:
            old_msg = await ctx.channel.fetch_message(int(match.old_msgs[0]))

        if old_msg:
            if old_msg.embeds and old_msg.embeds[0].title in (
                    "**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
            ):
                embed.title = old_msg.embeds[0].title
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=embed, existing_message=old_msg,
            )
        else:
            await publish_match_message(ctx, match, MatchMessageState.capture(match, 0), embed=embed)

        # public message in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        public_msg = None
        if match.msg:
            public_msg = await channel.fetch_message(int(match.msg))

        if public_msg:
            # update Host Clan field (index 2) in existing embed
            public_msg.embeds[0].fields[2] = ipy.EmbedField(
                name="**Host Clan**",
                value=f"[{coc_clan.name} ({coc_clan.tag})]({coc_clan.share_link})"
            )
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_msg.embeds[0],
                existing_message=public_msg, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully set the host clan of the match!", ephemeral=True)

    @base.subcommand(sub_cmd_name="abort", sub_cmd_description="Abort a live match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    async def match_abort(self, ctx: ipy.SlashContext):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True
            )
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # a live match must have a scheduled time
        if not match.times[0]:
            await ctx.send(f"{ERROR_EMOJI} A live match has not been initiated.", ephemeral=True)
            return

        # only the assigned host(s) can abort (mirrors "you are not the host")
        if match.hosts and any(match.hosts):
            if str(ctx.author.id) not in {h for h in match.hosts if h}:
                await ctx.send(f"{ERROR_EMOJI} You are not the host of this match.", ephemeral=True)
                return

        # disallow abort if any result already stored for either player
        p0 = match.participants[0].result
        p1 = match.participants[1].result
        has_results = any(p0) or any(p1)
        if has_results:
            await ctx.send(
                f"{ERROR_EMOJI} You cannot abort a match that already has a stored result.",
                ephemeral=True
            )
            return

        old_msg = (
            await ctx.channel.fetch_message(match.old_msgs[0])
            if match.old_msgs and match.old_msgs[0]
            else None
        )
        aborted = await live_start.abort_live_start(
            ctx,
            live_start.LiveAbort(match=match, message=old_msg),
        )
        if not aborted:
            await ctx.send(
                f"{ERROR_EMOJI} The match changed before it could be aborted safely. Please contact an operator.",
                ephemeral=True,
            )
            return
        await ctx.send(f"{SUCCESS_EMOJI} The match has been aborted.", ephemeral=True)

    @base.subcommand(sub_cmd_name="start", sub_cmd_description="Start a live match instantly")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN
    )
    async def match_start(self, ctx: ipy.SlashContext, clan: str, scout: bool = False):
        await self._start_live(
            ctx,
            LivePreflight(clan=clan, scout=scout, automatic=False),
        )

    @base.subcommand(sub_cmd_name="autostart", sub_cmd_description="Use Helper to host a live match")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="clan",
        description="The clan that will be used for the match",
        opt_type=ipy.OptionType.STRING,
        choices=HOST_CLAN_CHOICES,
        required=True,
    )
    @ipy.slash_option(
        name="scout",
        description="To allow 10 minutes scout or not",
        opt_type=ipy.OptionType.BOOLEAN,
    )
    @ipy.slash_option(
        name="district",
        description="Rerun one district after the match starts; leave blank to host all",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=False,
    )
    async def match_autostart(
            self,
            ctx: ipy.SlashContext,
            clan: str,
            scout: bool = False,
            district: str | None = None,
    ) -> None:
        await self._start_live(
            ctx,
            LivePreflight(
                clan=clan,
                scout=scout,
                automatic=True,
                requested_by=str(ctx.author.id),
                district=district,
            ),
        )

    async def _host_live_district(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            request: LivePreflight,
            match: Match,
    ) -> None:
        district = request.district
        assert district is not None
        assert request.requested_by is not None
        if not request.automatic or match.is_war:
            await ctx.send(f"{ERROR_EMOJI} This is not a live match.", ephemeral=True)
            return
        host_clan_tag = started_host_clan_tag(match, 0)
        if host_clan_tag is None:
            await ctx.send(
                f"{ERROR_EMOJI} You can only select a district after the live match has started.",
                ephemeral=True,
            )
            return
        if match_district_index(match, district) is None:
            await ctx.send(
                f"{ERROR_EMOJI} **{district}** is not one of this match's districts.",
                ephemeral=True,
            )
            return
        if request.clan != host_clan_tag:
            await ctx.send(
                f"{ERROR_EMOJI} This match is hosted in **{host_clan_tag}**. "
                "Select that clan for a district rerun.",
                ephemeral=True,
            )
            return

        player1_id = match.participants[0].user_id
        player2_id = match.participants[1].user_id
        player1 = await Player.find_one(Player.user_id == str(player1_id))
        player2 = await Player.find_one(Player.user_id == str(player2_id))
        if player1 is None or player2 is None:
            await ctx.send(f"{ERROR_EMOJI} Both players must be registered for Helper hosting.", ephemeral=True)
            return

        side0 = await try_reserve_resource_match_side(str(match.token), 0)
        if side0 is None:
            await ctx.send(
                f"{ERROR_EMOJI} A Helper job is already running for this match.",
                ephemeral=True,
            )
            return
        side1 = await try_reserve_resource_match_side(str(match.token), 1)
        if side1 is None:
            side0.release()
            await ctx.send(
                f"{ERROR_EMOJI} A Helper job is already running for this match.",
                ephemeral=True,
            )
            return
        try:
            clan = await self.coc_client.get_clan(host_clan_tag)
            await helper_live.host_live_district_automatically(
                self,
                ctx,
                helper_live.AutomaticLiveDistrictHost(
                    match=match,
                    clan=clan,
                    player1_tag=player1.tag,
                    player2_tag=player2.tag,
                    district=district,
                    requested_by=request.requested_by,
                    public_progress=request.public_progress,
                    allow_opponent_cancel=request.allow_opponent_cancel,
                ),
            )
        finally:
            side1.release()
            side0.release()

    async def _start_live(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            request: LivePreflight,
    ) -> None:
        if request.defer_response:
            await ctx.defer(ephemeral=True)

        host = request.host
        scout = request.scout
        if not host:
            host = ctx.author

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        if request.district is not None:
            await self._host_live_district(ctx, request, match)
            return

        # disallow if a war-style match already initiated for one side
        if match.is_war and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A war-style match is already initiated for one of the players.",
                           ephemeral=True)
            return

        if hosting_started(match):
            await ctx.send(
                f"{ERROR_EMOJI} This match is already being started.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return

        if request.automatic:
            reservation_id = ready_live_reservation_id(match, request.stored_reservation_id)
            if request.stored_reservation_id and reservation_id is None:
                await ctx.send(f"{ERROR_EMOJI} The stored live bases are no longer ready.", ephemeral=True)
                return
            if reservation_id:
                stored_clan = live_stores(match, reservation_id)[0][1].clan_tag
                if request.clan != stored_clan:
                    await ctx.send(
                        f"{ERROR_EMOJI} Both players' bases are stored in **{stored_clan}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                request = replace(request, stored_reservation_id=reservation_id)

        # host cannot be one of the two players
        player1_id = match.participants[0].user_id
        player2_id = match.participants[1].user_id
        if str(host.id) in {str(player1_id), str(player2_id)} and str(host.id) != str(self.bot.owner.id):
            await ctx.send(f"{ERROR_EMOJI} The players themselves cannot be the host.", ephemeral=True)
            return

        # player account checks
        p1_doc = await Player.find_one(Player.user_id == str(player1_id))
        p2_doc = await Player.find_one(Player.user_id == str(player2_id))

        player1_acc = await self.coc_client.get_player(p1_doc.tag)
        if player1_acc.town_hall >= 6:
            await ctx.send(f"{ERROR_EMOJI} <@{player1_id}>'s account is above TH6, which enables scouting traps "
                           f"from the FC preview. Please ask the player to change his account to a TH3!",
                           ephemeral=True)
            return

        stored_start = None
        if is_raid_weekend() and int(self.bot.user.id) == BOT_ID:
            if request.automatic and request.stored_reservation_id is None:
                await ctx.send(f"{ERROR_EMOJI} You cannot start a live match during the raid weekend.", ephemeral=True)
                return
            if not request.automatic:
                stored_clan = shared_store_clan(match)
                if stored_clan is None:
                    await ctx.send(
                        f"{ERROR_EMOJI} During raid weekend, `/match start` requires both players' bases "
                        "to be ready in separate slots in the same clan.", ephemeral=True,
                    )
                    return
                if request.clan != stored_clan:
                    await ctx.send(
                        f"{ERROR_EMOJI} Both players' bases are stored in **{stored_clan}**. "
                        "Start the match in that clan.", ephemeral=True,
                    )
                    return
                stored_start = StoredStartState.capture_stores(match, (0, 1))

        player2_acc = await self.coc_client.get_player(p2_doc.tag)
        if player2_acc.town_hall >= 6:
            await ctx.send(f"{ERROR_EMOJI} <@{player2_id}>'s account is above TH6, which enables scouting traps "
                           f"from the FC preview. Please ask the player to change his account to a TH3!",
                           ephemeral=True)
            return

        if scout:
            player_index = 1
            for pid in (player1_id, player2_id):
                district, link = is_missing_scout_view(match, str(pid))
                if district:
                    await ctx.send(f"{ERROR_EMOJI} Player {player_index}'s submitted [base]({link}) for "
                                   f"**{district}** is missing a scout view. Please manually set the scout view "
                                   f"by running {self.bot.mention_command('match base set-image')}!",
                                   ephemeral=True)
                    return
                player_index += 1

        clan_obj = await self.coc_client.get_clan(request.clan)
        if is_resource_clan_locked(clan_obj.tag) or not await is_clan_available(clan_obj.tag):
            await ctx.send(
                f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                f"{await available_resource_clans_text()}",
                ephemeral=True,
            )
            return
        slot_instruction = ""
        if request.automatic:
            assert request.requested_by is not None
            await helper_live.host_live_automatically(
                self,
                ctx,
                helper_live.AutomaticLiveHost(
                    match=match,
                    clan=clan_obj,
                    host=host,
                    player1=player1_acc,
                    player2=player2_acc,
                    player1_elo=p1_doc.elo,
                    player2_elo=p2_doc.elo,
                    scout=scout,
                    requested_by=request.requested_by,
                    public_progress=request.public_progress,
                    allow_opponent_cancel=request.allow_opponent_cancel,
                    stored_reservation_id=request.stored_reservation_id,
                ),
            )
            return
        if stored_start is None:
            available_slots, slot_owners = await get_start_base_store_slot_availability(
                clan_obj.tag, match.thread, bot=self.bot,
            )
        else:
            available_slots, slot_owners = [store.slot for _, store in stored_start.stores], {}
        if not available_slots:
            await ctx.send(
                construct_occupied_slots_error(clan_obj.tag, clan_obj.name, slot_owners),
                ephemeral=True,
            )
            return
        slot_instruction = construct_start_slot_instruction(available_slots, clan_obj.name)
        if stored_start is not None:
            slot_instruction = "\n" + "\n".join(
                f"Player {player + 1} attacks the bases in **{store.slot}** in **{clan_obj.name}**."
                for player, store in stored_start.stores
            )
        reply = live_start.ManualLiveStartReply(slot_instruction=slot_instruction, stored=stored_start is not None)
        request = live_start.LiveStart(
            match=match,
            host=host,
            clan=clan_obj,
            player1=player1_acc,
            player2=player2_acc,
            player1_elo=p1_doc.elo,
            player2_elo=p2_doc.elo,
            scout=scout,
            reply=reply,
            stored_start=stored_start,
        )
        outcome = await live_start.start_live_match(self.bot, ctx, request)
        match outcome:
            case live_start.LiveStartSucceeded():
                return
            case live_start.LiveStartBusy():
                await ctx.send(
                    f"{ERROR_EMOJI} This match is already being started.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
            case live_start.LiveStartClanUnavailable():
                await ctx.send(
                    f"{ERROR_EMOJI} **{clan_obj.name}** is currently occupied.\n\n"
                    f"{await available_resource_clans_text()}",
                    ephemeral=True,
                )
            case unreachable:
                assert_never(unreachable)

    @base.subcommand(sub_cmd_name="schedule", sub_cmd_description="Schedule a live match")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="timestamp",
        description="A Discord timestamp or a Unix timestamp",
        opt_type=ipy.OptionType.STRING,
        required=True
    )
    @coordinated_match_message
    async def match_schedule(self, ctx: ipy.SlashContext, timestamp: str):
        await ctx.defer(ephemeral=True)

        ts = extract_integer(timestamp)
        if not ts:
            await ctx.send(f"{ERROR_EMOJI} The timestamp you provided is not valid.", ephemeral=True)
            return

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return

        if is_match_finished(match):
            await ctx.send(f"{ERROR_EMOJI} The match has already finished.", ephemeral=True)
            return

        # disallow if a war-style match already initiated for one side
        if match.is_war and any(match.times or []):
            await ctx.send(f"{ERROR_EMOJI} A war-style match is already initiated for one of the players.",
                           ephemeral=True)
            return

        if ts > 32536799999:
            await ctx.send(f"{ERROR_EMOJI} The given timestamp is out of range.", ephemeral=True)
            return

        prev_time = next((t for t in (match.times or []) if t), None)
        title = "**Match Scheduled**" if (prev_time is None or prev_time == ts) else "**Match Rescheduled**"

        if is_final_match(match):
            try:
                match = await schedule_final_match(match, int(ts))
            except FinalError as error:
                await ctx.send(f"{ERROR_EMOJI} {error}", ephemeral=True)
                return
        else:
            await match.set({
                "is_war": False,
                "times.0": int(ts),
                "times.1": int(ts),
            })
        match.is_war = False
        match.times = [int(ts), int(ts)]

        # resolve host (if already chosen) and host clan (if set)
        host_id = next((h for h in match.hosts if h), None)
        host_user = await self.bot.fetch_user(int(host_id)) if host_id else None

        clan_tag = match.host_clans[0] or match.host_clans[1]
        clan = await self.coc_client.get_clan(clan_tag) if clan_tag else None

        long_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.LongDateTime)
        relative_ts = ipy.Timestamp.fromtimestamp(ts).format(ipy.TimestampStyles.RelativeTime)

        p1_id = int(match.participants[0].user_id)
        p2_id = int(match.participants[1].user_id)

        embed = ipy.Embed(
            title=title,
            fields=[
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host**",
                               value=mention_user(host_user, mention_type="Helper") if host_user else "Not assigned"),
                ipy.EmbedField(
                    name="**Host Clan**",
                    value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"
                ),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR,
        )

        await append_helper_task(match.thread, match.participants[0].user_id, ts)
        await append_helper_task(match.thread, match.participants[1].user_id, ts)
        await publish_match_message(
            ctx, match, MatchMessageState.capture(match, 0), content=f"<@{p1_id}>\n<@{p2_id}>", embed=embed,
        )

        # public message in MATCH_CHANNEL
        channel = await self.bot.fetch_channel(match.routing_channel_id if is_final_match(match)
                                              else MATCH_CHANNEL)
        guild = await member_guild(ctx)
        members = await asyncio.gather(guild.fetch_member(p1_id), guild.fetch_member(p2_id))
        public_embed = ipy.Embed(
            title=title,
            description=("Spectate in the host clan with a TH5 or lower account. "
                         + (f"Host: {mention_user(host_user, mention_type='Helper')}."
                            if host_user else "Currently the host is not determined.")),
            fields=[
                ipy.EmbedField(name="**Players**",
                               value=f"{mention_user(members[0], p1_id)} vs. {mention_user(members[1], p2_id)}"),
                ipy.EmbedField(name="**Time**", value=f"{long_ts}\n{relative_ts}"),
                ipy.EmbedField(name="**Host Clan**",
                               value=f"[{clan.name} ({clan.tag})]({clan.share_link})" if clan else "Not assigned"),
            ],
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=GREEN_COLOR,
        )
        if match.msg:
            pub = await channel.fetch_message(int(match.msg))
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                existing_message=pub, public_channel=channel,
            )
        else:
            tip = await choose_tip()
            await publish_match_message(
                ctx, match, MatchMessageState.capture(match, 0), embed=public_embed,
                content=tip, public_channel=channel,
            )

        await ctx.send(f"{SUCCESS_EMOJI} Successfully scheduled the match!", ephemeral=True)

        # channel name color based on whether a host is set
        if host_user is None:
            schedule_channel_name_edit(ctx.channel, "🔵")
        else:
            schedule_channel_name_edit(ctx.channel, "🟢")

    result_group = base.group(name="result", description="")

    @result_group.subcommand(sub_cmd_name="clear", sub_cmd_description="Clear a player's stored result")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player whose result should be cleared",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1),
        ],
        required=True,
    )
    @coordinated_match_message
    async def match_result_clear(self, ctx: ipy.SlashContext, player: int) -> None:
        await ctx.defer(ephemeral=True)
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           "this command in a 1v1 thread!", ephemeral=True)
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return
        if match.recorded_match_id:
            await ctx.send(f"{ERROR_EMOJI} This match result is already recorded. "
                           "Use `/match resubmit` to correct it.", ephemeral=True)
            return

        is_player = str(ctx.author.id) in {participant.user_id for participant in match.participants}
        if is_player and str(ctx.author.id) != str(match.hosts[player]):
            await ctx.send(f"{ERROR_EMOJI} You cannot clear result as one of the players in the match.",
                           ephemeral=True)
            return

        field = f"participants.{player}.result"
        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, player).binding_filter(), "result_operation": None,
             "recorded_match_id": None, "final_result_id": None, "final_result_operation": None,
             "final_superseded": {"$ne": True},
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             field: list(match.participants[player].result)},
            {"$set": {field: [None] * len(match.final_districts), f"submitters.{player}": None}},
        )
        if write.matched_count != 1:
            fresh = await Match.find_one(Match.token == match.token)
            reason = result_input_error(fresh) if fresh else None
            await ctx.send(f"{ERROR_EMOJI} {reason or 'The match changed. Check the current result before retrying.'}",
                           ephemeral=True)
            return
        await ctx.send(f"{SUCCESS_EMOJI} Cleared <@{match.participants[player].user_id}>'s result.", ephemeral=True)

    @result_group.subcommand(sub_cmd_name="autostore", sub_cmd_description="Store a player's result with Helper")
    @ipy.max_concurrency(CHANNEL_USER_CONCURRENCY_BUCKET, 1)
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to store result for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1),
        ],
        required=True,
    )
    @ipy.slash_option(
        name="detection_type",
        description="How Helper should detect the result",
        opt_type=ipy.OptionType.STRING,
        choices=[
            ipy.SlashCommandChoice(name="Strict", value="Strict"),
            ipy.SlashCommandChoice(name="Relaxed", value="Relaxed"),
        ],
        required=False,
    )
    async def match_result_autostore(
            self,
            ctx: ipy.SlashContext,
            player: int,
            detection_type: ResultDetectionType = "Strict",
    ) -> None:
        await ctx.defer(ephemeral=True)
        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(
                f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
                ephemeral=True,
            )
            return
        if str(ctx.author.id) == str(match.participants[player].user_id):
            await ctx.send(
                f"{ERROR_EMOJI} You cannot store your own result with this command. "
                "Please use `/self store result` instead.",
                ephemeral=True,
            )
            return
        if not match.is_war:
            await ctx.send(f"{ERROR_EMOJI} This match is not available for automatic result storage.", ephemeral=True)
            return
        if not match.times[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match has not started yet.", ephemeral=True)
            return
        if not match.host_clans[player]:
            await ctx.send(f"{ERROR_EMOJI} This war match does not have a helper clan yet.", ephemeral=True)
            return
        if len(match.final_districts) not in {3, 7} or not all(match.final_districts):
            await ctx.send(f"{ERROR_EMOJI} This match does not have three final districts yet.", ephemeral=True)
            return
        match detection_type:
            case "Strict":
                max_allowed_attacks = 2
            case "Relaxed":
                max_allowed_attacks = -2
            case unexpected:
                assert_never(unexpected)
        await self.store_match_result_automatically(
            ctx,
            match,
            player,
            max_allowed_attacks=max_allowed_attacks,
        )

    @result_group.subcommand(sub_cmd_name="store", sub_cmd_description="Store the result on a single district")
    @ipy.check(ipy.has_any_role(*LVL2_ROLES))
    @ipy.slash_option(
        name="player",
        description="The player to store result for",
        opt_type=ipy.OptionType.INTEGER,
        choices=[
            ipy.SlashCommandChoice(name="Player 1", value=0),
            ipy.SlashCommandChoice(name="Player 2", value=1)
        ],
        required=True
    )
    @ipy.slash_option(
        name="district",
        description="The district that got attacked?",
        opt_type=ipy.OptionType.STRING,
        choices=DISTRICT_CHOICES,
        required=True
    )
    @ipy.slash_option(
        name="stars",
        description="The number of stars the player scored",
        opt_type=ipy.OptionType.INTEGER,
        max_value=3,
        min_value=0,
        required=True
    )
    @ipy.slash_option(
        name="destruction",
        description="The destruction the player scored",
        opt_type=ipy.OptionType.INTEGER,
        max_value=100,
        min_value=0,
        required=True
    )
    async def match_result_store(self, ctx: ipy.SlashContext, player: int, district: str, stars: int, destruction: int):
        await ctx.defer(ephemeral=True)

        match = await Match.find_one(Match.thread == str(ctx.channel.id))
        if match is None:
            await ctx.send(f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use "
                           f"this command in a 1v1 thread!", ephemeral=True)
            return
        if reason := result_input_error(match):
            await ctx.send(f"{ERROR_EMOJI} {reason}", ephemeral=True)
            return

        # the district must be one of the chosen final districts
        if district not in (match.final_districts or []):
            await ctx.send(f"{ERROR_EMOJI} **{district}** is not chosen as part of the match!", ephemeral=True)
            return

        # if the clicker is a player, they can only store if they are the host for that side
        player_ids = [p.user_id for p in match.participants]
        is_player = str(ctx.author.id) in {str(pid) for pid in player_ids if pid}
        current_host = match.hosts[player]
        if is_player and str(ctx.author.id) != str(current_host):
            await ctx.send(f"{ERROR_EMOJI} You cannot store result as one of the players in the match.",
                           ephemeral=True)
            return

        # update the participant's result for the specified district
        district_idx = match.final_districts.index(district) if is_final_match(match) else DISTRICT_INDEX[district]
        field = f"participants.{player}.result.{district_idx}"
        write = await match.get_pymongo_collection().update_one(
            {**MatchMessageState.capture(match, player).binding_filter(), "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             field: match.participants[player].result[district_idx]},
            {"$set": {field: f"{stars}.{destruction}"}},
        )
        if write.matched_count != 1:
            fresh = await Match.find_one(Match.token == match.token)
            reason = result_input_error(fresh) if fresh else None
            await ctx.send(f"{ERROR_EMOJI} {reason or 'The match changed. Check the current result before retrying.'}",
                           ephemeral=True)
            return

        player_id = match.participants[player].user_id
        await ctx.send(f"{SUCCESS_EMOJI} You have recorded <@{player_id}>'s score on **{district}**.",
                       ephemeral=True)

    @ipy.Task.create(ipy.IntervalTrigger(hours=3))
    async def inactive_threads_delete(self):
        channel = await self.bot.fetch_channel(MATCH_CHANNEL)
        thread_list: ipy.ThreadList = await channel.fetch_all_threads()
        for thread in thread_list.threads:
            if not thread.name.startswith("🔴"):
                continue

            last_messages = await thread.fetch_messages(limit=1)
            if not last_messages:
                continue

            last_message = last_messages[0]
            if datetime.now().timestamp() - last_message.created_at.timestamp() > 24 * 3600:
                match = await Match.find_one(Match.thread == str(thread.id))
                lock_key = match.token if match else f"thread:{thread.id}"
                lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault(lock_key, anyio.Lock())
                async with lock:
                    match = await Match.find_one(Match.thread == str(thread.id))
                    if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                        continue
                    current_messages = await thread.fetch_messages(limit=1)
                    if not current_messages or (
                            datetime.now().timestamp() - current_messages[0].created_at.timestamp() <= 24 * 3600
                    ):
                        continue
                    async with BASE_STORE_RESERVATION_LOCK:
                        match = await Match.find_one(Match.thread == str(thread.id))
                        if match and (has_pending_match_operation(match) or has_reserved_match_slots(match)):
                            continue
                        await thread.delete()
                        if match and not await delete_unreserved_match(match):
                            logging.warning("Match %s changed before expired-thread cleanup could finish", match.token)
                            continue
                    if match:
                        await free_match_clans(match)
                        for participant in match.participants:
                            await remove_helper_task(match.thread, participant.user_id)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def auto_match_confirm(self):
        matches = await Match.find({
            "$or": [
                {"thread": None, "is_challenge": False},
                {"thread": {"$ne": None}, "times": [None, None], "old_msgs": [None, None],
                 "is_autowin": {"$ne": True}},
            ],
        }).to_list()

        for match in matches:
            if is_final_match(match):
                continue
            p0, p1 = match.participants[0], match.participants[1]

            if not (p0.confirmed and p1.confirmed):
                async with QUEUE_OPERATION_LOCK:
                    match = await Match.find_one(Match.token == match.token)
                    if not match or match.thread or match.is_challenge or is_final_match(match):
                        continue
                    p0, p1 = match.participants[0], match.participants[1]
                    if p0.confirmed == p1.confirmed:
                        continue
                    now = datetime.now().timestamp()
                    if now - match.matched_time < 43200:
                        continue

                    confirmed, other = (p0, p1) if p0.confirmed else (p1, p0)
                    other.confirmed = True
                    if match.confirmation_tracking:
                        other.confirmed_at = now
                        other.confirmation_method = "automatic"
                    for i in range(3):
                        if not other.districts[i]:
                            other.districts[i] = confirmed.districts[i]
                    try:
                        await match.save_changes()
                    except PyMongoError:
                        logging.exception("Unable to save match confirmation for %s; retrying on the next sweep",
                                          match.token)
                        continue

            try:
                async with match_message_lock(match.thread or match.token):
                    await confirm_match(self.bot, match.token)
            except (ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError, MatchChannelUnavailable):
                logging.exception("Unable to finish match confirmation for %s; retrying on the next sweep", match.token)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def auto_match_cancel(self):
        matches = await Match.find().to_list()
        for match in matches:
            if match.is_challenge or is_final_match(match):
                continue

            p0, p1 = match.participants[0], match.participants[1]

            # skip if someone already confirmed
            if p0.confirmed or p1.confirmed:
                continue

            now = datetime.now().timestamp()
            if now - match.matched_time < 43200:
                continue

            await cancel_match(self.bot, match.token)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=10))
    async def match_confirmation_remind(self):
        matches = await Match.find().to_list()
        now = datetime.now().timestamp()

        for match in matches:
            p0, p1 = match.participants[0], match.participants[1]

            # skip if both confirmed or both unconfirmed (equal states)
            if bool(p0.confirmed) == bool(p1.confirmed):
                continue

            # Keep the reminder at three quarters of the confirmation window.
            if now - match.matched_time < 32400:
                continue

            # find the unconfirmed participant and remind once
            for part in (p0, p1):
                if part.confirmed:
                    continue

                user = await self.bot.fetch_user(int(part.user_id))

                # avoid duplicate reminders if the last DM already warned them
                dm = await user.fetch_dm(force=True)
                last = await dm.fetch_messages(limit=1)
                if last and last[0].content.startswith(f"{IMPORTANT_EMOJI} Please remember"):
                    break

                await user.send(
                    f"{IMPORTANT_EMOJI} Please remember to confirm your match, thank you!\n\n"
                    f"*Please cancel the match, if you do not have time to play.*"
                )
                break

    @ipy.component_callback("match_queue")
    async def match_queue_button(self, ctx: ipy.ComponentContext):
        await self.match_queue(ctx)

    @ipy.Task.create(ipy.IntervalTrigger(minutes=5))
    async def repair_match_publications(self) -> None:
        async with self._match_publication_repair_lock:
            matches = await Match.find({
                "result_operation.completed": True,
                "result_operation.published": False,
                "result_operation.cancelled": False,
            }).to_list()
            for match in matches:
                try:
                    await self._finalize_match_results(match.token)
                except (ipy.errors.HTTPException, PyMongoError, ResultOperationConflict):
                    logging.exception("Unable to retry result publication for match %s", match.token)
            await repair_match_messages(self.bot)

    @ipy.Task.create(ipy.IntervalTrigger(hours=6))
    async def match_data_clean_up(self):
        matches = await Match.find().to_list()

        for match in matches:
            if not match.thread:
                continue

            try:
                await self.bot.http.get_channel(int(match.thread))
            except ipy.errors.NotFound:
                pass
            except ipy.errors.HTTPException:
                logging.warning("Unable to confirm whether match %s's thread was deleted", match.token, exc_info=True)
                continue
            else:
                continue
            async with BASE_STORE_RESERVATION_LOCK:
                match = await Match.find_one({"_id": match.id, "thread": match.thread})
                if not match:
                    continue
                await clear_match_base_stores(match, reset_stored_flags=True, bot=self.bot, reason="thread_deleted")
                if not await delete_unreserved_match(match):
                    logging.warning("Match %s changed before missing-thread cleanup could finish", match.token)
                    continue
                await free_match_clans(match)

    @ipy.Task.create(ipy.CronTrigger(cron="0 6 * * 5"))  # one hour before raid weekend, Friday UTC
    async def raid_weekend_auto_store(self) -> None:
        await store_before_raid_weekend(self)

    @ipy.Task.create(ipy.CronTrigger(cron="0 7 * * 4"))  # triggers 7:00 AM every Thursday UTC
    async def raid_weekend_start_notify(self):
        matches = await Match.find().to_list()
        for match in matches:
            if not match.thread:
                continue

            # skip if match has ended
            if is_match_finished(match):
                continue

            thread = await self.bot.fetch_channel(match.thread)
            if not thread or "🔴" in getattr(thread, "name", ""):
                continue

            for participant in match.participants:
                # skip if opponent bases are already stored
                if participant.opponent_bases_stored:
                    continue

                # skip if all results already recorded
                if all(participant.result):
                    continue

                await thread.send(
                    f"<@{participant.user_id}>, raid weekend is starting soon! "
                    f"To play during the raid weekend, please inform the helpers to store bases for you."
                )

    @ipy.listen(ipy.events.MessageCreate)
    async def on_message_create(self, event: ipy.events.MessageCreate):
        if not isinstance(event.message.channel, ipy.ThreadChannel):
            return

        if int(event.message.channel.parent_channel.id) != MATCH_CHANNEL:
            return

        if f"<@&{STAFF_ROLE}>" not in event.message.content:
            return

        # find the match whose thread is this channel
        match = await Match.find_one(Match.thread == str(event.message.channel.id))
        if match is None:
            return

        author_id = str(event.message.author.id)
        if author_id not in {str(match.participants[0].user_id), str(match.participants[1].user_id)}:
            return

        await append_helper_task(match.thread, author_id)

    @ipy.Task.create(ipy.IntervalTrigger(seconds=15))
    async def update_clan_availability_embed(self):
        embed_ref = (await ServerState.current()).clan_availability_embed
        if not embed_ref:
            return

        channel = await self.bot.fetch_channel(embed_ref.channel)
        if not channel:
            return

        message = await channel.fetch_message(embed_ref.message)
        if not message:
            return
        loading = message.embeds[0].description in ("Loading…", "Loading...")
        if ((loading and not message.components) or
                any(marker in (message.embeds[0].title or "") for marker in ("`(Confirming)`", "`(Unavailable)`"))):
            return

        await sync_base_store_slots(bot=self.bot)
        matches = await Match.find().to_list()
        slot_owners = collect_base_store_slot_owners(matches)
        clans = await Clan.find_all().to_list()
        clan_availability = {c.tag: c.availability_str for c in clans}
        clan_map = {}
        for c in clans:
            clan_map[c.tag] = format_clan_availability_status(c, slot_owners)

        user_ids = list(dict.fromkeys(re.findall(r"<@!?(\d+)>", "\n".join(clan_map.values()))))
        members = await asyncio.gather(*(channel.guild.fetch_member(user_id) for user_id in user_ids))
        names = {user_id: mention_user(member, user_id) for user_id, member in zip(user_ids, members)}

        embed = copy.deepcopy(message.embeds[0])
        edited = loading
        if loading:
            embed.description = ""

        for field in embed.fields:
            # extract clan tag from field name like "**Clan Name (#TAG)**"
            clan_tag = field.name.split("(")[1].replace(")**", "").strip()
            if clan_tag not in clan_map:
                continue

            thread_id_match = re.search(r"<#(\d+)>", clan_availability[clan_tag])
            if thread_id_match:
                thread_id = thread_id_match.group(1)
                thread = await self.bot.fetch_channel(thread_id)
                if not thread:
                    await free_clan(clan_tag, expected_thread=thread_id)
                    clan = await Clan.find_one(Clan.tag == clan_tag)
                    clan_availability[clan_tag] = clan.availability_str
                    clan_map[clan_tag] = format_clan_availability_status(clan, slot_owners)

            value = re.sub(r"<@!?(\d+)>", lambda mention: names.get(mention.group(1), mention.group(0)),
                           clan_map[clan_tag])
            if field.value == value:
                continue

            field.value = value

            edited = True

        if edited:
            await message.edit(embed=embed_edit_payload(embed))

    @ipy.Task.create(ipy.IntervalTrigger(seconds=15))
    async def update_hosting_queue_embed(self):
        if self._hosting_queue_embed_update_lock.locked():
            return

        async with self._hosting_queue_embed_update_lock:
            embed_ref = (await ServerState.current()).hosting_queue_embed
            if not embed_ref:
                return

            channel = await self.bot.fetch_channel(embed_ref.channel)
            if not channel:
                return

            message = await channel.fetch_message(embed_ref.message)
            if not message:
                return
            if "`(Unavailable)`" in (message.embeds[0].title or ""):
                return

            tasks = await HelperTask.find().sort("-timestamp").to_list()
            tasks_description = ""

            for task in tasks:
                thread = await self.bot.fetch_channel(task.thread)
                if not thread:
                    await task.delete()
                    continue
                discord_timestamp = ipy.Timestamp.fromtimestamp(task.timestamp).format(ipy.TimestampStyles.RelativeTime)
                member = await channel.guild.fetch_member(task.player_id)
                tasks_description += f"<#{task.thread}> {mention_user(member, task.player_id)} {discord_timestamp}\n"

            embed = message.embeds[0]
            title = (embed.title or "").replace("`(Confirming)`", "")
            if embed.description != tasks_description or embed.title != title:
                embed.title = title
                embed.description = tasks_description
                await message.edit(embed=embed_edit_payload(embed))

    @match_base_submit.autocomplete("match")
    @match_base_view.autocomplete("match")
    async def final_base_match_autocomplete(self, ctx: ipy.AutocompleteContext):
        from common.finals.lifecycle import resolve_final
        try:
            final = await resolve_final()
            choices = [ipy.SlashCommandChoice(name=node.id, value=node.id) for node in final.nodes.values()
                       if node.match_id and ctx.guild_id == final.guild_id
                       and not ctx.author.has_role(ELO_BLACKLIST_ROLE)
                       and ctx.input_text.lower() in node.id.lower()]
        except FinalError:
            choices = []
        await ctx.send(choices=choices)

    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        await sync_base_store_slots(bot=self.bot)
        print("➤ Match commands loaded")

        is_main_bot = int(self.bot.user.id) == BOT_ID
        if is_main_bot:
            await self.inactive_threads_delete()
        await self.auto_match_confirm()
        await self.auto_match_cancel()
        await self.match_confirmation_remind()
        await self.base_notify()
        await self.host_notify()
        await self.match_data_clean_up()
        await self.repair_match_publications()
        await self.update_clan_availability_embed()
        await self.update_hosting_queue_embed()

        if is_main_bot:
            self.inactive_threads_delete.start()
        self.auto_match_confirm.start()
        self.auto_match_cancel.start()
        self.match_confirmation_remind.start()
        self.base_notify.start()
        self.host_notify.start()
        self.match_data_clean_up.start()
        self.repair_match_publications.start()
        self.raid_weekend_start_notify.start()
        self.raid_weekend_auto_store.start()
        self.update_clan_availability_embed.start()
        self.update_hosting_queue_embed.start()

        last_matching_timestamp = (await ServerState.current()).last_matching
        if not last_matching_timestamp:
            await self.create_matches()
        else:
            last_matching = datetime.fromtimestamp(last_matching_timestamp)
            now = datetime.now()
            diff = (now - last_matching).seconds
            if diff < 10800:
                await asyncio.sleep(10800 - diff)

            await self.create_matches()

        self.create_matches.start()

import asyncio
import copy
import re
from contextlib import nullcontext
from contextvars import ContextVar
from datetime import datetime

import interactions as ipy

from elobot.db import Clan, HelperTask, Match
from elobot.discord.decorators import create_async_task
from elobot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elobot.features.base_store.start import StoredStartState
from elobot.features.finals.types import is_final_match
from elobot.features.matches.controls import update_match_control
from elobot.features.matches.messages import MatchMessageState

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

from dataclasses import dataclass
from typing import Literal, Protocol

import coc
import interactions as ipy

from elo_bot.db import Match
from elo_bot.features.base_store.messages import BaseStoreReleaseReason


@dataclass(frozen=True, slots=True)
class BaseStorePreparationRequest:
    thread_id: str
    player: int
    clan: coc.Clan | None
    slot: str | None
    raid_autostore: bool = False


@dataclass(frozen=True, slots=True)
class PreparedBaseStore:
    match: Match
    player: int
    clan_tag: str
    slot: str


@dataclass(frozen=True, slots=True)
class BaseStorePreparationFailure:
    message: str


type BaseStorePreparationOutcome = PreparedBaseStore | BaseStorePreparationFailure


@dataclass(frozen=True, slots=True)
class StoredBaseControlState:
    match: Match
    player: int
    clan_tag: str
    slot: str
    numeric_slot: Literal[1, 2]
    districts: list[str]


@dataclass(frozen=True, slots=True)
class StoredBaseControlFailure:
    message: str


type StoredBaseControlOutcome = StoredBaseControlState | StoredBaseControlFailure


@dataclass(frozen=True, slots=True)
class SelfBaseStoreRequest:
    match: Match
    player: int
    helper_id: str | None = None  # when set, overrides the default (bot user id)
    raid_autostore: bool = False


@dataclass(frozen=True, slots=True)
class StoredBaseActionRequest:
    token: str
    player: int
    reservation_id: str | None = None
    message_id: int | None = None


@dataclass(frozen=True, slots=True)
class BaseStorePresentationRequest:
    match: Match
    player: int
    helper_id: str
    clan: coc.Clan
    bases_stored: bool = True
    failure_reason: str | None = None


class ReservationLease(Protocol):
    def release(self) -> None: ...


class BaseStoreRuntime(Protocol):
    bot: ipy.Client
    coc_client: coc.Client
    helper_api_base_url: str
    helper_api_key: str

    async def _base_store_sync_slots(self, clan_tag: str | None = None) -> dict[str, list[str]]: ...

    async def _base_store_choose_bases(self, match: Match, player: int) -> None: ...

    async def _base_store_clear_participant(
            self,
            match: Match,
            player: int,
            reset_stored_flag: bool = False,
            *,
            reason: BaseStoreReleaseReason,
    ) -> bool: ...

    async def _base_store_try_reserve_side(self, token: str, player: int) -> ReservationLease | None: ...

    async def _base_store_try_reserve_clan(self, clan_tag: str) -> ReservationLease | None: ...

    async def _base_store_is_clan_available(self, clan_tag: str) -> bool: ...

    async def _base_store_confirm_pending(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
    ) -> bool: ...

    async def _base_store_start_local(
            self,
            ctx: ipy.SlashContext | ipy.ComponentContext,
            state: StoredBaseControlState,
            clan: coc.Clan,
    ) -> ipy.Message | None: ...


def numeric_base_store_slot(slot: str) -> Literal[1, 2] | None:
    if slot == "Slot 1":
        return 1
    if slot == "Slot 2":
        return 2
    return None


def stored_base_matches(match: Match, request: StoredBaseActionRequest) -> bool:
    if request.player < 0 or request.player >= len(match.participants):
        return False
    store = match.participants[request.player].base_store
    return bool(
        store
        and store.reservation_id == request.reservation_id
        and (store.message_id is None or store.message_id == request.message_id)
    )


async def bind_legacy_store_message(match: Match, player: int, message_id: int | None) -> bool:
    store = match.participants[player].base_store
    if store and store.reservation_id is None and store.message_id is None and message_id is not None:
        return await match.attach_base_store_message(player, message_id, ready=store.ready is not False)
    return True

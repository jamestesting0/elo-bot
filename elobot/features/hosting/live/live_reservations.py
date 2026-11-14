from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from elobot.db import Match
from elobot.features.matches.timing import hosting_started


class MatchSideReservation(Protocol):
    def release(self) -> None: ...


class ClanReservation(Protocol):
    clan_tag: str

    def release(self) -> None: ...


type ReserveMatchSide = Callable[[str, int], Awaitable[MatchSideReservation | None]]
type ReserveClan = Callable[[str], Awaitable[ClanReservation | None]]
type FindMatch = Callable[[str], Awaitable[Match | None]]
type IsMatchFinished = Callable[[Match], bool]


@dataclass(frozen=True, slots=True)
class LiveReservationOperations:
    reserve_match_side: ReserveMatchSide
    reserve_clan: ReserveClan
    find_match: FindMatch
    is_match_finished: IsMatchFinished


@dataclass(frozen=True, slots=True)
class LiveStartReservations:
    side0: MatchSideReservation
    side1: MatchSideReservation
    clan: ClanReservation

    def release(self) -> None:
        self.clan.release()
        self.side1.release()
        self.side0.release()


@dataclass(frozen=True, slots=True)
class LiveReservationsBusy:
    pass


@dataclass(frozen=True, slots=True)
class LiveReservationsClanUnavailable:
    pass


type LiveReservationOutcome = LiveStartReservations | LiveReservationsBusy | LiveReservationsClanUnavailable


async def reserve_live_start(
        operations: LiveReservationOperations,
        match_token: str,
        clan_tag: str,
) -> LiveReservationOutcome:
    side0 = await operations.reserve_match_side(match_token, 0)
    if not side0:
        return LiveReservationsBusy()
    side1 = None
    clan = None
    keep_reservations = False
    try:
        side1 = await operations.reserve_match_side(match_token, 1)
        if not side1:
            return LiveReservationsBusy()
        clan = await operations.reserve_clan(clan_tag)
        if not clan:
            return LiveReservationsClanUnavailable()
        reservations = LiveStartReservations(side0=side0, side1=side1, clan=clan)
        fresh_match = await operations.find_match(match_token)
        if (
                fresh_match is None
                or operations.is_match_finished(fresh_match)
                or hosting_started(fresh_match)
        ):
            return LiveReservationsBusy()
        keep_reservations = True
        return reservations
    finally:
        if not keep_reservations:
            if clan is not None:
                clan.release()
            if side1 is not None:
                side1.release()
            side0.release()

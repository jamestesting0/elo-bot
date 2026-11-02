from dataclasses import dataclass

from beanie import PydanticObjectId
from pydantic import JsonValue

from common.matches.messages import MatchMessageState
from common.utils import latest_completed_raid_weekend_end_timestamp
from database_models import BaseStore, Match, Participant


def held_store(participant: Participant) -> BaseStore | None:
    store = participant.base_store
    if (
        store is None or not store.clan_tag or store.slot not in ("Slot 1", "Slot 2")
        or (store.stored_at is not None and store.stored_at <= latest_completed_raid_weekend_end_timestamp())
    ):
        return None
    return store


def ready_store(participant: Participant) -> BaseStore | None:
    store = held_store(participant)
    if store is None or store.ready is False:
        return None
    return store


def shared_store_clan(match: Match) -> str | None:
    stores = [ready_store(participant) for participant in match.participants[:2]]
    if len(stores) != 2 or any(store is None for store in stores):
        return None
    first, second = stores
    assert first is not None and second is not None
    if first.clan_tag != second.clan_tag or first.slot == second.slot:
        return None
    return first.clan_tag


@dataclass(frozen=True, slots=True)
class FreshWarStartState(MatchMessageState):
    def binding_filter(self) -> dict[str, JsonValue | PydanticObjectId]:
        query = dict(MatchMessageState.binding_filter(self))
        query[f"participants.{self.player}.base_store"] = None
        query[f"participants.{self.player}.opponent_bases_stored"] = {"$ne": True}
        return query


@dataclass(frozen=True, slots=True)
class StoredStartState(MatchMessageState):
    stores: tuple[tuple[int, BaseStore], ...] = ()

    @classmethod
    def capture_stores(cls, match: Match, players: tuple[int, ...]) -> "StoredStartState":
        stores = []
        for player in players:
            store = held_store(match.participants[player])
            assert store is not None
            stores.append((player, store.model_copy(deep=True)))
        state = MatchMessageState.capture(match, players[0])
        return cls(
            state.match_id, state.player, state.is_war, state.participants, state.times,
            state.hosts, state.host_clans, state.old_messages, state.final_identity, tuple(stores),
        )

    def binding_filter(self) -> dict[str, JsonValue | PydanticObjectId]:
        query = dict(MatchMessageState.binding_filter(self))
        for player, store in self.stores:
            query.update({
                f"participants.{player}.base_store.{key}": value
                for key, value in store.model_dump().items()
            })
            query[f"participants.{player}.user_id"] = self.participants[player]
            query[f"times.{player}"] = self.times[player]
            query[f"hosts.{player}"] = self.hosts[player]
        return query

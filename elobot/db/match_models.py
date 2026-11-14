from dataclasses import dataclass
from typing import List, Literal, Optional

from beanie import Document, PydanticObjectId
from pydantic import BaseModel, Field, JsonValue, conlist, field_validator, model_validator
from pymongo import DESCENDING, IndexModel

from ._state import match_slot_changes
from .result_models import MatchResultOperation
from .publication_models import MatchMessagePublication
from .final_models import FinalResultOperation
from elobot.features.finals.types import FINAL_DISTRICTS, MatchFormat


class BaseLink(BaseModel):
    link: str
    image: str
    expiration_date: Optional[float] = None
    base_quality: str | None = None


class BaseStore(BaseModel):
    clan_tag: str
    slot: str
    stored_at: Optional[int] = None
    reservation_id: str | None = None
    message_id: int | None = None
    ready: bool | None = None
    is_live: bool | None = None


class Participant(BaseModel):
    user_id: str
    format: MatchFormat = "ordinary_three"
    secondary_base_index: int | None = Field(default=None, ge=0, le=6)
    districts: list[str | None] = Field(
        default_factory=lambda: [None, None, None]
    )
    bases: list[BaseLink | None] = Field(
        default_factory=lambda: [None, None, None]
    )
    result: list[str | None] = Field(
        default_factory=lambda: [None, None, None]
    )
    confirmed: bool = False
    confirmed_at: float | None = None
    confirmation_method: Literal["ready", "manual", "automatic"] | None = None
    queue_attempt_id: str | None = None
    queue_districts_locked: bool = False
    submit_bases: Optional[bool] = False
    opponent_bases_stored: Optional[bool] = False
    base_store: Optional[BaseStore] = None

    @model_validator(mode="after")
    def validate_slot_count(self):
        expected = 7 if self.format == "elo_final_seven" else 3
        if any(len(values) != expected for values in (self.districts, self.bases, self.result)):
            raise ValueError("District, base and result slots must match the persisted match format.")
        if self.secondary_base_index is not None and self.secondary_base_index >= expected:
            raise ValueError("The secondary base index must identify a district slot.")
        return self


@dataclass(frozen=True, slots=True)
class ExpectedLiveStartState:
    times: tuple[int | None, int | None]
    hosts: tuple[str | None, str | None]
    host_clans: tuple[str | None, str | None]
    old_messages: tuple[int | None, int | None]


class Match(Document):
    token: str
    format: MatchFormat = "ordinary_three"
    final_id: str | None = None
    final_node_id: str | None = None
    final_contestant_revision: int | None = None
    final_attempt: int | None = None
    routing_channel_id: int | None = None
    routing_guild_id: int | None = None
    final_result_id: str | None = None
    final_result_operation: FinalResultOperation | None = None
    final_superseded: bool = False
    final_start_claim: str | None = None
    final_started_at: int | None = None
    final_started_times: list[int | None] = Field(default_factory=lambda: [None, None], min_length=2, max_length=2)
    final_hosting_times: list[int | None] = Field(default_factory=lambda: [None, None], min_length=2, max_length=2)
    final_legacy_hosting_evidence: list[bool] = Field(default_factory=lambda: [False, False], min_length=2, max_length=2)
    scheduled_times: list[int | None] = Field(default_factory=lambda: [None, None], min_length=2, max_length=2)
    final_thread_creation_claimed: bool = False
    final_base_page_revision: int = 0
    result_operation: MatchResultOperation | None = None
    recorded_match_id: str | None = None
    matched_time: float
    confirmation_tracking: bool = False
    is_war: bool = True
    is_challenge: bool = False
    challenge_activity_override: bool = False
    wizard_valley_available: bool = False
    capital_peak_available: bool = False
    is_autowin: bool = False
    autowin_winner_id: str | None = None
    autowin_reason: str | None = None
    autowin_actor_id: str | None = None
    autowin_completed: bool = False
    thread: Optional[str] = None
    thread_message_id: int | None = None
    msg: Optional[str] = None
    public_message_publication: MatchMessagePublication | None = None
    primary_base_quality: Optional[str] = None
    secondary_base_quality: Optional[str] = None
    base_submission: Optional[bool] = True
    submitters: conlist(Optional[str], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    hosts: conlist(Optional[str], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    host_clans: conlist(Optional[str], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    persistent_host_clans: conlist(Optional[str], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    times: conlist(Optional[int], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    old_msgs: conlist(Optional[int], min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    message_publications: conlist(MatchMessagePublication | None, min_length=2, max_length=2) = Field(
        default_factory=lambda: [None, None]
    )
    final_districts: list[str | None] = Field(
        default_factory=lambda: [None, None, None]
    )
    participants: List[Participant] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_format(self):
        expected = 7 if self.format == "elo_final_seven" else 3
        if len(self.final_districts) != expected or any(p.format != self.format for p in self.participants):
            raise ValueError("District slots and participants must match the persisted format.")
        if self.format == "elo_final_seven" and (
            tuple(self.final_districts) != FINAL_DISTRICTS or not self.final_id or not self.final_node_id
            or len(self.participants) != 2
        ):
            raise ValueError("A Final match needs its exact seven ordered districts and tournament identity.")
        return self

    @field_validator("message_publications", mode="before")
    @classmethod
    def normalize_message_publications(cls, value: JsonValue) -> JsonValue:
        """Recover numeric Mongo object slots without losing their participant identity."""
        if isinstance(value, dict) and value.keys() <= {"0", "1"}:
            return [value.get("0"), value.get("1")]
        return value

    def get_changes(self) -> dict[str, JsonValue | PydanticObjectId]:
        changes = super().get_changes()
        saved_state = self.get_saved_state()
        assert saved_state is not None
        return match_slot_changes(changes, saved_state)

    def base_store_filter(self, player: int) -> dict[str, JsonValue | PydanticObjectId]:
        participant = self.participants[player]
        query: dict[str, JsonValue | PydanticObjectId] = {
            "_id": self.id, f"participants.{player}.user_id": participant.user_id,
        }
        prefix = f"participants.{player}.base_store"
        if participant.base_store is None:
            query[prefix] = None
        else:
            query.update({f"{prefix}.{key}": value for key, value in participant.base_store.model_dump().items()})
        return query

    async def clear_base_store(self, player: int, reset_stored_flag: bool = False) -> bool:
        updates: dict[str, JsonValue] = {f"participants.{player}.base_store": None}
        if reset_stored_flag:
            updates[f"participants.{player}.opponent_bases_stored"] = False
        result = await self.get_pymongo_collection().update_one(self.base_store_filter(player), {"$set": updates})
        if result.matched_count == 0:
            return False
        self.participants[player].base_store = None
        if reset_stored_flag:
            self.participants[player].opponent_bases_stored = False
        self._sync_base_store_state(player, reset_stored_flag)
        return True

    async def attach_base_store_message(self, player: int, message_id: int, *, ready: bool = True) -> bool:
        store = self.participants[player].base_store
        if store is None:
            return False
        result = await self.get_pymongo_collection().update_one(
            self.base_store_filter(player),
            {"$set": {
                f"participants.{player}.base_store.message_id": message_id,
                f"participants.{player}.base_store.ready": ready,
            }},
        )
        if result.matched_count == 0:
            return False
        store.message_id = message_id
        store.ready = ready
        self._sync_base_store_state(player)
        return True

    def _sync_base_store_state(self, player: int, reset_stored_flag: bool = False) -> None:
        saved_state = self.get_saved_state()
        if saved_state is None:
            return
        store = self.participants[player].base_store
        saved_state["participants"][player]["base_store"] = store.model_dump() if store else None
        if reset_stored_flag:
            saved_state["participants"][player]["opponent_bases_stored"] = False

    async def ensure_persistent_host_clans(self) -> None:
        if "persistent_host_clans" in self.model_fields_set:
            return
        persistent_host_clans = list(self.host_clans or [None, None])
        await self.get_pymongo_collection().update_one(
            {"_id": self.id, "persistent_host_clans": {"$exists": False}},
            {"$set": {"persistent_host_clans": persistent_host_clans}},
        )
        self.persistent_host_clans = list(persistent_host_clans)
        self.model_fields_set.add("persistent_host_clans")

    async def ensure_final_hosting_times(self) -> None:
        """Atomically retain ambiguous legacy hosting evidence without creating a start commitment."""
        if self.format == "elo_final_seven" and "final_hosting_times" not in self.model_fields_set:
            evidence = [
                self.times[side] is not None and self.scheduled_times[side] is not None
                and bool(self.hosts[side] or self.host_clans[side])
                for side in (0, 1)
            ]
            def element(field: str) -> dict:
                return {"$arrayElemAt": [{"$ifNull": [f"${field}", [None, None]]}, "$$side"]}

            result = await self.get_pymongo_collection().update_one(
                {"_id": self.id, "final_hosting_times": {"$exists": False}},
                [{"$set": {
                    "final_hosting_times": [None, None],
                    "final_legacy_hosting_evidence": {"$map": {
                        "input": [0, 1], "as": "side", "in": {"$and": [
                            {"$ne": [element("times"), None]},
                            {"$ne": [element("scheduled_times"), None]},
                            {"$or": [{"$not": [{"$in": [element("hosts"), [None, ""]]}]},
                                     {"$not": [{"$in": [element("host_clans"), [None, ""]]}]}]},
                        ]},
                    }},
                }}],
            )
            if result.matched_count:
                self.final_legacy_hosting_evidence = evidence
                self.model_fields_set.add("final_legacy_hosting_evidence")
            else:
                row = await self.get_pymongo_collection().find_one(
                    {"_id": self.id}, {"final_legacy_hosting_evidence": 1},
                )
                if row is not None and "final_legacy_hosting_evidence" in row:
                    self.final_legacy_hosting_evidence = list(row["final_legacy_hosting_evidence"])
                    self.model_fields_set.add("final_legacy_hosting_evidence")

    async def abort_started_war_side(
            self,
            player: int,
            expected_time: int | None,
            expected_host: str | None,
            expected_host_clan: str | None,
            expected_old_message: int,
    ) -> bool:
        await self.ensure_persistent_host_clans()
        await self.ensure_final_hosting_times()
        query: dict[str, JsonValue | PydanticObjectId] = {
            "_id": self.id,
            "is_war": True,
            f"times.{player}": expected_time,
            f"hosts.{player}": expected_host,
            f"host_clans.{player}": expected_host_clan,
            f"old_msgs.{player}": expected_old_message,
        }
        updates: dict[str, JsonValue] = {
            f"times.{player}": None,
            f"hosts.{player}": None,
            f"host_clans.{player}": None,
        }
        if self.format == "elo_final_seven":
            query[f"final_hosting_times.{player}"] = self.final_hosting_times[player]
            updates[f"final_hosting_times.{player}"] = None
            if "final_legacy_hosting_evidence" in self.model_fields_set:
                query[f"final_legacy_hosting_evidence.{player}"] = self.final_legacy_hosting_evidence[player]
                updates[f"final_legacy_hosting_evidence.{player}"] = False
        result = await self.get_pymongo_collection().update_one(query, {"$set": updates})
        if result.matched_count == 0:
            return False
        self.times[player] = None
        self.hosts[player] = None
        self.host_clans[player] = None
        if self.format == "elo_final_seven":
            self.final_hosting_times[player] = None
            self.final_legacy_hosting_evidence[player] = False
        self._save_state()
        return True

    async def abort_started_live_match(self, expected: ExpectedLiveStartState) -> bool:
        await self.ensure_persistent_host_clans()
        await self.ensure_final_hosting_times()
        query: dict[str, JsonValue | PydanticObjectId] = {
            "_id": self.id,
            "is_war": False,
            "times": list(expected.times),
            "hosts": list(expected.hosts),
            "host_clans": list(expected.host_clans),
            "old_msgs": list(expected.old_messages),
        }
        updates: dict[str, JsonValue] = {
            "times": [None, None],
            "hosts": [None, None],
            "host_clans": [None, None],
        }
        if self.format == "elo_final_seven":
            query["final_hosting_times"] = list(self.final_hosting_times)
            updates["final_hosting_times"] = [None, None]
            if "final_legacy_hosting_evidence" in self.model_fields_set:
                query["final_legacy_hosting_evidence"] = list(self.final_legacy_hosting_evidence)
                updates["final_legacy_hosting_evidence"] = [False, False]
        result = await self.get_pymongo_collection().update_one(query, {"$set": updates})
        if result.matched_count == 0:
            return False
        self.times = [None, None]
        self.hosts = [None, None]
        self.host_clans = [None, None]
        if self.format == "elo_final_seven":
            self.final_hosting_times = [None, None]
            self.final_legacy_hosting_evidence = [False, False]
        self._save_state()
        return True

    class Settings:
        name = "matches"
        indexes = [
            "token",
            "thread",
            "participants.user_id",
            [("is_challenge", 1), ("matched_time", 1)],
        ]
        use_state_management = True


class Clan(Document):
    tag: str
    name: str
    availability_str: str
    available_slots: List[str] = Field(default_factory=list)

    class Settings:
        name = "clans"
        indexes = ["tag"]
        use_state_management = True


class HelperTask(Document):
    thread: str
    player_id: str
    timestamp: int

    class Settings:
        name = "helper_tasks"
        indexes = [
            "thread",
            "player_id",
            [("thread", 1), ("player_id", 1)],
            IndexModel([("timestamp", DESCENDING)], name="idx_timestamp"),
        ]
        use_state_management = True


__all__ = [
    "BaseLink", "BaseStore", "Participant", "ExpectedLiveStartState", "Match", "Clan", "HelperTask",
]

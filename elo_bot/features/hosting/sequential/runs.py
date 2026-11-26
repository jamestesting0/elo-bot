import time
from typing import Literal

from beanie import PydanticObjectId
from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.core.finals import FINAL_DISTRICTS, FinalError, is_final_match
from elo_bot.db import Clan, Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun, SequentialRunEvent
from elo_bot.features.hosting.sequential.policy import EVENT_LOG_LIMIT, HELPER_DISTRICT_NAMES, TRACKING_STALE_SECONDS
from elo_bot.features.matches.resources import is_resource_match_side_locked
from elo_bot.features.finals.persistence import transaction
from elo_bot.integrations.helper_api import HelperSequentialResult, HelperSequentialRoundPlayer


def event_push(kind: str, detail: str = "", actor_id: str | None = None) -> dict:
    event = SequentialRunEvent(at=time.time(), kind=kind, detail=detail, actor_id=actor_id)
    return {"$push": {"events": {"$each": [event.model_dump()], "$slice": -EVENT_LOG_LIMIT}}}


async def record_event(run: HelperSequentialRun, kind: str, detail: str = "", actor_id: str | None = None) -> None:
    await HelperSequentialRun.get_pymongo_collection().update_one(
        {"_id": run.id}, event_push(kind, detail, actor_id),
    )


async def transition(
        run: HelperSequentialRun, updates: dict, kind: str, detail: str = "", actor_id: str | None = None,
) -> bool:
    write = await HelperSequentialRun.get_pymongo_collection().update_one(
        {"_id": run.id, "open": True, "state": run.state},
        {"$set": updates, **event_push(kind, detail, actor_id)},
    )
    return write.matched_count == 1


async def open_regular_run(run: HelperSequentialRun) -> None:
    async def register(session: AsyncClientSession) -> None:
        matches = Match.get_pymongo_collection()
        runs = HelperSequentialRun.get_pymongo_collection()
        updated = await matches.update_one({
            "_id": run.match_id, "final_id": run.final_id, "final_node_id": run.final_node_id,
            "final_contestant_revision": run.final_contestant_revision, "final_attempt": run.final_attempt,
            "final_superseded": {"$ne": True}, "final_result_id": None, "final_result_operation": None,
            "final_replay_run_id": None,
        }, {"$set": {"final_last_helper_run_id": run.id}}, session=session)
        if not updated.matched_count:
            raise FinalError("The match changed or a district replay is running. Check `/match helper status`.")
        if run.previous_run_id is not None:
            latest = await runs.find_one({"match_id": run.match_id, "side": run.side},
                                         sort=[("created_at", -1)], session=session)
            if latest is None or latest["_id"] != run.previous_run_id or latest["open"]:
                raise FinalError("A newer Helper run exists. Use its progress message.")
        await runs.insert_one(run.model_dump(by_alias=True), session=session)

    await transaction(register)


async def close_run(
        run: HelperSequentialRun, reason: str, actor_id: str | None = None, updates: dict | None = None,
) -> bool:
    async def close(session=None) -> bool:
        write = await HelperSequentialRun.get_pymongo_collection().update_one(
            {"_id": run.id, "open": True},
            {"$set": {**(updates or {}), "state": "TERMINAL", "open": False, "close_reason": reason},
             **event_push("closed", reason, actor_id)},
            **({"session": session} if session is not None else {}),
        )
        if not write.matched_count:
            return False
        if run.replay_baseline is not None:
            matches = Match.get_pymongo_collection()
            row = await matches.find_one({"_id": run.match_id, "final_replay_run_id": run.id}, session=session)
            await matches.update_one(
                {"_id": run.match_id, "final_replay_run_id": run.id}, {"$set": {"final_replay_run_id": None}},
                session=session,
            )
            if run.replay_clan_claim:
                availability = "Available"
                if row is not None and not row.get("final_superseded") and row.get("final_result_id") is None:
                    sides = result_sides(run.side)
                    if not all(all(row["participants"][side]["result"]) for side in sides):
                        availability = run.replay_previous_clan_availability or availability
                await Clan.get_pymongo_collection().update_one(
                    {"tag": run.host_clan_tag, "availability_str": run.replay_clan_claim},
                    {"$set": {"availability_str": availability}}, session=session,
                )
        return True

    return await transaction(close) if run.replay_baseline is not None else await close()


def side_filter(side: int | None) -> dict:
    return {} if side is None else {"side": {"$in": [side, LIVE_SIDE]}}


async def open_runs(match_token: str, side: int | None = None) -> list[HelperSequentialRun]:
    return await HelperSequentialRun.find({"match_token": match_token, "open": True, **side_filter(side)}).to_list()


async def open_run_refusal(match: Match, side: int | None = None) -> str | None:
    if is_final_match(match) and isinstance(match.final_replay_run_id, PydanticObjectId):
        return "A district replay is running. Check it with `/match helper status`."
    if not match.is_war and match.times[0] and is_resource_match_side_locked(str(match.token), 1):
        return "Helper is setting up this live match. Try again when setup finishes."
    if is_final_match(match) and await open_runs(str(match.token), side):
        return ("Helper is running a sequential match here. Check it with `/match helper status`, "
                "resume it with `/match helper recover`, or cancel it first.")
    return None


async def latest_run(match_id: PydanticObjectId, side: int | None) -> HelperSequentialRun | None:
    return await HelperSequentialRun.find_one(
        {"match_id": match_id, **({} if side is None else {"side": side})}, sort=[("created_at", -1)],
    )


async def match_runs(match_id: PydanticObjectId, side: int) -> list[HelperSequentialRun]:
    return await HelperSequentialRun.find({"match_id": match_id, "side": side}).sort("created_at").to_list()


def original_run(run: HelperSequentialRun, history: list[HelperSequentialRun]) -> HelperSequentialRun:
    by_id = {earlier.id: earlier for earlier in history}
    while run.previous_run_id in by_id:
        run = by_id[run.previous_run_id]
    return run


def is_tracked(run: HelperSequentialRun, now: float) -> bool:
    return run.last_polled_at is not None and now - run.last_polled_at < TRACKING_STALE_SECONDS


def run_result(run: HelperSequentialRun) -> HelperSequentialResult | None:
    return HelperSequentialResult.model_validate(run.snapshot) if run.snapshot else None


def result_sides(side: int) -> tuple[int, ...]:
    return (0, 1) if side == LIVE_SIDE else (side,)


def district_states(result: HelperSequentialResult) -> dict[str, Literal["waiting", "in progress", "played"]]:
    assignments: dict[str, dict[str, HelperSequentialRoundPlayer]] = {}
    for record in result.completion.rounds:
        for key, code in record.districts.items():
            if (player := record.players.get(key)) is not None:
                assignments.setdefault(code, {})[key] = player

    required_players = {player.key for player in result.match.players}
    states: dict[str, Literal["waiting", "in progress", "played"]] = {}
    for code, players in assignments.items():
        completed = {key for key, player in players.items() if player.completed}
        if required_players and required_players <= completed:
            states[code] = "played"
        elif any(player.posted or player.completed for player in players.values()):
            states[code] = "in progress"
        else:
            states[code] = "waiting"
    return states


def played_district_codes(result: HelperSequentialResult) -> set[str]:
    return {code for code, state in district_states(result).items() if state == "played"}


def remaining_districts(match: Match, runs: list[HelperSequentialRun]) -> list[str]:
    played = set()
    for run in runs:
        if result := run_result(run):
            played.update(HELPER_DISTRICT_NAMES[code] for code in played_district_codes(result))
    sides = result_sides(runs[-1].side)
    return [
        district for index, district in enumerate(FINAL_DISTRICTS)
        if district not in played and not all(match.participants[side].result[index] for side in sides)
    ]

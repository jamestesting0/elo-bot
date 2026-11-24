import time

from beanie import PydanticObjectId

from elo_bot.core.finals import FINAL_DISTRICTS, is_final_match
from elo_bot.db import Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun, SequentialRunEvent
from elo_bot.features.hosting.sequential.policy import EVENT_LOG_LIMIT, HELPER_DISTRICT_NAMES, TRACKING_STALE_SECONDS
from elo_bot.features.matches.resources import is_resource_match_side_locked
from elo_bot.integrations.helper_api import HelperSequentialResult


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


async def close_run(
        run: HelperSequentialRun, reason: str, actor_id: str | None = None, updates: dict | None = None,
) -> bool:
    write = await HelperSequentialRun.get_pymongo_collection().update_one(
        {"_id": run.id, "open": True},
        {"$set": {**(updates or {}), "state": "TERMINAL", "open": False, "close_reason": reason},
         **event_push("closed", reason, actor_id)},
    )
    return write.matched_count == 1


def side_filter(side: int | None) -> dict:
    return {} if side is None else {"side": {"$in": [side, LIVE_SIDE]}}


async def open_runs(match_token: str, side: int | None = None) -> list[HelperSequentialRun]:
    return await HelperSequentialRun.find({"match_token": match_token, "open": True, **side_filter(side)}).to_list()


async def open_run_refusal(match: Match, side: int | None = None) -> str | None:
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


def is_tracked(run: HelperSequentialRun, now: float) -> bool:
    return run.last_polled_at is not None and now - run.last_polled_at < TRACKING_STALE_SECONDS


def run_result(run: HelperSequentialRun) -> HelperSequentialResult | None:
    return HelperSequentialResult.model_validate(run.snapshot) if run.snapshot else None


def result_sides(side: int) -> tuple[int, ...]:
    return (0, 1) if side == LIVE_SIDE else (side,)


def played_districts(result: HelperSequentialResult) -> set[str]:
    played = set()
    for record in result.completion.rounds:
        if record.players and all(player.completed for player in record.players.values()):
            played.update(HELPER_DISTRICT_NAMES[code] for code in record.districts.values())
    return played


def remaining_districts(match: Match, runs: list[HelperSequentialRun]) -> list[str]:
    played = set()
    for run in runs:
        if result := run_result(run):
            played |= played_districts(result)
    sides = result_sides(runs[-1].side)
    return [
        district for index, district in enumerate(FINAL_DISTRICTS)
        if district not in played and not all(match.participants[side].result[index] for side in sides)
    ]

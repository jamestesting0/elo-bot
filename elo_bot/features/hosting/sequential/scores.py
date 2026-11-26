from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.core.match_results import score_entry
from elo_bot.db import Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.hosting.sequential.policy import HELPER_DISTRICT_NAMES
from elo_bot.features.hosting.sequential.runs import event_push, result_sides
from elo_bot.integrations.helper_api import HelperSequentialOutcome, HelperSequentialResult


def outcome_side(run: HelperSequentialRun, outcome: HelperSequentialOutcome) -> int:
    # Requests always send participant 0 as Helper's player_1, so the key fixes the side.
    return run.side if run.side != LIVE_SIDE else int(outcome.player == "player_2")


def outcome_entry(outcome: HelperSequentialOutcome) -> str | None:
    if outcome.status != "verified" or outcome.score is None:
        return None
    return score_entry(outcome.score.stars_gained, outcome.score.destruction_gained)


def storable_entries(run: HelperSequentialRun, result: HelperSequentialResult) -> dict[tuple[str, int], str]:
    entries = {}
    players = {"player_1", "player_2"} if run.side == LIVE_SIDE else {"player_1"}
    for outcome in result.score_outcomes:
        district = HELPER_DISTRICT_NAMES.get(outcome.district)
        entry = outcome_entry(outcome)
        if district in FINAL_DISTRICTS and entry is not None and outcome.player in players:
            entries[(district, outcome_side(run, outcome))] = entry
    if run.side == LIVE_SIDE:
        # A live district is stored only as a pair, so a replayed half-finished district never overwrites a score.
        entries = {key: entry for key, entry in entries.items() if (key[0], 1 - key[1]) in entries}
    return entries


async def store_verified_scores(run: HelperSequentialRun, result: HelperSequentialResult) -> list[str]:
    stored = []
    for (district, side), entry in storable_entries(run, result).items():
        slot = f"participants.{side}.result.{FINAL_DISTRICTS.index(district)}"
        write = await Match.get_pymongo_collection().update_one(
            {
                "_id": run.match_id,
                "final_contestant_revision": run.final_contestant_revision,
                "final_attempt": run.final_attempt,
                "final_superseded": {"$ne": True},
                "final_result_id": None,
                "final_result_operation": None,
                slot: {"$in": [None, ""]},
            },
            {"$set": {slot: entry}},
        )
        if write.modified_count:
            stored.append(f"{district} P{side + 1} {entry}")
    return stored


async def store_replay_scores(run: HelperSequentialRun, result: HelperSequentialResult) -> None:
    entries = storable_entries(run, result)
    sides = result_sides(run.side)
    verified = {district: [entries[district, side] for side in sides] for district in run.districts
                if all((district, side) in entries for side in sides)}
    if not verified.keys() - run.replay_replacements.keys() - set(run.replay_conflicts):
        return

    async def store(session) -> None:
        runs = HelperSequentialRun.get_pymongo_collection()
        matches = Match.get_pymongo_collection()
        row = await runs.find_one({"_id": run.id, "open": True}, session=session)
        if row is None:
            return
        current = HelperSequentialRun.model_validate(row)
        assert current.replay_baseline is not None
        for district, scores in verified.items():
            if district in current.replay_replacements or district in current.replay_conflicts:
                continue
            index = FINAL_DISTRICTS.index(district)
            slots = [f"participants.{side}.result.{index}" for side in sides]
            before = current.replay_baseline[district]
            changes: dict = {"$set": dict(zip(slots, scores, strict=True))}
            if before != scores:
                changes["$inc"] = {"final_replay_revision": 1}
            write = await matches.update_one({
                "_id": run.match_id, "final_replay_run_id": run.id,
                "final_contestant_revision": run.final_contestant_revision, "final_attempt": run.final_attempt,
                "final_superseded": {"$ne": True}, "final_result_id": None, "final_result_operation": None,
                **dict(zip(slots, before, strict=True)),
            }, changes, session=session)
            if write.matched_count:
                old = "/".join(score or "–" for score in before)
                detail = f"{district} {old} → {'/'.join(scores)}"
                update = {"$set": {f"replay_replacements.{district}": scores},
                          **event_push("scores_replayed", detail, run.requested_by)}
            else:
                update = event_push("replay_scores_kept", district, run.requested_by)
                update["$push"]["replay_conflicts"] = district
            await runs.update_one({"_id": run.id, "open": True}, update, session=session)

    await transaction(store)

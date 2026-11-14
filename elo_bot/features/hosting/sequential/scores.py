from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.core.match_results import score_entry
from elo_bot.db import Match
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.hosting.sequential.policy import HELPER_DISTRICT_NAMES
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
    for outcome in result.score_outcomes:
        district = HELPER_DISTRICT_NAMES.get(outcome.district)
        entry = outcome_entry(outcome)
        if district in FINAL_DISTRICTS and entry is not None:
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

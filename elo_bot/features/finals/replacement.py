from dataclasses import dataclass

from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.core.finals import FINAL_DISTRICTS, FinalError, ROUND_NODES
from elo_bot.db import EloFinal, Participant, Player
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.match_models import Match
from elo_bot.db.season_models import EloFinalMatch
from elo_bot.features.finals.bracket import advance_nodes
from elo_bot.features.finals.lifecycle import ongoing_final
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.profiles import rebuild_profiles

STARTED = "Their match has started. Use `/final match forfeit`."


@dataclass(frozen=True, slots=True)
class Replacement:
    final: EloFinal
    node_id: str
    # The pairing's existing thread, which now belongs to the new pair.
    thread_id: str | None
    # The pairing already had a match, so its thread and betting must be brought up to date.
    round_started: bool


def replaceable_entrants(final: EloFinal) -> list[str]:
    """Quarterfinal entrants whose first match has not started."""
    if not final.active or final.state not in {"BRACKET_READY", "RUNNING"}:
        return []
    users = []
    for node_id in ROUND_NODES["Quarterfinals"]:
        node = final.nodes[node_id]
        first = next_node(final, node) if node.bye else node
        if first is not None and not _started(first):
            users += [user for user in node.contestants if user]
    return users


def next_node(final: EloFinal, node: EloFinalMatch) -> EloFinalMatch | None:
    return next((candidate for candidate in final.nodes.values() if node.id in candidate.feeders), None)


def _started(node: EloFinalMatch) -> bool:
    return bool(node.result_id or node.first_actual_started_at or node.actual_started_at or node.start_claim)


async def _source(node: EloFinalMatch, session: AsyncClientSession) -> Match | None:
    if node.match_id is None:
        return None
    row = await Match.get_pymongo_collection().find_one({"_id": node.match_id}, session=session)
    return Match.model_validate(row) if row else None


def _source_started(source: Match) -> bool:
    return bool(source.final_started_at or any(source.final_started_times)
                or any(any(participant.result) for participant in source.participants))


def _reset(node: EloFinalMatch) -> None:
    node.contestant_revision += 1
    node.match_id = node.thread_id = node.start_claim = node.provisioning_error = None
    node.provisioning = False
    node.scheduled_times = [None, None]
    node.scheduled_is_war = None
    node.state = "READY" if all(node.contestants) else "WAITING"


async def replace_entrant(final: EloFinal, leaving: str, joining: str, name: str) -> Replacement:
    async def replace(session) -> Replacement:
        current = await ongoing_final(final.id, session=session)
        if leaving not in replaceable_entrants(current):
            raise FinalError(STARTED if leaving in current.entrant_ids else "Only quarterfinal entrants can be replaced.")
        if joining in current.entrant_ids:
            raise FinalError("The replacement is already an entrant.")
        player = await Player.find_one({"user_id": joining}, session=session)
        if player is None:
            raise FinalError("The replacement is not registered.")
        node = next(current.nodes[key] for key in ROUND_NODES["Quarterfinals"]
                    if leaving in current.nodes[key].contestants)
        first = next_node(current, node) if node.bye else node
        source = await _source(first, session)
        if source and _source_started(source):
            raise FinalError(STARTED)
        if source and any(participant.base_store or participant.opponent_bases_stored
                           for participant in source.participants):
            raise FinalError("Undo the bases stored for this match first.")
        kept = first.model_copy(deep=True)
        node.contestants[node.contestants.index(leaving)] = joining
        if node.bye:
            node.winner = joining
            advance_nodes(current.nodes)
        else:
            _reset(node)
        matches = Match.get_pymongo_collection()
        if source and kept.thread_id:
            # Keep the pairing's match and thread; only the player and the revision change.
            for field in ("match_id", "thread_id", "scheduled_times", "scheduled_is_war", "state"):
                setattr(first, field, getattr(kept, field))
            side = [participant.user_id for participant in source.participants].index(leaving)
            participant = Participant(user_id=joining, format="elo_final_seven", districts=list(FINAL_DISTRICTS),
                                      bases=[None] * 7, result=[None] * 7, confirmed=True, submit_bases=True)
            await matches.update_one({"_id": source.id}, {"$set": {
                f"participants.{side}": participant.model_dump(),
                "final_contestant_revision": first.contestant_revision}}, session=session)
        elif source:
            await matches.update_one({"_id": source.id}, {"$set": {"final_superseded": True}}, session=session)
        current.entrant_ids[current.entrant_ids.index(leaving)] = joining
        current.player_nicknames.pop(leaving, None)
        current.player_nicknames[joining] = name.strip()[:80] or joining
        current.seed_elo.pop(leaving, None)
        current.seed_elo[joining] = player.elo
        current.nickname_overrides = [user for user in current.nickname_overrides if user != leaving]
        current.revision += 1
        current.publication_pending = True
        await EloFinal.get_pymongo_collection().replace_one({"_id": current.id}, current.model_dump(by_alias=True),
                                                           session=session)
        predictions = FinalPrediction.get_pymongo_collection()
        async for row in predictions.find({"final_id": str(current.id), "$or": [
                {"draft": {"$exists": True}}, {"submitted": {"$exists": True}}]}, session=session):
            draft = {key: joining if value == leaving else value for key, value in row.get("draft", {}).items()}
            submitted = {key: joining if value == leaving else value for key, value in row.get("submitted", {}).items()}
            if draft != row.get("draft", {}) or submitted != row.get("submitted", {}):
                await predictions.update_one({"_id": row["_id"]}, {"$set": {"draft": draft, "submitted": submitted}},
                                             session=session)
        await rebuild_profiles(current, session)
        return Replacement(current, first.id, first.thread_id, source is not None)
    return await transaction(replace)

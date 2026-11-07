import time
from dataclasses import dataclass
from hashlib import sha256

from beanie import PydanticObjectId

from common.finals.bracket import advance_nodes, require_round_ready
from common.finals.persistence import transaction
from common.finals.matches import require_current_match
from common.finals.profiles import rebuild_profiles
from common.finals.predictions import rescore_predictions
from common.finals.types import FinalError, Outcome, ROUND_NODES
from common.utils import get_destruction, get_stars
from database_models import EloFinal, Match, Season, HelperHistory
from database_models.final_models import FinalHelperCredit, FinalMatchResult, FinalResultOperation, FinalResultSnapshot
from database_models.season_models import FinalResultProgress


@dataclass(frozen=True, slots=True)
class FinalResultSubmission:
    final_id: PydanticObjectId
    node_id: str
    actor_id: str
    operation_key: str
    expected_version: int
    contestant_revision: int
    attempt: int
    scores: dict[str, list[str]]
    winner: str | None = None
    outcome: Outcome = "Played"
    reason: str | None = None


def score_winner(contestants: list[str], scores: dict[str, list[str]]) -> str | None:
    totals = [(sum(get_stars(score) for score in scores[user]),
               sum(get_destruction(score) for score in scores[user])) for user in contestants]
    if totals[0] == totals[1]:
        return None
    return contestants[0] if totals[0] > totals[1] else contestants[1]


def submission_snapshot(final: EloFinal, request: FinalResultSubmission) -> FinalResultSnapshot:
    node = final.nodes.get(request.node_id)
    if node is None or node.bye or not all(node.contestants):
        raise FinalError("This match has no playable pairing.")
    if (node.result_version != request.expected_version or node.contestant_revision != request.contestant_revision
            or node.attempt != request.attempt):
        raise FinalError("The match changed. Open its current result form.")
    if final.state in {"CANCELLING", "CANCELLED"} or final.repair_pending:
        raise FinalError("Final updates are pending. Use `/final recover` before changing results.")
    if final.prediction_state == "OPEN":
        raise FinalError("Close predictions with `/final predictions close` first.")
    if node.result_id is None:
        require_round_ready(final, next(name for name, nodes in ROUND_NODES.items() if node.id in nodes))
    if (node.result_id is not None or request.outcome == "Forfeit" or request.winner is not None) and not request.reason:
        raise FinalError("Enter a reason for a correction, tie decision or forfeit.")
    contestants = [user for user in node.contestants if user is not None]
    FinalResultSnapshot(contestants=contestants, scores=request.scores, winner=request.winner, outcome=request.outcome)
    winner = score_winner(contestants, request.scores) if request.outcome == "Played" else request.winner
    if request.outcome == "Played" and request.winner is not None:
        if winner is not None and winner != request.winner:
            raise FinalError("The selected winner conflicts with the scores.")
        winner = request.winner
    return FinalResultSnapshot(contestants=contestants, scores=request.scores, winner=winner, outcome=request.outcome)


async def submit_result(request: FinalResultSubmission) -> FinalMatchResult:
    async def commit(session):
        collection = EloFinal.get_pymongo_collection()
        row = await collection.find_one({"_id": request.final_id}, session=session)
        if row is None:
            raise FinalError("The Final is unavailable.")
        final = EloFinal.model_validate(row)
        node = final.nodes.get(request.node_id)
        if node is None or node.bye or not all(node.contestants):
            raise FinalError("This match has no playable pairing.")
        existing = await FinalMatchResult.get_pymongo_collection().find_one(
            {"operation_key": request.operation_key}, session=session)
        if existing:
            result = FinalMatchResult.model_validate(existing)
            if (result.final_id != str(final.id) or result.node_id != node.id or result.snapshot.scores != request.scores
                    or result.contestant_revision != request.contestant_revision or result.attempt != request.attempt
                    or result.actor_id != request.actor_id or result.snapshot.outcome != request.outcome
                    or result.reason != request.reason or node.result_id != result.id
                    or request.winner is not None and request.winner != result.snapshot.winner):
                raise FinalError("This submission is superseded or belongs to another action. Refresh the result.")
            return result
        snapshot = submission_snapshot(final, request)
        contestants, winner = snapshot.contestants, snapshot.winner
        previous = await FinalMatchResult.get_pymongo_collection().find_one({"_id": node.result_id}, session=session) \
            if node.result_id else None
        prior = FinalMatchResult.model_validate(previous) if previous else None
        if prior and (prior.final_id != str(final.id) or prior.node_id != node.id
                      or prior.contestant_revision != node.contestant_revision or prior.attempt != node.attempt
                      or prior.snapshot.contestants != snapshot.contestants):
            raise FinalError("The earlier official result belongs to another pairing or attempt.")
        source_row = await Match.get_pymongo_collection().find_one({"_id": node.match_id}, session=session) \
            if node.match_id else None
        source = Match.model_validate(source_row) if source_row else None
        if source:
            require_current_match(final, source)
        if snapshot.outcome == "Played":
            if prior and prior.snapshot.outcome == "Played":
                snapshot.scheduled_times = list(prior.snapshot.scheduled_times)
                snapshot.actual_started_at = prior.snapshot.actual_started_at
            else:
                if not all(node.scheduled_times) or node.match_id is None:
                    raise FinalError("Schedule both sides of this Final match before recording a played result.")
                snapshot.scheduled_times = list(node.scheduled_times)
                snapshot.actual_started_at = source.final_started_at if source else node.actual_started_at
        version = node.result_version + 1
        result = FinalMatchResult(
            id=f"{final.id}:{node.id}:{version}", final_id=str(final.id), season_number=final.season_number,
            node_id=node.id, contestant_revision=node.contestant_revision, attempt=node.attempt, version=version,
            operation_key=request.operation_key, actor_id=request.actor_id, reason=request.reason,
            previous_id=node.result_id, created_at=int(time.time()), snapshot=snapshot,
            source_match_id=str(source.id) if source else (str(node.match_id) if node.match_id else None),
            source_token=source.token if source else None, source_thread=source.thread if source else node.thread_id,
            bases={participant.user_id: [base.link if base else None for base in participant.bases]
                   for participant in source.participants} if source else {},
        )
        if source:
            ids = [participant.user_id for participant in source.participants]
            if not source.is_war and source.hosts[0] and (source.matched_time or source.times[0]):
                result.helper_credits.append(FinalHelperCredit(user_id=source.hosts[0],
                    receipt=f"result:{source.id}:host:live", points=3,
                    history=HelperHistory(time=int(source.matched_time or source.times[0]), player1=ids[0], player2=ids[1])))
            elif source.is_war:
                for side, user in enumerate(ids):
                    if source.hosts[side] and source.times[side]:
                        result.helper_credits.append(FinalHelperCredit(user_id=source.hosts[side],
                            receipt=f"result:{source.id}:host:{side}", points=0.5,
                            history=HelperHistory(time=int(source.times[side]), player1=user, player2=None)))
                    if request.outcome == "Played" and source.submitters[side]:
                        result.helper_credits.append(FinalHelperCredit(user_id=str(source.submitters[side]),
                            receipt=f"result:{source.id}:submitter:{side}", points=0.5))
        await FinalMatchResult.get_pymongo_collection().insert_one(result.model_dump(by_alias=True), session=session)
        old_nodes = {key: value.model_copy(deep=True) for key, value in final.nodes.items()}
        node.result_id, node.result_version, node.winner, node.outcome = result.id, version, winner, request.outcome
        node.state = "DECIDED" if winner else "TIED"
        changed = advance_nodes(final.nodes)
        for node_id in changed:
            old = old_nodes[node_id]
            if old.match_id:
                await Match.get_pymongo_collection().update_one({"_id": old.match_id},
                    {"$set": {"final_superseded": True}}, session=session)
        final.champion = final.nodes["F1"].winner
        final.result_revision += 1
        final.predictions_scoring_revision = final.result_revision
        final.revision += 1
        final.repair_pending = True
        final.repair_nodes = list(dict.fromkeys([node.id, *changed]))
        final.publication_pending = True
        final.result_progress[result.id.replace(".", "_")] = FinalResultProgress()
        if not final.active and changed:
            other = await collection.find_one({"active": True, "_id": {"$ne": final.id}}, session=session)
            if other:
                final.reopening_pending = True
            else:
                final.active = True
                final.state = "RUNNING"
        await collection.replace_one({"_id": final.id}, final.model_dump(by_alias=True), session=session)
        await rebuild_profiles(final, session)
        await rescore_predictions(final, session)
        if source:
            operation = FinalResultOperation(operation_key=request.operation_key, result_id=result.id, version=version,
                                             before=FinalResultSnapshot.model_validate(previous["snapshot"]) if previous else None,
                                             after=snapshot)
            updates = {"final_result_id": result.id, "final_result_operation": operation.model_dump()}
            for side, participant in enumerate(source.participants):
                updates[f"participants.{side}.result"] = snapshot.scores[participant.user_id]
            await Match.get_pymongo_collection().update_one({"_id": source.id}, {"$set": updates}, session=session)
        if final.completed_at is not None:
            await Season.get_pymongo_collection().update_one({"_id": final.season_id}, {"$set": {"final_summary": {
                "final_id": str(final.id), "champion": final.champion, "revision": final.result_revision,
            }}}, session=session)
        return result

    return await transaction(commit)


def automatic_operation_key(source: Match, scores: dict[str, list[str]]) -> str:
    digest = sha256(repr(sorted(scores.items())).encode()).hexdigest()
    return f"final-auto:{source.id}:{source.final_contestant_revision}:{source.final_attempt}:{digest}"

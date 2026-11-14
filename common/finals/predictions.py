import time
from dataclasses import dataclass
from typing import Literal, assert_never

from beanie import PydanticObjectId
from pymongo.asynchronous.client_session import AsyncClientSession

from common.finals.persistence import transaction
from common.finals.types import FinalError, NODE_IDS
from elobot.db import EloFinal, Player
from elobot.db.final_models import FinalPrediction


@dataclass(frozen=True, slots=True)
class PredictionAction:
    final_id: PydanticObjectId
    user_id: str
    session_revision: int
    action: Literal["pick", "next", "back", "submit"]
    node_id: str | None = None
    choice: str | None = None


def pick_options(final: EloFinal, picks: dict[str, str], node_id: str) -> list[str]:
    node = final.nodes[node_id]
    if not node.feeders:
        return [user for user in node.contestants if user is not None]
    return [picks[feeder] for feeder in node.feeders if feeder in picks]


def normalize_picks(final: EloFinal, picks: dict[str, str]) -> dict[str, str]:
    normalized = {node: user for node, user in picks.items() if node in final.nodes}
    for node_id in NODE_IDS:
        node = final.nodes[node_id]
        if node.bye:
            normalized[node_id] = node.winner
        elif (node.feeders and any(feeder not in normalized for feeder in node.feeders)
              or normalized.get(node_id) not in pick_options(final, normalized, node_id)):
            normalized.pop(node_id, None)
    return normalized


async def change_prediction_state(final: EloFinal, open_predictions: bool) -> EloFinal:
    async def change(session):
        collection = EloFinal.get_pymongo_collection()
        current = EloFinal.model_validate(await collection.find_one({"_id": final.id}, session=session))
        if not current.active or not current.nodes or current.state in {"CANCELLING", "CANCELLED"}:
            raise FinalError("Freeze an active bracket before managing predictions.")
        if open_predictions:
            if current.prediction_state == "CLOSED" or current.first_actual_started_at or any(
                not node.bye and node.result_id for node in current.nodes.values()
            ):
                raise FinalError("Predictions cannot open after closure, play or an official result.")
            current.prediction_state = "OPEN"
            current.predictions_ever_opened = True
        else:
            current.prediction_state = "CLOSED"
        current.revision += 1
        await collection.replace_one({"_id": current.id}, current.model_dump(by_alias=True), session=session)
        return current

    return await transaction(change)


async def begin_prediction(final: EloFinal, user_id: str) -> FinalPrediction:
    async def begin(session):
        current = EloFinal.model_validate(await EloFinal.get_pymongo_collection().find_one({"_id": final.id}, session=session))
        if current.prediction_state != "OPEN" or not current.active:
            raise FinalError("Predictions are closed.")
        if await Player.get_pymongo_collection().find_one({"user_id": user_id}, session=session) is None:
            raise FinalError("Register before predicting.")
        collection = FinalPrediction.get_pymongo_collection()
        row = await collection.find_one({"final_id": str(final.id), "user_id": user_id}, session=session)
        prediction = FinalPrediction.model_validate(row) if row else FinalPrediction(
            id=f"{final.id}:{user_id}", final_id=str(final.id), season_number=current.season_number,
            user_id=user_id, setup_revision=current.setup_revision,
        )
        prediction.draft = normalize_picks(current, prediction.draft or prediction.submitted)
        prediction.session_revision += 1
        await collection.replace_one({"_id": prediction.id}, prediction.model_dump(by_alias=True), upsert=True, session=session)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}}, session=session)
        return prediction

    return await transaction(begin)


async def apply_prediction_action(request: PredictionAction) -> FinalPrediction:
    async def apply(session):
        final_row = await EloFinal.get_pymongo_collection().find_one({"_id": request.final_id}, session=session)
        row = await FinalPrediction.get_pymongo_collection().find_one(
            {"final_id": str(request.final_id), "user_id": request.user_id}, session=session)
        if final_row is None or row is None:
            raise FinalError("Open `/final predict` to start a prediction draft.")
        final = EloFinal.model_validate(final_row)
        prediction = FinalPrediction.model_validate(row)
        if (final.prediction_state != "OPEN" or not final.active or prediction.setup_revision != final.setup_revision
                or prediction.session_revision != request.session_revision):
            raise FinalError("Predictions closed or this draft changed. Use `/final predict` to resume.")
        if await Player.get_pymongo_collection().find_one({"user_id": request.user_id}, session=session) is None:
            raise FinalError("Register before submitting predictions.")
        match request.action:
            case "pick":
                if request.node_id not in final.nodes or request.choice not in pick_options(final, prediction.draft, request.node_id):
                    raise FinalError("Choose a contestant from this pairing.")
                prediction.draft[request.node_id] = request.choice
                prediction.draft = normalize_picks(final, prediction.draft)
            case "back":
                prediction.stage = max(0, prediction.stage - 1)
            case "next":
                required = (NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:], NODE_IDS)[prediction.stage]
                if any(node not in prediction.draft for node in required):
                    raise FinalError("Choose each winner on this page before continuing.")
                prediction.stage = min(3, prediction.stage + 1)
            case "submit":
                prediction.draft = normalize_picks(final, prediction.draft)
                if prediction.stage != 3 or set(prediction.draft) != set(NODE_IDS):
                    raise FinalError("Complete every pairing and review the bracket before submitting.")
                prediction.submitted = dict(prediction.draft)
                prediction.submitted_at = int(time.time())
            case unreachable:
                assert_never(unreachable)
        prediction.session_revision += 1
        await FinalPrediction.get_pymongo_collection().replace_one({"_id": prediction.id}, prediction.model_dump(by_alias=True),
                                                                  session=session)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}}, session=session)
        return prediction

    return await transaction(apply)


async def rescore_predictions(final: EloFinal, session: AsyncClientSession) -> None:
    collection = FinalPrediction.get_pymongo_collection()
    eligible = {node.id: node.winner for node in final.nodes.values()
                if not node.bye and node.outcome == "Played" and node.winner is not None and node.result_id is not None}
    rows = await collection.find({"final_id": str(final.id), "submitted_at": {"$ne": None}}, session=session).to_list()
    for row in rows:
        correct = {node: row["submitted"].get(node) == winner for node, winner in eligible.items()}
        await collection.update_one({"_id": row["_id"]}, {"$set": {
            "correct": correct, "eligible": len(eligible), "points": sum(correct.values()),
            "scoring_revision": final.result_revision,
        }}, session=session)

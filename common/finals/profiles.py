from pymongo.asynchronous.client_session import AsyncClientSession

from common.finals.types import FINAL_DISTRICTS
from common.utils import get_destruction, get_stars
from database_models import EloFinal, Player
from database_models.final_models import FinalEditionStats, FinalMatchResult, FinalPlayerProfile
from database_models.token_models import TokenWallet


async def rebuild_profiles(final: EloFinal, session: AsyncClientSession) -> None:
    result_ids = [node.result_id for node in final.nodes.values() if node.result_id]
    results = await FinalMatchResult.get_pymongo_collection().find({"_id": {"$in": result_ids}},
                                                                   session=session).to_list()
    for user in final.entrant_ids:
        stats = FinalEditionStats(season_number=final.season_number, revision=final.result_revision)
        stats.byes = sum(node.bye and node.winner == user for node in final.nodes.values())
        for row in results:
            result = FinalMatchResult.model_validate(row)
            snapshot = result.snapshot
            if user not in snapshot.contestants:
                continue
            if snapshot.outcome == "Forfeit":
                stats.forfeits += 1
                continue
            scores = snapshot.scores[user]
            stats.played += 1
            stats.district_count += len(FINAL_DISTRICTS)
            stats.stars += sum(get_stars(score) for score in scores)
            stats.destruction += sum(get_destruction(score) for score in scores)
            stats.wins += int(snapshot.winner == user)
            stats.losses += int(snapshot.winner is not None and snapshot.winner != user)
        if final.champion == user:
            stats.placement = "Champion"
        else:
            for node in reversed(list(final.nodes.values())):
                if node.winner and user in node.contestants and node.winner != user:
                    stats.placement = {"F1": "Runner-up", "SF1": "Semifinalist", "SF2": "Semifinalist"}.get(
                        node.id, "Quarterfinalist")
                    break
        wallet = await TokenWallet.get_pymongo_collection().find_one({"identity_claims": user}, session=session)
        owner = wallet["user_id"] if wallet else user
        player = await Player.get_pymongo_collection().find_one({"user_id": owner}, session=session)
        await FinalPlayerProfile.get_pymongo_collection().update_one({"user_id": owner}, {
            "$set": {f"editions.{final.id}": stats.model_dump()},
            "$setOnInsert": {"_id": wallet["_id"] if wallet else user, "user_id": owner, "player_id": str(player["_id"]) if player else user},
        }, upsert=True, session=session)

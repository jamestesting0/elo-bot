import time

from elobot.features.finals.persistence import transaction
from elobot.core.finals import FinalError
from elobot.db import EloFinal, Player
from elobot.db.final_models import FinalMatchResult, FinalPlayerProfile, FinalPrediction
from elobot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet


async def guard_full_reset() -> None:
    if (await EloFinal.find_one({"$or": [{"active": True}, {"repair_pending": True}, {"reopening_pending": True}]})
            or await FinalBettingMarket.find_one({"$or": [{"escrow": {"$ne": 0}},
                {"state": {"$in": ["SETTLING", "CORRECTING"]}}]})):
        raise FinalError("Finish or cancel the ongoing Final and recover its payouts before resetting data.")


async def guard_deregistration(user_id: str) -> None:
    if await EloFinal.find_one({"active": True, "entrant_ids": user_id}):
        raise FinalError("Remove this entrant during registration, or finish/cancel the Final before deregistration.")


async def deregister_player(user_id: str) -> None:
    async def remove(session):
        collection = EloFinal.get_pymongo_collection()
        active = await collection.find({"active": True}, session=session).to_list()
        if any(user_id in row["entrant_ids"] for row in active):
            raise FinalError("Remove this entrant during registration, or finish/cancel the Final before deregistration.")
        for row in active:
            await collection.update_one({"_id": row["_id"]}, {"$inc": {"revision": 1}}, session=session)
        result = await Player.get_pymongo_collection().delete_one({"user_id": user_id}, session=session)
        if result.deleted_count != 1:
            raise FinalError("The registration changed. Refresh before deregistering.")
    await transaction(remove)


async def guard_result_deletion(match_id: str) -> None:
    if await FinalMatchResult.find_one({"$or": [{"_id": match_id}, {"source_token": match_id}, {"source_match_id": match_id}]}):
        raise FinalError("Use `/final match result` to correct this Final outcome and repair its bracket and payouts.")


async def migrate_final_identity(old: str, new: str, actor: str) -> None:
    async def migrate(session):
        finals = EloFinal.get_pymongo_collection()
        active = await finals.find({"$or": [{"active": True}, {"repair_pending": True},
                                          {"reopening_pending": True}]}, session=session).to_list()
        if await Player.get_pymongo_collection().find_one({"user_id": new}, session=session):
            raise FinalError("The destination is already registered to another player.")
        if any(old in row["entrant_ids"] or new in row["entrant_ids"] or row.get("repair_pending")
               or row.get("reopening_pending") for row in active):
            raise FinalError("Finish active participation and Final recovery before migrating this account.")
        wallets = TokenWallet.get_pymongo_collection()
        old_row = await wallets.find_one({"$or": [{"user_id": old}, {"identity_claims": old}]}, session=session)
        collision = await wallets.find_one({"$or": [{"user_id": new}, {"identity_claims": new}]}, session=session)
        if collision and (old_row is None or collision["_id"] != old_row["_id"]):
            raise FinalError("The destination already owns a token identity. Resolve the collision before migration.")
        if old_row and old_row["user_id"] != old:
            raise FinalError("The old identity is already owned by another wallet account.")
        predictions = FinalPrediction.get_pymongo_collection()
        if await predictions.find_one({"user_id": new}, session=session):
            raise FinalError("The destination already owns predictions. Resolve the collision before migration.")
        profiles = FinalPlayerProfile.get_pymongo_collection()
        if await profiles.find_one({"user_id": new}, session=session):
            raise FinalError("The destination already owns a Final profile.")
        if old_row:
            market_rows = await FinalBettingMarket.get_pymongo_collection().find({"state": {"$nin": ["PAID", "REFUNDED"]}}, session=session).to_list()
            if await FinalBet.get_pymongo_collection().find_one({"wallet_id": old_row["_id"], "active": True,
                "market_id": {"$in": [row["_id"] for row in market_rows]}}, session=session):
                raise FinalError("Resolve outstanding bets before migrating this account.")
            receipt = f"identity:{old_row['_id']}:{old}:{new}"
            await wallets.update_one({"_id": old_row["_id"]}, {"$set": {"user_id": new},
                "$addToSet": {"identity_claims": {"$each": [old, new]}}, "$inc": {"version": 1}}, session=session)
            entry = TokenLedgerEntry(id=receipt, operation_key=receipt, wallet_id=old_row["_id"], actor_id=actor,
                                     amount=0, kind="identity", identity_from=old, identity_to=new, created_at=int(time.time()))
            await TokenLedgerEntry.get_pymongo_collection().insert_one(entry.model_dump(by_alias=True), session=session)
        await predictions.update_many({"user_id": old}, {"$set": {"user_id": new}}, session=session)
        await profiles.update_one({"user_id": old}, {"$set": {"user_id": new}}, session=session)
        for row in active:
            await finals.update_one({"_id": row["_id"]}, {"$inc": {"revision": 1}}, session=session)
        changed = await Player.get_pymongo_collection().update_one({"user_id": old}, {"$set": {"user_id": new}}, session=session)
        if changed.matched_count != 1:
            raise FinalError("The registered account changed. Refresh before migrating.")
    await transaction(migrate)

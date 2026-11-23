import logging

from pymongo.errors import PyMongoError

from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import TokenAward, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.receipts import receipt, write_receipt
from elo_bot.features.tokens.wallets import ensure_wallet


async def freeze_prediction_awards(final, session) -> None:
    edition = str(final.id)
    revision = str(final.result_revision)
    rows = FinalPrediction.get_pymongo_collection().find({
        "final_id": edition, "submitted_at": {"$ne": None},
        "scoring_revision": final.result_revision, "points": {"$gt": 0},
    }, session=session)
    awards = TokenAward.get_pymongo_collection()
    async for row in rows:
        user_id = row["user_id"]
        wallet = await TokenWallet.get_pymongo_collection().find_one(
            {"identity_claims": user_id}, {"_id": 1}, session=session)
        allocation = TokenAward(id=f"prediction:{edition}:{wallet['_id'] if wallet else user_id}",
                                source=edition, user_id=user_id,
                                wallet_id=str(wallet["_id"]) if wallet else None,
                                kind="prediction_reward", amount=500 * row["points"], revision=revision)
        await awards.update_one({"_id": allocation.id}, {"$setOnInsert": allocation.model_dump(by_alias=True)},
                                upsert=True, session=session)


async def pay_award(award: TokenAward) -> None:
    if award.wallet_id is None:
        wallet = await TokenWallet.find_one({"identity_claims": award.user_id})
        if wallet is None:
            wallet = await ensure_wallet(award.user_id)
        wallet_id = wallet.id
    else:
        wallet_id = award.wallet_id

    async def pay(session):
        awards = TokenAward.get_pymongo_collection()
        row = await awards.find_one({"_id": award.id, "pending": True}, session=session)
        if row is None:
            return
        wallet = await TokenWallet.get_pymongo_collection().find_one({"_id": wallet_id}, session=session)
        if wallet is None:
            raise FinalError("Token award wallet is unavailable.")
        delta = row["amount"] - row["paid"]
        if delta:
            entry = receipt(f"{award.id}:{row['revision']}", wallet_id, wallet["user_id"], delta,
                            kind=row["kind"], match_id=row["source"] if row["kind"] == "activity_reward" else None)
            await write_receipt(session, entry, allow_debt=True)
        await awards.update_one({"_id": award.id}, {"$set": {
            "pending": False, "paid": row["amount"], "wallet_id": wallet_id,
        }}, session=session)

    await transaction(pay)


async def recover_awards(source: str | None = None) -> bool:
    query = {"pending": True}
    if source is not None:
        query["source"] = source
    complete = True
    async for award in TokenAward.find(query):
        try:
            await pay_award(award)
        except (FinalError, PyMongoError):
            complete = False
            logging.exception("Token award %s is pending", award.id)
    return complete

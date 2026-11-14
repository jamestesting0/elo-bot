import time

from pymongo.errors import DuplicateKeyError

from elobot.features.finals.persistence import transaction
from elobot.features.finals.types import FinalError
from elobot.db import Player
from elobot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet


async def ensure_wallet(user_id: str) -> TokenWallet:
    async def grant(session):
        if await Player.get_pymongo_collection().find_one({"user_id": user_id}, session=session) is None:
            raise FinalError("Register before using tokens.")
        collection = TokenWallet.get_pymongo_collection()
        row = await collection.find_one({"$or": [{"user_id": user_id}, {"identity_claims": user_id}]}, session=session)
        wallet = TokenWallet.model_validate(row) if row else TokenWallet(id=user_id, user_id=user_id,
                                                                       identity_claims=[user_id])
        if wallet.user_id != user_id:
            raise FinalError("This identity belongs to an existing wallet. A manager must resolve the account mapping.")
        receipt = await TokenLedgerEntry.get_pymongo_collection().find_one({"operation_key": f"grant:{wallet.id}"},
                                                                           session=session)
        if wallet.initialized and receipt:
            return wallet
        if not receipt and wallet.balance != 0:
            raise FinalError("Wallet initialization is incomplete. A manager must recover it before spending.")
        if not receipt:
            entry = TokenLedgerEntry(id=f"grant:{wallet.id}", operation_key=f"grant:{wallet.id}", wallet_id=wallet.id,
                                     actor_id=user_id, amount=5000, kind="grant", created_at=int(time.time()))
            await TokenLedgerEntry.get_pymongo_collection().insert_one(entry.model_dump(by_alias=True), session=session)
            wallet.balance += 5000
        wallet.initialized = True
        wallet.version += 1
        await collection.replace_one({"_id": wallet.id}, wallet.model_dump(by_alias=True), upsert=True, session=session)
        return wallet

    try:
        return await transaction(grant)
    except DuplicateKeyError:
        wallet = await TokenWallet.find_one({"user_id": user_id})
        receipt = await TokenLedgerEntry.find_one({"operation_key": f"grant:{wallet.id}"}) if wallet else None
        if wallet and wallet.initialized and receipt:
            return wallet
        raise


async def committed_stakes(wallet_id: str) -> int:
    markets = await FinalBettingMarket.find({"state": {"$in": ["OPEN", "CLOSED", "SETTLING", "CORRECTING"]}}).to_list()
    market_ids = [market.id for market in markets]
    bets = await FinalBet.find({"wallet_id": wallet_id, "market_id": {"$in": market_ids}, "active": True}).to_list()
    return sum(bet.amount for bet in bets)


async def wallet_summary(wallet: TokenWallet) -> tuple[int, int, int]:
    return max(wallet.balance, 0), await committed_stakes(wallet.id), max(-wallet.balance, 0)


async def backfill_wallets() -> int:
    count = 0
    for player in await Player.find_all().to_list():
        await ensure_wallet(player.user_id)
        count += 1
    return count

import time

from pymongo.asynchronous.client_session import AsyncClientSession

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import MAX_TOKENS
from elo_bot.db.token_models import TokenLedgerEntry, TokenWallet


async def write_receipt(session: AsyncClientSession, receipt: TokenLedgerEntry, *, allow_debt: bool = False) -> bool:
    """Write one ledger delta inside the caller's transaction."""
    ledger = TokenLedgerEntry.get_pymongo_collection()
    previous = await ledger.find_one({"_id": receipt.id}, session=session)
    if previous is not None:
        fields = ("wallet_id", "amount", "kind", "match_id", "item")
        if any(previous.get(field) != getattr(receipt, field) for field in fields):
            raise FinalError("This token action has changed. Open a fresh view.")
        return False
    wallets = TokenWallet.get_pymongo_collection()
    wallet = await wallets.find_one({"_id": receipt.wallet_id, "user_id": receipt.actor_id, "initialized": True},
                                    session=session)
    if wallet is None:
        raise FinalError("This token wallet is unavailable.")
    balance = wallet["balance"] + receipt.amount
    if not -MAX_TOKENS <= balance <= MAX_TOKENS:
        raise FinalError("This transaction exceeds the wallet limit.")
    if not allow_debt and receipt.amount < 0 and balance < 0:
        raise FinalError("You don’t have enough available tokens.")
    await wallets.update_one({"_id": receipt.wallet_id}, {"$set": {"balance": balance}, "$inc": {"version": 1}},
                             session=session)
    await ledger.insert_one(receipt.model_dump(by_alias=True), session=session)
    return True


def receipt(operation: str, wallet_id: str, actor: str, amount: int, **details) -> TokenLedgerEntry:
    return TokenLedgerEntry(id=operation, operation_key=operation, wallet_id=wallet_id, actor_id=actor,
                            amount=amount, created_at=int(time.time()), **details)

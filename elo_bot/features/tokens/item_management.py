from dataclasses import dataclass
import time

from pymongo import ReturnDocument

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import MAX_TOKENS, TokenItem
from elo_bot.db.token_models import TokenItemUse, TokenLedgerEntry, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.queue.policy import QUEUE_OPERATION_LOCK
from elo_bot.features.tokens.items import finish_use
from elo_bot.features.tokens.wallets import ensure_wallet


async def deactivate_item(wallet: TokenWallet, use_id: str) -> TokenItem:
    async def disable(session):
        row = await TokenItemUse.get_pymongo_collection().find_one(
            {"_id": use_id, "wallet_id": wallet.id}, session=session)
        if row is None:
            raise FinalError("This activated item is unavailable. Refresh your items.")
        use = TokenItemUse.model_validate(row)
        if use.item is TokenItem.EXP and use.state != "queued":
            raise FinalError("A pending EXP Potion cannot be disabled. Contact a manager.")
        if use.state == "returned" and not use.active:
            return use.item
        if use.state not in {"armed", "queued"} or not use.active:
            raise FinalError("This item is already reserved or used and cannot be disabled.")
        if not await finish_use(use, session, consume=False):
            raise FinalError("This item's status changed. Refresh your items.")
        return use.item

    async with QUEUE_OPERATION_LOCK:
        return await transaction(disable)


@dataclass(frozen=True, slots=True)
class ItemAdjustment:
    user_id: str
    item: TokenItem
    quantity: int
    manager_id: str
    operation_key: str


async def adjust_items(change: ItemAdjustment) -> TokenWallet:
    if not 1 <= abs(change.quantity) <= 1_000_000:
        raise FinalError("Choose between 1 and 1,000,000 items.")
    wallet = await ensure_wallet(change.user_id)

    async def adjust(session):
        ledger = TokenLedgerEntry.get_pymongo_collection()
        wallets = TokenWallet.get_pymongo_collection()
        previous = await ledger.find_one({"_id": change.operation_key}, session=session)
        if previous is not None:
            if (previous["wallet_id"], previous["actor_id"], previous["item"], previous["item_quantity"]) != (
                    wallet.id, change.manager_id, change.item, change.quantity):
                raise FinalError("This item adjustment changed. Run the command again.")
            return TokenWallet.model_validate(await wallets.find_one({"_id": wallet.id}, session=session))
        current = await wallets.find_one({"_id": wallet.id, "user_id": change.user_id}, session=session)
        if current is None:
            raise FinalError("This token wallet is unavailable.")
        quantity = current.get("inventory", {}).get(change.item, 0) + change.quantity
        if quantity < 0:
            raise FinalError("The player does not have enough of this item in inventory.")
        if quantity > MAX_TOKENS:
            raise FinalError("This adjustment exceeds the inventory limit.")
        updated = await wallets.find_one_and_update(
            {"_id": wallet.id}, {"$inc": {f"inventory.{change.item}": change.quantity, "version": 1}},
            session=session, return_document=ReturnDocument.AFTER)
        entry = TokenLedgerEntry(id=change.operation_key, operation_key=change.operation_key, wallet_id=wallet.id,
                                 actor_id=change.manager_id, amount=0, kind="item_adjust", item=change.item,
                                 item_quantity=change.quantity, created_at=int(time.time()))
        await ledger.insert_one(entry.model_dump(by_alias=True), session=session)
        return TokenWallet.model_validate(updated)

    return await transaction(adjust)

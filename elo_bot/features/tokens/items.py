from datetime import UTC, datetime
import secrets

from pymongo.errors import DuplicateKeyError

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import ITEMS, TokenItem
from elo_bot.db import Player, Season
from elo_bot.db.system_models import OVERRIDE_CODE_LIFETIME
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.receipts import receipt, write_receipt

ITEM_SLOTS = {
    TokenItem.PRIORITY_PRO: "queue",
    TokenItem.PRIORITY_ULTRA: "queue",
    TokenItem.DISTRICT: "district",
    TokenItem.CHALLENGE: "challenge",
    TokenItem.PROTECTION: "protection",
}


def same_activation(use: TokenItemUse, item: TokenItem, district: str | None) -> TokenItemUse:
    if use.item != item or use.district != district:
        raise FinalError("This item selection changed. Open your inventory again.")
    return use


async def current_season(session) -> int:
    archive = await Season.get_pymongo_collection().find_one({}, {"season_number": 1},
                                                             sort=[("season_number", -1)], session=session)
    return archive["season_number"] + 1 if archive else 1


async def purchase(wallet: TokenWallet, item: TokenItem, action_id: str) -> bool:
    async def buy(session):
        written = await write_receipt(session, receipt(f"purchase:{wallet.id}:{action_id}", wallet.id,
                                                      wallet.user_id, -ITEMS[item].price, kind="purchase", item=item))
        if written:
            await TokenWallet.get_pymongo_collection().update_one(
                {"_id": wallet.id}, {"$inc": {f"inventory.{item}": 1}}, session=session)
        return written

    return await transaction(buy)


async def activate(wallet: TokenWallet, item: TokenItem, action_id: str, *, district: str | None = None) -> TokenItemUse:
    slot = ITEM_SLOTS[item]
    use_id = f"{wallet.id}:{action_id}"
    now = int(datetime.now(UTC).timestamp())
    code = secrets.token_hex(8) if item is TokenItem.CHALLENGE else None

    async def arm(session):
        uses = TokenItemUse.get_pymongo_collection()
        existing = await uses.find_one({"_id": use_id}, session=session)
        if existing:
            return same_activation(TokenItemUse.model_validate(existing), item, district)
        previous = await uses.find_one({"wallet_id": wallet.id, "slot": slot, "active": True}, session=session)
        if previous:
            if previous.get("expires_at") is not None and previous["expires_at"] <= now and previous["state"] == "armed":
                await finish_use(TokenItemUse.model_validate(previous), session, consume=False)
            else:
                raise FinalError("You already have an active item of this type.")
        season = None
        if item is TokenItem.PROTECTION:
            season = await current_season(session)
            if await uses.find_one({"wallet_id": wallet.id, "slot": "protection", "season": season,
                                    "state": "used"}, session=session):
                raise FinalError("You’ve already used Rank Protection this season.")
        updated = await TokenWallet.get_pymongo_collection().update_one(
            {"_id": wallet.id, "user_id": wallet.user_id, f"inventory.{item}": {"$gt": 0}},
            {"$inc": {f"inventory.{item}": -1, "version": 1}}, session=session)
        if updated.matched_count != 1:
            raise FinalError("You don’t have this item.")
        use = TokenItemUse(id=use_id, wallet_id=wallet.id, item=item, slot=slot, district=district, target=code,
                           season=season,
                           expires_at=now + int(OVERRIDE_CODE_LIFETIME.total_seconds()) if code else None)
        await uses.insert_one(use.model_dump(by_alias=True), session=session)
        await Player.get_pymongo_collection().update_one(
            {"user_id": wallet.user_id}, {"$set": {"token_items_active": True}}, session=session)
        return use

    try:
        return await transaction(arm)
    except DuplicateKeyError as error:
        existing = await TokenItemUse.get(use_id)
        if existing is not None:
            return same_activation(existing, item, district)
        raise FinalError("You already have an active item of this type.") from error


async def finish_use(use: TokenItemUse, session, *, consume: bool) -> bool:
    updated = await TokenItemUse.get_pymongo_collection().update_one(
        {"_id": use.id, "state": use.state, "target": use.target},
        {"$set": {"state": "used" if consume else "returned", "active": False}}, session=session,
    )
    if updated.modified_count != 1:
        return False
    wallets = TokenWallet.get_pymongo_collection()
    increments = {"version": 1}
    if not consume:
        increments[f"inventory.{use.item}"] = 1
    wallet = await wallets.find_one_and_update({"_id": use.wallet_id}, {"$inc": increments}, session=session)
    if wallet is None:
        raise FinalError("This item’s token wallet is unavailable.")
    if not await TokenItemUse.get_pymongo_collection().find_one(
            {"wallet_id": use.wallet_id, "active": True}, {"_id": 1}, session=session):
        await Player.get_pymongo_collection().update_one(
            {"user_id": wallet["user_id"]}, {"$set": {"token_items_active": False}}, session=session)
    return True


async def release_expired_codes() -> None:
    now = int(datetime.now(UTC).timestamp())
    async for use in TokenItemUse.find({"slot": "challenge", "state": "armed", "expires_at": {"$lte": now}}):
        await transaction(lambda session, use=use: finish_use(use, session, consume=False))

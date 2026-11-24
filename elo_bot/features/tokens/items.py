from datetime import UTC, datetime
import secrets

from pymongo.errors import DuplicateKeyError
from pymongo import ReturnDocument

from elo_bot.core.finals import FinalError
from elo_bot.config.game import DISTRICT_INDEX
from elo_bot.core.token_items import ITEMS, MAX_TOKENS, TokenItem
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
    TokenItem.EXP: "exp",
}
CODE_LIFETIME_SECONDS = int(OVERRIDE_CODE_LIFETIME.total_seconds())


def same_activation(use: TokenItemUse, item: TokenItem, district: str | None) -> TokenItemUse:
    if use.item != item or use.district != district:
        raise FinalError("This item selection changed. Open your inventory again.")
    return use


async def current_season(session) -> int:
    archive = await Season.get_pymongo_collection().find_one({}, {"season_number": 1},
                                                             sort=[("season_number", -1)], session=session)
    return archive["season_number"] + 1 if archive else 1


async def purchase(wallet: TokenWallet, item: TokenItem, action_id: str, *, amount: int = 1) -> bool:
    if not 1 <= amount <= 1_000_000:
        raise FinalError("Choose between 1 and 1,000,000 items.")

    async def buy(session):
        written = await write_receipt(session, receipt(f"purchase:{wallet.id}:{action_id}", wallet.id,
                                                      wallet.user_id, -ITEMS[item].price * amount,
                                                      kind="purchase", item=item, item_quantity=amount))
        if written:
            updated = await TokenWallet.get_pymongo_collection().update_one(
                {"_id": wallet.id, "$or": [{f"inventory.{item}": {"$exists": False}},
                                            {f"inventory.{item}": {"$lte": MAX_TOKENS - amount}}]},
                {"$inc": {f"inventory.{item}": amount}}, session=session)
            if updated.matched_count != 1:
                raise FinalError("This purchase exceeds the inventory limit.")
        return written

    return await transaction(buy)


async def promote_next(wallet_id: str, slot: str, session) -> TokenItemUse | None:
    uses = TokenItemUse.get_pymongo_collection()
    head = await uses.find_one({"wallet_id": wallet_id, "slot": slot, "active": True,
                                "state": {"$in": ["armed", "reserved"]}}, session=session)
    if head:
        return TokenItemUse.model_validate(head)
    if slot == "protection" and await uses.find_one(
            {"wallet_id": wallet_id, "slot": slot, "season": await current_season(session), "state": "used"},
            session=session):
        return None
    fields = {"state": "armed"}
    if slot == "challenge":
        fields.update(target=secrets.token_hex(8), expires_at=int(datetime.now(UTC).timestamp())
                      + CODE_LIFETIME_SECONDS)
    row = await uses.find_one_and_update(
        {"wallet_id": wallet_id, "slot": slot, "active": True, "state": "queued"}, {"$set": fields},
        sort=[("activation_order", 1), ("_id", 1)], return_document=ReturnDocument.AFTER, session=session)
    return TokenItemUse.model_validate(row) if row else None


async def activate(wallet: TokenWallet, item: TokenItem, action_id: str, *, district: str | None = None) -> TokenItemUse:
    return (await activate_many(wallet, item, action_id, district=district))[0]


async def activate_many(wallet: TokenWallet, item: TokenItem, action_id: str, *, amount: int = 1,
                        district: str | None = None) -> list[TokenItemUse]:
    if not 1 <= amount <= 100:
        raise FinalError("Choose between 1 and 100 items.")
    if item is TokenItem.DISTRICT:
        if district not in DISTRICT_INDEX:
            raise FinalError("Choose a district when using District Choice.")
    elif district is not None:
        raise FinalError("A district can only be selected for District Choice.")
    slot = ITEM_SLOTS[item]
    issues_code = item is TokenItem.CHALLENGE
    use_id = f"{wallet.id}:{action_id}"
    use_ids = [use_id, *(f"{use_id}:{index}" for index in range(1, amount))]
    now = int(datetime.now(UTC).timestamp())

    async def replay(uses, existing, session=None):
        if existing.get("batch_size", 1) != amount:
            raise FinalError("This activation amount changed. Run the command again.")
        rows = await uses.find({"_id": {"$in": use_ids}}, session=session).sort("activation_order").to_list()
        if len(rows) != amount:
            raise FinalError("This activation is incomplete. Contact a manager.")
        return [same_activation(TokenItemUse.model_validate(row), item, district) for row in rows]

    async def arm(session):
        uses = TokenItemUse.get_pymongo_collection()
        existing = await uses.find_one({"_id": use_id}, session=session)
        if existing:
            return await replay(uses, existing, session)
        season = None
        if item is TokenItem.PROTECTION:
            season = await current_season(session)
        updated = await TokenWallet.get_pymongo_collection().find_one_and_update(
            {"_id": wallet.id, "user_id": wallet.user_id, f"inventory.{item}": {"$gte": amount}},
            {"$inc": {f"inventory.{item}": -amount, "version": amount}}, session=session, return_document=ReturnDocument.AFTER)
        if updated is None:
            raise FinalError("You don’t have enough of this item in inventory.")
        activations = [TokenItemUse(
            id=identifier, wallet_id=wallet.id, item=item, slot=slot, district=district, season=season,
            active=not issues_code, state="armed" if issues_code else "queued", batch_size=amount,
            target=secrets.token_hex(8) if issues_code else None,
            expires_at=now + CODE_LIFETIME_SECONDS if issues_code else None,
            activation_order=updated["version"] - amount + index + 1,
        ) for index, identifier in enumerate(use_ids)]
        await uses.insert_many([use.model_dump(by_alias=True) for use in activations], session=session)
        if not issues_code:
            await promote_next(wallet.id, slot, session)
            await Player.get_pymongo_collection().update_one(
                {"user_id": wallet.user_id}, {"$set": {"token_items_active": True}}, session=session)
        return await replay(uses, {"batch_size": amount}, session)

    try:
        return await transaction(arm)
    except DuplicateKeyError as error:
        uses = TokenItemUse.get_pymongo_collection()
        existing = await uses.find_one({"_id": use_id})
        if existing is not None:
            return await replay(uses, existing)
        raise FinalError("Your activated items changed. Refresh your inventory and try again.") from error


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
    await promote_next(use.wallet_id, use.slot, session)
    if not await TokenItemUse.get_pymongo_collection().find_one(
            {"wallet_id": use.wallet_id, "active": True}, {"_id": 1}, session=session):
        await Player.get_pymongo_collection().update_one(
            {"user_id": wallet["user_id"]}, {"$set": {"token_items_active": False}}, session=session)
    return True


async def release_expired_codes() -> None:
    now = int(datetime.now(UTC).timestamp())
    async for use in TokenItemUse.find({"slot": "challenge", "state": "armed", "expires_at": {"$lte": now}}):
        await transaction(lambda session, use=use: finish_use(use, session, consume=False))

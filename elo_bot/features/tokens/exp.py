import logging

from pymongo.errors import PyMongoError

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import TokenItem
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.items import activate, finish_use
from elo_bot.integrations.exp import ExpGrantError, grant_exp


async def redeem_exp(wallet: TokenWallet, action_id: str) -> TokenItemUse:
    use = await activate(wallet, TokenItem.EXP, action_id)
    if use.state == "queued":
        return use
    return await fulfill_exp(wallet, use)


async def fulfill_exp(wallet: TokenWallet, use: TokenItemUse) -> TokenItemUse:
    if use.state == "used":
        return use
    if use.state == "returned":
        raise FinalError("This potion was returned. Open your inventory to try again.")
    if use.state != "armed":
        raise FinalError("This EXP Potion is pending. Contact a manager before using another.")

    async def reserve() -> None:
        async def claim(session):
            updated = await TokenItemUse.get_pymongo_collection().update_one(
                {"_id": use.id, "state": "armed", "active": True}, {"$set": {"state": "reserved"}}, session=session)
            if updated.modified_count != 1:
                raise FinalError("This EXP Potion is already being used. Check your inventory.")
        try:
            await transaction(claim)
        except PyMongoError as error:
            raise FinalError("EXP was not submitted. Your potion is pending; contact a manager.") from error
        use.state = "reserved"

    try:
        response_id = await grant_exp(wallet.user_id, reserve)
    except ExpGrantError as error:
        if error.uncertain:
            raise FinalError("Your EXP grant is unconfirmed. The potion is pending; contact a manager.") from error
        try:
            returned = await transaction(lambda session: finish_use(use, session, consume=False))
        except PyMongoError as refund_error:
            raise FinalError("EXP was not granted. Your potion return is unconfirmed; contact a manager.") from refund_error
        if not returned:
            raise FinalError("This potion’s status changed. Check your inventory.") from error
        raise FinalError("EXP could not be granted. Your potion was returned; try again later.") from error

    async def complete(session):
        if not await finish_use(use, session, consume=True):
            raise FinalError("Your EXP grant is pending. Contact a manager.")
        await TokenItemUse.get_pymongo_collection().update_one(
            {"_id": use.id}, {"$set": {"target": response_id}}, session=session)

    try:
        await transaction(complete)
    except PyMongoError as error:
        raise FinalError("3,000 EXP was granted, but your potion is pending. Contact a manager.") from error
    use.state = "used"
    use.active = False
    use.target = response_id
    return use


async def recover_exp() -> None:
    async for use in TokenItemUse.find({"slot": "exp", "state": "armed", "active": True}):
        wallet = await TokenWallet.get(use.wallet_id)
        if wallet is None:
            logging.error("EXP activation %s has no wallet", use.id)
            continue
        try:
            await fulfill_exp(wallet, use)
        except FinalError as error:
            logging.warning("EXP activation %s: %s", use.id, error)

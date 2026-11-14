import time

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field

from elo_bot.features.finals.persistence import transaction
from elo_bot.core.finals import FinalError
from elo_bot.features.tokens.wallets import ensure_wallet
from elo_bot.db import EloFinal, Player
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet
from elo_bot.features.tokens.settlement import set_settlement_intent


class BetAction(BaseModel):
    model_config = ConfigDict(frozen=True)
    market_id: str
    user_id: str
    contestant: str
    amount: int = Field(ge=0, strict=True)
    expected_version: int = Field(ge=0)
    action_key: str


async def open_market(final: EloFinal, node_id: str) -> FinalBettingMarket:
    async def open_pairing(session):
        current = EloFinal.model_validate(await EloFinal.get_pymongo_collection().find_one({"_id": final.id}, session=session))
        node = current.nodes.get(node_id)
        if (node is None or node.bye or not all(node.contestants) or node.result_id or node.first_actual_started_at
                or not current.active or current.repair_pending or current.state in {"CANCELLING", "CANCELLED"}):
            raise FinalError("Betting can open only for a known unstarted pairing.")
        market_id = f"{current.id}:{node.id}:{node.contestant_revision}"
        row = await FinalBettingMarket.get_pymongo_collection().find_one({"_id": market_id}, session=session)
        if row:
            return FinalBettingMarket.model_validate(row)
        market = FinalBettingMarket(id=market_id, final_id=str(current.id), node_id=node_id,
                                   contestant_revision=node.contestant_revision, contestants=list(node.contestants),
                                   pools={user: 0 for user in node.contestants})
        await FinalBettingMarket.get_pymongo_collection().insert_one(market.model_dump(by_alias=True), session=session)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}}, session=session)
        return market

    return await transaction(open_pairing)


def projected_return(market: FinalBettingMarket, old: FinalBet | None, contestant: str, amount: int) -> tuple[int, int]:
    previous = old.amount if old and old.active else 0
    pool = sum(market.pools.values()) - previous + amount
    winning = market.pools.get(contestant, 0) - (previous if old and old.contestant == contestant else 0) + amount
    gross = pool * amount // winning if winning else amount
    return gross, gross - amount


async def apply_bet(request: BetAction) -> FinalBet:
    wallet = await ensure_wallet(request.user_id)

    async def apply(session):
        market_row = await FinalBettingMarket.get_pymongo_collection().find_one({"_id": request.market_id}, session=session)
        wallet_row = await TokenWallet.get_pymongo_collection().find_one({"_id": wallet.id}, session=session)
        if market_row is None or wallet_row is None:
            raise FinalError("This market or wallet is unavailable.")
        market = FinalBettingMarket.model_validate(market_row)
        current_wallet = TokenWallet.model_validate(wallet_row)
        final_row = await EloFinal.get_pymongo_collection().find_one({"_id": PydanticObjectId(market.final_id)}, session=session)
        final = EloFinal.model_validate(final_row)
        bet_id = f"{market.id}:{wallet.id}"
        old_row = await FinalBet.get_pymongo_collection().find_one({"_id": bet_id}, session=session)
        old = FinalBet.model_validate(old_row) if old_row else None
        receipt = await TokenLedgerEntry.get_pymongo_collection().find_one({"operation_key": request.action_key}, session=session)
        if receipt:
            if (old and old.action_id == request.action_key and old.user_id == request.user_id
                    and old.amount == request.amount and (request.amount == 0 or old.contestant == request.contestant)):
                return old
            raise FinalError("This action was already applied and changed later. Refresh the bet.")
        if (market.state != "OPEN" or not final.active or final.repair_pending
                or final.state in {"CANCELLING", "CANCELLED"}
                or final.nodes[market.node_id].contestant_revision != market.contestant_revision):
            raise FinalError("Betting is closed for this pairing.")
        if current_wallet.user_id != request.user_id or await Player.get_pymongo_collection().find_one(
            {"user_id": request.user_id}, session=session,
        ) is None:
            raise FinalError("This wallet belongs to another account or registration changed.")
        if set(current_wallet.identity_claims) & set(market.contestants):
            raise FinalError("Participants cannot bet on their own match.")
        if (old.version if old else 0) != request.expected_version:
            raise FinalError("Your bet changed. Open its current review.")
        previous = old.amount if old and old.active else 0
        contestant = old.contestant if request.amount == 0 and old else request.contestant
        if contestant not in market.contestants or request.amount == 0 and previous == 0:
            raise FinalError("Choose a contestant and a positive stake, or cancel an active bet.")
        if current_wallet.balance < 0 and (request.amount > previous or old and contestant != old.contestant):
            raise FinalError(f"Cover your {abs(current_wallet.balance)} token debt before increasing or switching a bet.")
        difference = previous - request.amount
        if difference < 0 and current_wallet.balance + difference < 0:
            raise FinalError("Insufficient Available Tokens.")
        if old and old.active:
            market.pools[old.contestant] = market.pools.get(old.contestant, 0) - previous
        market.pools[contestant] = market.pools.get(contestant, 0) + request.amount
        market.escrow -= difference
        market.version += 1
        bet = FinalBet(id=bet_id, market_id=market.id, wallet_id=wallet.id, user_id=request.user_id,
                       contestant=contestant, amount=request.amount, active=request.amount > 0,
                       version=(old.version if old else 0) + 1, action_id=request.action_key)
        entry = TokenLedgerEntry(id=request.action_key, operation_key=request.action_key, wallet_id=wallet.id,
                                 actor_id=request.user_id, amount=difference, escrow_amount=-difference,
                                 kind="cancel" if request.amount == 0 else ("edit" if previous else "stake"),
                                 market_id=market.id, created_at=int(time.time()))
        await TokenLedgerEntry.get_pymongo_collection().insert_one(entry.model_dump(by_alias=True), session=session)
        await TokenWallet.get_pymongo_collection().update_one({"_id": wallet.id},
            {"$inc": {"balance": difference, "version": 1}}, session=session)
        await FinalBet.get_pymongo_collection().replace_one({"_id": bet.id}, bet.model_dump(by_alias=True), upsert=True,
                                                          session=session)
        await FinalBettingMarket.get_pymongo_collection().replace_one({"_id": market.id}, market.model_dump(by_alias=True),
                                                                    session=session)
        await EloFinal.get_pymongo_collection().update_one({"_id": final.id}, {"$inc": {"revision": 1}}, session=session)
        return bet

    return await transaction(apply)


async def close_market(market_id: str) -> None:
    await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market_id, "state": "OPEN"},
        {"$set": {"state": "CLOSED"}, "$inc": {"version": 1}})


async def void_market(market_id: str, actor: str, reason: str) -> None:
    if not reason.strip():
        raise FinalError("Enter a reason for the refund.")
    async def void(session):
        collection = FinalBettingMarket.get_pymongo_collection()
        market = FinalBettingMarket.model_validate(await collection.find_one({"_id": market_id}, session=session))
        final_collection = EloFinal.get_pymongo_collection()
        final = EloFinal.model_validate(await final_collection.find_one({"_id": PydanticObjectId(market.final_id)}, session=session))
        if market.void_reason:
            return
        if market.state not in {"OPEN", "CLOSED"} or final.nodes[market.node_id].winner or final.repair_pending:
            raise FinalError("Only an undecided market may be voided. Use result correction for paid outcomes.")
        await collection.update_one({"_id": market.id}, {"$set": {"void_reason": reason.strip(), "void_actor": actor}}, session=session)
        await set_settlement_intent(market.id, final.result_revision + 1, refund=True, session=session)
        await final_collection.update_one({"_id": final.id}, {"$inc": {"revision": 1, "result_revision": 1},
            "$set": {"repair_pending": True}}, session=session)
    await transaction(void)

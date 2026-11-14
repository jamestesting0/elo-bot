import time

from common.finals.persistence import transaction
from common.finals.types import FinalError
from elobot.db.token_models import (
    FinalBet, FinalBettingMarket, FrozenStake, SettlementGeneration, TokenLedgerEntry, TokenWallet,
)


def allocations(stakes: dict[str, FrozenStake], winner: str | None, refund: bool) -> tuple[dict[str, int], bool]:
    pool = sum(stake.amount for stake in stakes.values())
    winning = {wallet: stake for wallet, stake in stakes.items() if stake.contestant == winner}
    total = sum(stake.amount for stake in winning.values())
    if refund or total == 0:
        return {wallet: stake.amount for wallet, stake in stakes.items()}, True
    payouts = {wallet: pool * stake.amount // total for wallet, stake in winning.items()}
    remaining = pool - sum(payouts.values())
    ordered = sorted(winning, key=lambda wallet: (-(pool * winning[wallet].amount % total), winning[wallet].bet_id))
    for wallet in ordered[:remaining]:
        payouts[wallet] += 1
    return payouts, False


async def set_settlement_intent(market_id: str, result_version: int, *, winner: str | None = None,
                                refund: bool = False, session=None) -> None:
    async def freeze(session):
        collection = FinalBettingMarket.get_pymongo_collection()
        row = await collection.find_one({"_id": market_id}, session=session)
        if row is None:
            return
        market = FinalBettingMarket.model_validate(row)
        if market.desired_result_version is not None and result_version < market.desired_result_version:
            raise FinalError("A newer settlement correction already owns this market.")
        if market.desired_result_version == result_version:
            if market.desired_winner != winner or market.desired_refund != refund:
                raise FinalError("This result version already has a different settlement decision.")
            return
        if not market.stakes:
            bets = await FinalBet.get_pymongo_collection().find({"market_id": market.id, "active": True}, session=session).to_list()
            market.stakes = {row["wallet_id"]: FrozenStake(bet_id=row["_id"], contestant=row["contestant"], amount=row["amount"])
                             for row in bets}
        market.desired_result_version = result_version
        market.desired_winner = winner
        market.desired_refund = refund
        market.state = "CORRECTING" if market.generations else "SETTLING"
        market.version += 1
        await collection.replace_one({"_id": market.id}, market.model_dump(by_alias=True), session=session)

    if session is not None:
        await freeze(session)
    else:
        await transaction(freeze)


async def advance_settlement(market_id: str) -> bool:
    async def advance(session):
        collection = FinalBettingMarket.get_pymongo_collection()
        row = await collection.find_one({"_id": market_id}, session=session)
        if row is None:
            raise FinalError("The betting market is unavailable.")
        market = FinalBettingMarket.model_validate(row)
        if market.desired_result_version is None:
            return False
        wanted, refund = allocations(market.stakes, market.desired_winner, market.desired_refund)
        if not market.generations:
            if market.escrow != sum(stake.amount for stake in market.stakes.values()):
                raise FinalError("The market's escrow does not match its frozen stakes. Contact a manager.")
            market.generations.append(SettlementGeneration(version=1, result_version=market.desired_result_version,
                                                           winner=market.desired_winner, refund=refund, allocations=wanted))
        generation = market.generations[-1]
        unpaid = [wallet for wallet in generation.allocations if wallet not in generation.paid]
        reversing = not unpaid and (generation.allocations != wanted or generation.refund != refund
                                    or bool(generation.reversed))
        unreversed = [wallet for wallet in generation.allocations if wallet not in generation.reversed]
        if unpaid:
            wallet_id = unpaid[0]
            amount = generation.allocations[wallet_id]
            kind = "refund" if generation.refund else "payout"
            generation.paid.append(wallet_id)
        elif reversing and unreversed:
            wallet_id = unreversed[0]
            amount = -generation.allocations[wallet_id]
            kind = "reversal"
            generation.reversed.append(wallet_id)
        elif reversing:
            if market.escrow != sum(stake.amount for stake in market.stakes.values()):
                raise FinalError("Payout reversal is incomplete. Contact a manager.")
            market.generations.append(SettlementGeneration(version=generation.version + 1,
                result_version=market.desired_result_version, winner=market.desired_winner, refund=refund, allocations=wanted))
            await collection.replace_one({"_id": market.id}, market.model_dump(by_alias=True), session=session)
            return True
        else:
            market.state = "REFUNDED" if refund else "PAID"
            market.settled_result_version = market.desired_result_version
            await collection.replace_one({"_id": market.id}, market.model_dump(by_alias=True), session=session)
            return False
        receipt_key = f"{market.id}:{generation.version}:{kind}:{wallet_id}"
        receipt = TokenLedgerEntry(id=receipt_key, operation_key=receipt_key, wallet_id=wallet_id,
                                   actor_id="final-settlement", amount=amount, escrow_amount=-amount, kind=kind,
                                   market_id=market.id, generation=generation.version, created_at=int(time.time()))
        wallet = await TokenWallet.get_pymongo_collection().find_one({"_id": wallet_id}, session=session)
        if wallet is None:
            raise FinalError("A settlement wallet is missing. Contact a manager.")
        await TokenLedgerEntry.get_pymongo_collection().insert_one(receipt.model_dump(by_alias=True), session=session)
        await TokenWallet.get_pymongo_collection().update_one({"_id": wallet_id},
            {"$inc": {"balance": amount, "version": 1}}, session=session)
        market.escrow -= amount
        await collection.replace_one({"_id": market.id}, market.model_dump(by_alias=True), session=session)
        return True

    return await transaction(advance)


async def recover_market(market_id: str, limit: int = 100) -> bool:
    for _ in range(limit):
        if not await advance_settlement(market_id):
            return True
    return False

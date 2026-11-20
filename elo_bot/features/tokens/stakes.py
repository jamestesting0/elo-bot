from typing import Literal

from beanie import PydanticObjectId

from elo_bot.core.finals import FinalError, is_final_match
from elo_bot.core.match_results import is_match_finished
from elo_bot.core.token_items import MAX_TOKENS
from elo_bot.db import EloFinal, Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import ParticipantStake, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.matches.timing import hosting_started
from elo_bot.features.tokens.receipts import receipt, write_receipt
from elo_bot.features.tokens.wallets import ensure_wallet


type StakeInput = int | Literal["all-in"]


def parse_stake(value: str) -> StakeInput:
    digits = value.strip()
    if digits.lower() == "all-in":
        return "all-in"
    if not digits or not digits.isascii() or not digits.isdecimal() or len(digits) > 19:
        raise FinalError("Enter a whole number of tokens or `all-in`.")
    amount = int(digits)
    if amount > MAX_TOKENS:
        raise FinalError("This stake exceeds the wallet limit.")
    return amount


def stake_cutoff_reached(match: Match) -> bool:
    if match.match_stake_closed or match.final_start_claim or match.autowin_winner_id:
        return True
    if is_final_match(match):
        return hosting_started(match)
    # Legacy ordinary schedules record a time with no host.
    return any(host and timestamp is not None for host, timestamp in zip(match.hosts, match.times, strict=True))


def validate_stake_match(match: Match, user_id: str, amount: StakeInput) -> None:
    if (match.is_challenge or match.final_superseded or len(match.participants) != 2
            or user_id not in {p.user_id for p in match.participants}):
        raise FinalError("Only participants in a queued match or Final can place a stake here.")
    if not is_final_match(match) and isinstance(amount, int) and amount > 1_000:
        raise FinalError("Queued matches have a 1,000-token stake limit.")
    if stake_cutoff_reached(match) or is_match_finished(match) or match.result_operation:
        raise FinalError("Stakes close when the match starts.")


async def set_stake(match: Match, user_id: str, requested: StakeInput) -> ParticipantStake | None:
    validate_stake_match(match, user_id, requested)
    users = tuple(p.user_id for p in match.participants)
    wallet = await ensure_wallet(user_id) if requested else None
    match_id = str(match.id)
    # Snapshot the offer seen by this command. A racing edit requires a new acceptance.
    observed = await ParticipantStake.get(match_id)

    async def apply(session):
        matches = Match.get_pymongo_collection()
        row = await matches.find_one({"_id": match.id, "thread": match.thread}, session=session)
        if row is None:
            raise FinalError("This match is unavailable.")
        current = Match.model_validate(row)
        validate_stake_match(current, user_id, requested)
        if current.final_contestant_revision != match.final_contestant_revision or current.final_attempt != match.final_attempt:
            raise FinalError("This Final pairing changed.")
        if is_final_match(current):
            edition = await EloFinal.get(PydanticObjectId(current.final_id), session=session)
            node = edition.nodes.get(current.final_node_id) if edition else None
            if (edition is None or not edition.active or edition.state == "CANCELLING" or node is None
                    or node.match_id != current.id or node.attempt != current.final_attempt
                    or node.contestant_revision != current.final_contestant_revision or node.result_id):
                raise FinalError("This Final pairing changed.")
        stakes = ParticipantStake.get_pymongo_collection()
        row = await stakes.find_one({"_id": match_id}, session=session)
        stake = ParticipantStake.model_validate(row) if row else None
        if stake and stake.state == "locked":
            if requested == stake.committed_amount(user_id) or (requested == "all-in" and user_id in stake.all_in_users):
                return stake
            raise FinalError("Both stakes are already fixed.")
        if (stake and (observed is None or stake.revision != observed.revision)) or (not stake and observed):
            raise FinalError("The offer changed. Run `/match stake` again.")
        if not requested and (not stake or stake.state != "offer" or stake.author != user_id):
            return stake
        current_wallet = None
        amount = requested
        if wallet is not None:
            current_wallet = await TokenWallet.get_pymongo_collection().find_one(
                {"_id": wallet.id, "user_id": user_id}, session=session)
            if not current_wallet or current_wallet["balance"] <= 0:
                raise FinalError("You don’t have enough available tokens.")
            if requested == "all-in":
                amount = current_wallet["balance"]
                validate_stake_match(current, user_id, amount)
            if current_wallet["balance"] < amount:
                raise FinalError("You don’t have enough available tokens.")
        revision = current.match_stake_revision + 1
        result = ParticipantStake(id=match_id, final_id=current.final_id, users=users, author=user_id,
                                  amount=amount, state="offer" if amount else "closed", revision=revision,
                                  all_in_users=[user_id] if requested == "all-in" else [])
        if stake and stake.state == "offer" and stake.author != user_id:
            if requested != "all-in" and amount != stake.amount:
                raise FinalError(f"Enter {stake.amount:,} tokens to match the current stake.")
            other = await TokenWallet.get_pymongo_collection().find_one(
                {"user_id": stake.author, "initialized": True}, session=session)
            if other is None or other["balance"] < amount:
                raise FinalError("Your opponent doesn’t have enough available tokens.")
            if other["_id"] == wallet.id:
                raise FinalError("Both players need separate token wallets.")
            call = min(amount, stake.amount)
            offered = call if call * 2 < stake.amount else stake.amount
            if other["balance"] + call > MAX_TOKENS or current_wallet["balance"] + offered > MAX_TOKENS:
                raise FinalError("This stake exceeds the wallet limit.")
            wallets = {user_id: wallet.id, stake.author: other["_id"]}
            contributions = {user_id: call, stake.author: offered}
            for user in users:
                held = contributions[user]
                await write_receipt(session, receipt(f"hold:{match_id}:{revision}:{user}", wallets[user], user, -held,
                                                    kind="match_hold", match_id=match_id, escrow_amount=held))
            result.author = stake.author
            result.amount = stake.amount
            result.contributions = contributions
            result.all_in_users = [
                user for user in [*stake.all_in_users, *result.all_in_users]
                if contributions[user] == (other["balance"] if user == stake.author else current_wallet["balance"])
            ]
            result.state = "locked"
            result.wallets = tuple(wallets[user] for user in users)
        await matches.update_one({"_id": current.id}, {"$set": {"match_stake_enabled": True},
                                                       "$inc": {"match_stake_revision": 1}}, session=session)
        await stakes.replace_one({"_id": match_id}, result.model_dump(by_alias=True), upsert=True, session=session)
        return result

    return await transaction(apply)


async def stake_window_closed(match: Match, *, expected: dict | None = None) -> None:
    result = await match.get_pymongo_collection().update_one(
        {**(expected or {}), "_id": match.id, "final_superseded": {"$ne": True}},
        {"$set": {"match_stake_closed": True}},
    )
    if result.matched_count != 1:
        raise FinalError("The match changed before the start was accepted.")


async def stake_outcome(match: Match, session) -> tuple[str, str | None] | None:
    if is_final_match(match):
        if match.final_superseded:
            return "superseded", None
        row = await FinalMatchResult.get_pymongo_collection().find_one({"_id": match.final_result_id}, session=session)
        if row:
            result = FinalMatchResult.model_validate(row)
            return final_stake_outcome(result)
        return None
    if match.is_autowin and match.autowin_completed:
        return "autowin", None
    operation = match.result_operation
    if operation and operation.cancelled:
        return f"cancel:{operation.key}", None
    if operation and operation.completed and operation.record:
        return operation.key, next((user for user, stats in operation.record.players.items() if stats.won), None)
    return None


def final_stake_outcome(result: FinalMatchResult) -> tuple[str, str | None]:
    return result.id, result.snapshot.winner if result.snapshot.outcome == "Played" else None


async def reconcile_stake(
    match_id: str, *, deleted: Match | None = None, official_outcome: tuple[str, str | None] | None = None, session=None,
) -> None:
    async def reconcile(session):
        stakes = ParticipantStake.get_pymongo_collection()
        row = await stakes.find_one({"_id": match_id}, session=session)
        if row is None:
            return
        stake = ParticipantStake.model_validate(row)
        match_row = await Match.get_pymongo_collection().find_one({"_id": PydanticObjectId(match_id)}, session=session)
        match = deleted or (Match.model_validate(match_row) if match_row else None)
        outcome = official_outcome or (await stake_outcome(match, session) if match else None)
        cancelled = deleted is not None or match is None
        if stake.state == "offer" and (cancelled or outcome or stake_cutoff_reached(match)):
            await stakes.update_one({"_id": match_id}, {"$set": {"state": "closed", "publication_pending": not cancelled},
                                                        "$inc": {"revision": 1}}, session=session)
        if stake.state not in {"locked", "settled"} or stake.wallets is None:
            return
        if outcome is None and cancelled and stake.state == "locked":
            outcome = "cancelled", None
        if outcome is None:
            return
        key, winner = outcome
        if key == stake.settlement_key:
            return
        pot = sum(stake.committed_amount(user) for user in stake.users)
        paid = {wallet_id: (pot if user == winner else 0) if winner else stake.committed_amount(user)
                for user, wallet_id in zip(stake.users, stake.wallets, strict=True)}
        kind = "match_correction" if stake.settlement_key else "match_payout" if winner else "match_refund"
        for wallet_id, allocation in paid.items():
            delta = allocation - stake.paid.get(wallet_id, 0)
            if delta:
                wallet = await TokenWallet.get_pymongo_collection().find_one({"_id": wallet_id}, session=session)
                if wallet is None:
                    raise FinalError("A match stake wallet is unavailable.")
                await write_receipt(session, receipt(f"stake:{match_id}:{stake.revision}:{key}:{wallet_id}", wallet_id,
                                                    wallet["user_id"], delta, kind=kind, match_id=match_id),
                                    allow_debt=True)
        await stakes.update_one({"_id": match_id}, {"$set": {
            "state": "settled", "paid": paid, "settlement_key": key, "publication_pending": not cancelled,
        }}, session=session)

    if session is not None:
        await reconcile(session)
    else:
        await transaction(reconcile)


async def retire_replaced_stake(match: Match, session) -> None:
    if not match.match_stake_enabled:
        return
    await reconcile_stake(str(match.id), official_outcome=("replaced", None), session=session)
    await ParticipantStake.get_pymongo_collection().update_one(
        {"_id": str(match.id)}, {"$set": {
            "state": "closed", "paid": {}, "wallets": None, "settlement_key": None, "publication_pending": True,
            "contributions": {}, "all_in_users": [],
            "revision": match.match_stake_revision + 1,
        }}, session=session)

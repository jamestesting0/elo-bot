from elo_bot.config.game import LEAGUES
from elo_bot.core.finals import FinalError
from elo_bot.db import Match, Player, RecordedMatch
from elo_bot.db.token_models import TokenAward, TokenItemUse, TokenWallet
from elo_bot.db.result_models import ResultRecordSnapshot
from elo_bot.features.tokens.items import current_season, finish_use, promote_next
from elo_bot.features.tokens.receipts import receipt, write_receipt
from elo_bot.features.tokens.wallets import initialize_wallet


async def reserve_protections(match: Match, session) -> None:
    users = [participant.user_id for participant in match.participants]
    wallets = await TokenWallet.get_pymongo_collection().find({"user_id": {"$in": users}}, session=session).to_list()
    uses = TokenItemUse.get_pymongo_collection()
    season = await current_season(session)
    for wallet in wallets:
        await promote_next(wallet["_id"], "protection", session)
        use = await uses.find_one({"wallet_id": wallet["_id"], "slot": "protection", "state": "armed"},
                                  session=session)
        if use is None:
            continue
        if await uses.find_one({"wallet_id": wallet["_id"], "slot": "protection", "season": season,
                                "state": "used"}, session=session):
            await finish_use(TokenItemUse.model_validate(use), session, consume=False)
            continue
        await uses.update_one({"_id": use["_id"]}, {"$set": {
            "season": season, "state": "reserved", "target": str(match.id),
        }}, session=session)
        match.token_protections[wallet["user_id"]] = use["_id"]


async def protect_result(match: Match, candidate: RecordedMatch, session) -> None:
    uses = TokenItemUse.get_pymongo_collection()
    for user, use_id in match.token_protections.items():
        row = await uses.find_one({"_id": use_id, "target": str(match.id)}, session=session)
        if row is None or row["state"] not in {"reserved", "used"}:
            continue
        use = TokenItemUse.model_validate(row)
        stats = candidate.players[user]
        elo = row.get("protection_elo")
        if elo is None:
            player = await Player.get_pymongo_collection().find_one({"user_id": user}, {"elo": 1}, session=session)
            if player is None:
                continue
            elo = player["elo"]
        floor = max((low for low, _ in LEAGUES.values() if low <= elo), default=None)
        loss = any(other.won for opponent, other in candidate.players.items() if opponent != user)
        if not match.is_autowin and loss and floor is not None and elo + stats.elo_change < floor:
            stats.elo_change = floor - elo
            if use.state == "reserved":
                await uses.update_one({"_id": use.id}, {"$set": {"protection_elo": elo}}, session=session)
                await finish_use(use, session, consume=True)
        else:
            await finish_use(use, session, consume=False)


async def release_protections(match: Match, session, *, restore_used: bool = False) -> None:
    for use_id in match.token_protections.values():
        use = await TokenItemUse.get(use_id, session=session)
        if use and (use.state == "reserved" or restore_used and use.state == "used") and use.target == str(match.id):
            await finish_use(use, session, consume=False)


async def apply_ranked_awards(match: Match, record: ResultRecordSnapshot, revision: str, session) -> None:
    source = str(match.id)
    awards = TokenAward.get_pymongo_collection()
    for user, stats in record.players.items():
        award_id = f"activity:{source}:{user}"
        old = await awards.find_one({"_id": award_id}, session=session)
        if old and old["revision"] == revision and not old["pending"]:
            continue
        amount = 200 if stats.won else 100
        wallet_filter = {"_id": old["wallet_id"]} if old and old.get("wallet_id") else {"identity_claims": user}
        wallet = await TokenWallet.get_pymongo_collection().find_one(wallet_filter, session=session)
        owner = TokenWallet.model_validate(wallet) if wallet else await initialize_wallet(user, session)
        delta = amount - (old["paid"] if old else 0)
        if delta:
            await write_receipt(session, receipt(f"{award_id}:{revision}", owner.id, owner.user_id, delta,
                                                kind="activity_reward", match_id=source), allow_debt=True)
        allocation = TokenAward(
            id=award_id, source=source, kind="activity_reward", user_id=user, wallet_id=owner.id,
            amount=amount, revision=revision, paid=amount, pending=False,
        )
        await awards.replace_one({"_id": award_id}, allocation.model_dump(by_alias=True), upsert=True, session=session)


async def reverse_ranked_awards(source: str, revision: str, session) -> None:
    awards = TokenAward.get_pymongo_collection()
    cancellation = f"cancel:{revision}"
    async for award in awards.find({"source": source, "kind": "activity_reward", "revision": revision}, session=session):
        if award["paid"]:
            wallet_id = award["wallet_id"]
            wallet = await TokenWallet.get_pymongo_collection().find_one({"_id": wallet_id}, session=session)
            if wallet is None:
                raise FinalError("A match reward wallet is unavailable.")
            await write_receipt(session, receipt(f"{award['_id']}:{cancellation}", wallet_id, wallet["user_id"],
                                                -award["paid"], kind="activity_reward", match_id=source), allow_debt=True)
        await awards.update_one({"_id": award["_id"], "revision": revision}, {"$set": {
            "revision": cancellation, "amount": 0, "paid": 0, "pending": False,
        }}, session=session)

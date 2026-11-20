import time

from beanie import PydanticObjectId

from elo_bot.core.finals import FinalError
from elo_bot.db import Match
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.tokens.items import finish_use
from elo_bot.features.tokens.ranked import reserve_protections


async def owned_challenge_code(user_id: str, code: str | None) -> TokenItemUse | None:
    if not code:
        return None
    wallet = await TokenWallet.find_one({"user_id": user_id})
    if wallet is None:
        return None
    return await TokenItemUse.find_one({"wallet_id": wallet.id, "slot": "challenge", "state": "armed",
                                       "target": code, "expires_at": {"$gt": int(time.time())}})


async def insert_challenge(match: Match, use: TokenItemUse | None) -> None:
    match.id = PydanticObjectId()

    async def insert(session):
        if use is not None:
            result = await TokenItemUse.get_pymongo_collection().update_one(
                {"_id": use.id, "state": "armed", "target": use.target, "expires_at": {"$gt": int(time.time())}},
                {"$set": {"state": "reserved", "target": match.token}}, session=session)
            if result.modified_count != 1:
                raise FinalError("The override code has expired or was already used.")
            match.token_challenge_use = use.id
        await reserve_protections(match, session)
        await match.insert(session=session)

    await transaction(insert)


async def accept_challenge(match: Match) -> None:
    async def accept(session):
        use = await TokenItemUse.get(match.token_challenge_use, session=session)
        if use is None or use.state != "reserved" or use.target != match.token:
            raise FinalError("The override code has expired or was already used.")
        participant = match.participants[1]
        result = await Match.get_pymongo_collection().update_one(
            {"_id": match.id, "token_challenge_use": use.id, "participants.1.confirmed": False},
            {"$set": {"participants.1.confirmed": True, "participants.1.confirmed_at": participant.confirmed_at,
                      "participants.1.confirmation_method": participant.confirmation_method}},
            session=session,
        )
        if not result.matched_count:
            raise FinalError("This challenge is no longer awaiting acceptance.")
        await finish_use(use, session, consume=True)

    await transaction(accept)


async def return_challenge_code(match: Match, session) -> None:
    if match.token_challenge_use is not None:
        use = await TokenItemUse.get(match.token_challenge_use, session=session)
        if use is not None and use.state == "reserved" and use.target == match.token:
            await finish_use(use, session, consume=False)

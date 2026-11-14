from collections.abc import Awaitable, Callable

from pymongo.asynchronous.client_session import AsyncClientSession
from pymongo.read_concern import ReadConcern
from pymongo.write_concern import WriteConcern
from pymongo import IndexModel

from common.finals.types import FinalError
from elobot.db.final_models import FinalMatchResult, FinalPlayerProfile, FinalPrediction
from elobot.db.season_models import EloFinal
from elobot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet
from elobot.db.match_models import Match

FINAL_DOCUMENT_MODELS = (FinalMatchResult, FinalPlayerProfile, FinalPrediction,
                         TokenWallet, TokenLedgerEntry, FinalBettingMarket, FinalBet)


async def register_final_indexes() -> None:
    for model in (EloFinal, *FINAL_DOCUMENT_MODELS):
        await model.get_pymongo_collection().create_indexes(model.Settings.indexes)
    await Match.get_pymongo_collection().create_indexes([
        IndexModel([("final_id", 1), ("final_node_id", 1), ("final_contestant_revision", 1), ("final_attempt", 1)],
                   unique=True, partialFilterExpression={"format": "elo_final_seven"}, name="final_match_attempt"),
    ])


async def require_transactions() -> None:
    database = EloFinal.get_pymongo_collection().database
    hello = await database.command("hello")
    if not hello.get("setName") and hello.get("msg") != "isdbgrid":
        raise FinalError("Final results, predictions and tokens require a transaction-capable database.")


async def transaction[T](action: Callable[[AsyncClientSession], Awaitable[T]]) -> T:
    await require_transactions()
    client = EloFinal.get_pymongo_collection().database.client
    async with client.start_session() as session:
        return await session.with_transaction(action, read_concern=ReadConcern("snapshot"),
                                              write_concern=WriteConcern("majority"))

import asyncio
import json
import os
import sys

from beanie import init_beanie, PydanticObjectId
from pymongo import AsyncMongoClient

from common.constants import DATA_BASES, FINAL_CHANNEL, MAIN_GUILD
from common.finals.lifecycle import add_entrant, confirm_bracket, resolve_final, start_final
from common.finals.persistence import FINAL_DOCUMENT_MODELS, register_final_indexes
from common.finals.recovery import recover_final
from common.finals.results import FinalResultSubmission, submit_result
from common.tokens.bets import BetAction, apply_bet, open_market
from common.tokens.wallets import ensure_wallet
from database_models import BaseLayout, EloFinal, Helper, Match, Player, PlayerSeasonData, RecordedMatch, Season


async def run():
    version, database_name, action = sys.argv[1:]
    if version not in DATA_BASES or not database_name.startswith("elo_final_it_") or not database_name.endswith(f"_{version}"):
        raise ValueError("Only run-specific disposable environment databases are accepted.")
    if os.environ.get("MONGO_DB_URL") or os.environ.get("BOT_MAIN_TOKEN") or os.environ.get("BOT_BETA_TOKEN"):
        raise ValueError("Production credentials must not enter the isolation probe.")
    client = AsyncMongoClient(os.environ["FINAL_TEST_MONGO_URI"], serverSelectionTimeoutMS=3000)
    try:
        database = client[database_name]
        await init_beanie(database=database, document_models=[Match, Player, Season, EloFinal, Helper, BaseLayout,
            RecordedMatch, *FINAL_DOCUMENT_MODELS], skip_indexes=True)
        await register_final_indexes()
        if action == "seed":
            await Season(id=PydanticObjectId("000000000000000000000007"), season_number=7, start=1, end=2,
                players=[PlayerSeasonData(player_id=str(user), elo=1000-user) for user in range(1,10)]).insert()
            for user in range(1,10):
                await Player(id=str(user), user_id=str(user), tag=f"#P{user}", elo=2000, highest_elo=2100).insert()
            final = await start_final(None, MAIN_GUILD, FINAL_CHANNEL)
            for user in range(1, 6):
                final = await add_entrant(final, str(user), f"Player {user}")
            await confirm_bracket(final, "manager")
            await ensure_wallet("9")
        elif action == "exercise":
            final = await resolve_final()
            node_id = next(node.id for node in final.nodes.values() if node.id.startswith("QF") and not node.bye)
            market = await open_market(final, node_id)
            for user, side in ((9, 0), (8, 1)):
                await apply_bet(BetAction(market_id=market.id, user_id=str(user), contestant=market.contestants[side],
                    amount=5000, expected_version=0, action_key=f"stake:{user}"))
            node = final.nodes[node_id]
            for revision, reverse in ((0, False), (1, True)):
                await submit_result(FinalResultSubmission(final_id=final.id, node_id=node_id, actor_id="manager",
                    operation_key=f"result:{revision}", expected_version=revision, contestant_revision=node.contestant_revision,
                    attempt=0, scores={user: ["3.100" if (side == 0) != reverse else "2.80"] * 7
                                      for side,user in enumerate(node.contestants)},
                    reason="Winner corrected" if revision else None))
                await recover_final(final)
                if not revision:
                    other_node = "SF2" if node_id in {"QF1", "QF2"} else "SF1"
                    other = await open_market(await resolve_final(), other_node)
                    await apply_bet(BetAction(market_id=other.id, user_id="9", contestant=other.contestants[0],
                        amount=10000, expected_version=0, action_key="spend-winnings"))
        elif action != "snapshot":
            raise ValueError("Unknown isolation probe action.")
        snapshot = {}
        for model in (Player, Season, EloFinal, Match, RecordedMatch, *FINAL_DOCUMENT_MODELS):
            snapshot[model.Settings.name] = await model.get_pymongo_collection().find().sort("_id", 1).to_list()
        print(json.dumps({"selected_environment": DATA_BASES[version], "database": database.name,
                          "snapshot": snapshot}, sort_keys=True, default=str))
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(run())

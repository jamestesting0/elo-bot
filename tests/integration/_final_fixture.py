import os
import unittest
from urllib.parse import urlsplit
from uuid import uuid4

from beanie import init_beanie
from pymongo import AsyncMongoClient

from common.finals.persistence import FINAL_DOCUMENT_MODELS, register_final_indexes, require_transactions
from database_models import BaseLayout, Clan, EloFinal, Helper, Match, Player, PlayerSeasonData, RecordedMatch, Season


class FinalMongoCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = os.environ.get("FINAL_TEST_MONGO_URI")
        if not uri:
            raise RuntimeError("FINAL_TEST_MONGO_URI must explicitly identify a disposable local replica set.")
        parsed = urlsplit(uri)
        if parsed.scheme != "mongodb" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("Final integration tests accept only a disposable loopback MongoDB URI.")
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
        self.database = self.client[f"elo_final_it_{uuid4().hex}_main"]
        self.addAsyncCleanup(self.cleanup_database)
        await init_beanie(database=self.database, document_models=[
            Match, Player, Season, EloFinal, BaseLayout, RecordedMatch, Helper, Clan, *FINAL_DOCUMENT_MODELS,
        ], skip_indexes=True)
        await register_final_indexes()
        await require_transactions()

    async def cleanup_database(self):
        if not self.database.name.startswith("elo_final_it_"):
            raise RuntimeError("Refusing to clean a database outside this test run.")
        await self.client.drop_database(self.database.name)
        await self.client.close()


class FinalScenarioCase(FinalMongoCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.archive = await Season(season_number=7, start=1, end=2, players=[
            PlayerSeasonData(player_id=str(user), elo=1000 - user) for user in range(1, 10)
        ]).insert()
        for user in range(1, 10):
            await Player(id=str(user), user_id=str(user), tag=f"#P{user}", elo=2000, highest_elo=2100).insert()

    async def roster(self, count):
        from common.finals.lifecycle import add_entrant, start_final
        from common.constants import MAIN_GUILD, FINAL_CHANNEL
        final = await start_final(None, MAIN_GUILD, FINAL_CHANNEL)
        for user in range(1, count + 1):
            final = await add_entrant(final, str(user), f"Player {user}")
        return final

    async def scheduled_pairing(self, final, node_id, *, is_war=True):
        from common.finals.matches import prepare_match, schedule_final_match
        source = await prepare_match(final, node_id)
        if is_war:
            source = await schedule_final_match(source, 1700000000, player=0)
            return await schedule_final_match(source, 1700003600, player=1)
        return await schedule_final_match(source, 1700000000)

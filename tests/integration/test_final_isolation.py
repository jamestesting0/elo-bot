import asyncio
import json
import os
import sys
from uuid import uuid4

from tests.integration._final_fixture import FinalMongoCase


class TestFinalIsolation(FinalMongoCase):
    async def test_separate_beanie_processes_keep_main_and_beta_independent(self):
        prefix = f"elo_final_it_{uuid4().hex}"
        databases = {version: f"{prefix}_{version}" for version in ("main", "beta")}
        for name in databases.values():
            self.addAsyncCleanup(self.client.drop_database, name)

        async def probe(version, action):
            environment = {"PATH": os.environ["PATH"], "PYTHONPATH": os.getcwd(),
                           "FINAL_TEST_MONGO_URI": os.environ["FINAL_TEST_MONGO_URI"]}
            process = await asyncio.create_subprocess_exec(sys.executable, "tests/integration/_final_environment_probe.py",
                version, databases[version], action, env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode())
            output = json.loads(stdout)
            self.assertEqual(output["selected_environment"], "elo_bot" if version == "main" else "elo_beta")
            return output["snapshot"]

        before_main = await probe("main", "seed")
        await probe("beta", "seed")
        beta = await probe("beta", "exercise")
        self.assertEqual(before_main, await probe("main", "snapshot"))
        main = await probe("main", "exercise")
        self.assertEqual(beta, await probe("beta", "snapshot"))
        for snapshot in (main, beta):
            wallet = next(row for row in snapshot["token_wallets"] if row["user_id"] == "9")
            self.assertEqual(wallet["balance"], -10000)
            self.assertEqual(sum(row["balance"] for row in snapshot["token_wallets"])
                             + sum(row["escrow"] for row in snapshot["final_betting_markets"]),
                             sum(row["amount"] for row in snapshot["token_ledger"] if row["kind"] == "grant"))
            self.assertEqual(snapshot["recorded_matches"], [])
            self.assertTrue(all(row["elo"] == 2000 and row["highest_elo"] == 2100 for row in snapshot["players"]))

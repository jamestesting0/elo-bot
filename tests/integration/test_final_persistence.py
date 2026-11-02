from pymongo.errors import DuplicateKeyError

from tests.integration._final_fixture import FinalMongoCase
from common.finals.persistence import transaction
from common.finals.types import FINAL_DISTRICTS
from database_models import EloFinal, Match, Participant
from database_models.final_models import FinalMatchResult, FinalResultOperation, FinalResultSnapshot
from database_models.token_models import TokenWallet


class TestFinalPersistence(FinalMongoCase):
    async def test_unique_active_and_season_claims_are_database_enforced(self):
        await EloFinal(season_number=1, active=True).insert()
        with self.assertRaises(DuplicateKeyError):
            await EloFinal(season_number=2, active=True).insert()
        with self.assertRaises(DuplicateKeyError):
            await EloFinal(season_number=1, active=False).insert()
        self.assertEqual(await EloFinal.count(), 1)

    async def test_seven_scores_and_checkpoint_survive_real_roundtrip(self):
        snapshot = FinalResultSnapshot(contestants=["42", "43"], scores={"42": ["3.100"] * 7, "43": ["0.00"] * 7},
                                       winner="42")
        match = Match(token="seven", matched_time=1, format="elo_final_seven", final_id="edition",
                      final_node_id="QF1", final_contestant_revision=1, final_attempt=1,
                      final_districts=list(FINAL_DISTRICTS), participants=[
                          Participant(user_id=user, format="elo_final_seven", districts=list(FINAL_DISTRICTS),
                                      bases=[None] * 7, result=list(snapshot.scores[user])) for user in ("42", "43")
                      ], final_result_operation=FinalResultOperation(operation_key="submit", result_id="result",
                                                                    version=1, after=snapshot))
        await match.insert()
        saved = await Match.get(match.id)
        self.assertEqual(saved.final_result_operation.after.scores["43"], ["0.00"] * 7)
        self.assertEqual(saved.final_districts, list(FINAL_DISTRICTS))
        self.assertEqual(saved.hosts, [None, None])

    async def test_transaction_aborts_all_writes_on_failure(self):
        async def failing(session):
            await TokenWallet.get_pymongo_collection().insert_one(
                {"_id": "42", "user_id": "42", "identity_claims": ["42"], "balance": 5000}, session=session)
            await EloFinal.get_pymongo_collection().insert_one({"season_number": 1}, session=session)
            raise RuntimeError("injected write failure")

        with self.assertRaises(RuntimeError):
            await transaction(failing)
        self.assertEqual(await TokenWallet.count(), 0)
        self.assertEqual(await EloFinal.count(), 0)

    async def test_result_operation_keys_are_unique(self):
        result = FinalMatchResult(id="r1", final_id="f", season_number=1, node_id="QF1", contestant_revision=1,
                                  attempt=1, version=1, operation_key="same", actor_id="manager", created_at=1,
                                  snapshot=FinalResultSnapshot(contestants=["42", "43"],
                                                               scores={"42": ["0.00"] * 7, "43": ["0.00"] * 7}))
        await result.insert()
        with self.assertRaises(DuplicateKeyError):
            await result.model_copy(update={"id": "r2", "version": 2}).insert()
        self.assertEqual(await FinalMatchResult.count(), 1)

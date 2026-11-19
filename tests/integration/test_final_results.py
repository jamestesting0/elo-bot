from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.core.finals import FinalError
from elo_bot.db import Match, Player, RecordedMatch
from elo_bot.db.final_models import FinalMatchResult, FinalPlayerProfile


class TestFinalResults(FinalScenarioCase):
    async def pairing(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1")
        return await final_for_view(), source

    def request(self, final, source, *, scores=None, winner=None, outcome="Played", reason=None):
        node = final.nodes[source.final_node_id]
        contestants = [participant.user_id for participant in source.participants]
        return FinalResultSubmission(final_id=final.id, node_id=node.id, actor_id="manager", operation_key="submit",
                                     expected_version=node.result_version, contestant_revision=node.contestant_revision,
                                     attempt=node.attempt, scores=scores or {contestants[0]: ["3.100"] * 7,
                                                                           contestants[1]: ["2.80"] * 7},
                                     winner=winner, outcome=outcome, reason=reason)

    async def test_submission_is_official_and_profiles_are_separate_from_ranked_state(self):
        final, source = await self.pairing()
        before = {user.user_id: user.model_dump() for user in await Player.find_all().to_list()}
        request = self.request(final, source)
        result = await submit_result(request)
        self.assertEqual((await final_for_view()).nodes["QF1"].result_id, result.id)
        self.assertEqual(result.snapshot.winner, source.participants[0].user_id)
        self.assertEqual(await RecordedMatch.count(), 0)
        after = {user.user_id: user.model_dump() for user in await Player.find_all().to_list()}
        self.assertEqual(before, after)
        profile = await FinalPlayerProfile.get(source.participants[0].user_id)
        self.assertEqual(profile.editions[str(final.id)].played, 1)
        self.assertEqual(profile.editions[str(final.id)].wins, 1)
        self.assertEqual(profile.editions[str(final.id)].district_count, 7)
        self.assertEqual(profile.editions[str(final.id)].destruction, 700)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_result_id, result.id)
        self.assertFalse(saved.final_result_operation.completed)
        self.assertEqual((await submit_result(request)).id, result.id)
        self.assertEqual(await FinalMatchResult.count(), 1)

    async def test_tied_scores_and_played_winner_overrides_are_refused(self):
        final, source = await self.pairing()
        tied = {participant.user_id: ["2.80"] * 7 for participant in source.participants}
        for request in (self.request(final, source, scores=tied),
                        self.request(final, source, winner=source.participants[1].user_id, reason="Override")):
            with self.subTest(winner=request.winner), self.assertRaises(FinalError):
                await submit_result(request)
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertIsNone((await final_for_view()).nodes["QF1"].result_id)

    async def test_forfeit_uses_explicit_winner_without_played_statistics(self):
        final, source = await self.pairing()
        scores = {participant.user_id: ["0.00"] * 7 for participant in source.participants}
        result = await submit_result(self.request(final, source, scores=scores,
                                     winner=source.participants[1].user_id, outcome="Forfeit", reason="No show"))
        stats = (await FinalPlayerProfile.get(source.participants[1].user_id)).editions[str(final.id)]
        self.assertEqual(result.snapshot.winner, source.participants[1].user_id)
        self.assertEqual(stats.played, 0)
        self.assertEqual(stats.district_count, 0)
        self.assertEqual(stats.forfeits, 1)
        self.assertEqual(await RecordedMatch.count(), 0)

    async def test_open_predictions_and_stale_result_forms_do_not_write(self):
        final, source = await self.pairing()
        request = self.request(final, source)
        await final.set({"prediction_state": "OPEN"})
        with self.assertRaises(FinalError):
            await submit_result(request)
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual((await Match.get(source.id)).participants[0].result, [None] * 7)

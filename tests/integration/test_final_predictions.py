import asyncio

from tests.integration._final_fixture import FinalScenarioCase
from common.finals.lifecycle import confirm_bracket, resolve_final
from common.finals.predictions import PredictionAction, apply_prediction_action, begin_prediction, change_prediction_state
from common.finals.prediction_ui import prediction_view
from common.finals.types import FinalError, NODE_IDS
from database_models.final_models import FinalPrediction
from common.finals.results import FinalResultSubmission, submit_result
from common.finals.recovery import recover_final


class TestFinalPredictions(FinalScenarioCase):
    async def draft(self, count=8):
        final = await confirm_bracket(await self.roster(count), "manager")
        final = await change_prediction_state(final, True)
        prediction = await begin_prediction(final, "9")
        return final, prediction

    async def action(self, final, prediction, action, node=None, choice=None):
        return await apply_prediction_action(PredictionAction(final_id=final.id, user_id="9",
            session_revision=prediction.session_revision, action=action, node_id=node, choice=choice))

    async def complete_draft(self, final, prediction):
        for stage, nodes in enumerate((NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:])):
            self.assertEqual(prediction.stage, stage)
            for node in nodes:
                if final.nodes[node].bye:
                    continue
                choices = (final.nodes[node].contestants if not final.nodes[node].feeders
                           else [prediction.draft[feeder] for feeder in final.nodes[node].feeders])
                prediction = await self.action(final, prediction, "pick", node, choices[0])
            components = prediction_view(final, prediction)
            body = components[0].to_dict()
            self.assertEqual(body["type"], 17)
            self.assertLess(len(str(body)), 6000)
            prediction = await self.action(final, prediction, "next")
        return prediction

    async def test_full_wizard_submits_once_and_keeps_official_picks_during_edits(self):
        final, draft = await self.draft()
        draft = await self.complete_draft(final, draft)
        submitted = await self.action(final, draft, "submit")
        original = dict(submitted.submitted)
        resumed = await begin_prediction(final, "9")
        resumed = await self.action(final, resumed, "back")
        resumed = await self.action(final, resumed, "pick", "QF1", final.nodes["QF1"].contestants[1])
        saved = await FinalPrediction.get(resumed.id)
        self.assertEqual(saved.submitted, original)
        self.assertNotIn("SF1", saved.draft)
        self.assertNotIn("F1", saved.draft)
        self.assertEqual(saved.draft["SF2"], original["SF2"])

    async def test_five_player_byes_are_autofilled(self):
        final, draft = await self.draft(5)
        self.assertEqual(len(draft.draft), 3)
        draft = await self.complete_draft(final, draft)
        submitted = await self.action(final, draft, "submit")
        self.assertEqual(len(submitted.submitted), 7)
        self.assertEqual(submitted.points, 0)

    async def test_stale_session_and_closure_preserve_submission(self):
        final, draft = await self.draft()
        resumed = await begin_prediction(final, "9")
        with self.assertRaises(FinalError):
            await self.action(final, draft, "pick", "QF1", final.nodes["QF1"].contestants[0])
        await change_prediction_state(final, False)
        with self.assertRaises(FinalError):
            await self.action(final, resumed, "pick", "QF1", final.nodes["QF1"].contestants[0])
        self.assertEqual((await FinalPrediction.get(draft.id)).submitted, {})

    async def test_submit_versus_close_commits_only_an_allowed_outcome(self):
        final, draft = await self.draft()
        draft = await self.complete_draft(final, draft)
        barrier = asyncio.Event()

        async def submit():
            async with asyncio.timeout(5):
                await barrier.wait()
            try:
                return await self.action(final, draft, "submit")
            except FinalError as error:
                return error

        async def close():
            async with asyncio.timeout(5):
                await barrier.wait()
            return await change_prediction_state(final, False)

        tasks = [asyncio.create_task(submit()), asyncio.create_task(close())]
        barrier.set()
        result, _ = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        saved = await FinalPrediction.get(draft.id)
        self.assertEqual((await resolve_final()).prediction_state, "CLOSED")
        self.assertEqual(bool(saved.submitted), not isinstance(result, FinalError))

    async def test_correction_rescores_original_submission_and_forfeit_removes_eligibility(self):
        final, draft = await self.draft(5)
        draft = await self.complete_draft(final, draft)
        prediction = await self.action(final, draft, "submit")
        original = dict(prediction.submitted)
        await change_prediction_state(final, False)
        node_id = next(node.id for node in final.nodes.values() if not node.bye and not node.feeders)
        await self.scheduled_pairing(await resolve_final(), node_id)
        for version, reverse, forfeit in ((0, False, False), (1, True, False), (2, True, True)):
            current = await resolve_final()
            node = current.nodes[node_id]
            winner = node.contestants[int(reverse)]
            await submit_result(FinalResultSubmission(final_id=final.id, node_id=node_id, actor_id="manager",
                operation_key=f"prediction-result:{version}", expected_version=version,
                contestant_revision=node.contestant_revision, attempt=node.attempt,
                scores={user: ["0.00" if forfeit else "3.100" if user == winner else "2.80"] * 7
                        for user in node.contestants}, winner=winner if forfeit else None,
                outcome="Forfeit" if forfeit else "Played", reason="Correction" if version else None))
            saved = await FinalPrediction.get(prediction.id)
            self.assertEqual(saved.submitted, original)
            self.assertEqual(saved.eligible, 0 if forfeit else 1)
            self.assertEqual(saved.points, int(not reverse and not forfeit))
            self.assertEqual(saved.scoring_revision, (await resolve_final()).result_revision)
            await recover_final(final)

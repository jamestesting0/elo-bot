from unittest.mock import MagicMock

import interactions as ipy

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalPrediction
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, request_cancellation
from elo_bot.features.finals.predictions import (
    PredictionAction, apply_prediction_action, begin_prediction, normalize_picks, pick_options,
)
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.recovery import recover_final
from tests.integration._final_ui_fixture import FinalUICase


def predictions_field(payload):
    return next((field.value for field in payload["embed"].fields if field.name == "**Predictions**"), None)


def predict_buttons(payload):
    return [component for row in payload["components"] for component in row.components
            if getattr(component, "custom_id", "").startswith("finalpredict|")]


class TestFinalBracketPredictions(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(self.final, "9")

    async def reviewed_prediction(self):
        prediction = await begin_prediction(self.final, "9")
        picks = {}
        for key in self.final.nodes:
            picks[key] = pick_options(self.final, picks, key)[0]
        await prediction.set({"draft": normalize_picks(self.final, picks), "stage": 3})
        return await FinalPrediction.get(prediction.id)

    async def test_predictions_live_on_the_pinned_bracket_post(self):
        await self.cog.final_predictions_open.callback.func(self.cog, self.context())
        current = await final_for_view()
        bracket = self.posts[current.bracket_message_id]
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(predictions_field(bracket.payload), "Open · 0 submitted")
        self.assertEqual([button.custom_id for button in predict_buttons(bracket.payload)],
                         [f"finalpredict|{self.final.id}"])
        prediction = await self.reviewed_prediction()
        submit = self.context(component=True)
        submit.custom_id = f"finalpred|{self.final.id}|9|{prediction.session_revision}|submit|-"
        await self.cog.final_prediction_action.callback.func(self.cog, submit)
        self.assertEqual(len((await FinalPrediction.get(prediction.id)).submitted), 7)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(predictions_field(bracket.edit.await_args.kwargs), "Open · 1 submitted")
        self.assertIn("Prediction submitted", submit.edit.await_args.kwargs["embed"].description)
        close = self.context()
        await self.cog.final_predictions_close.callback.func(self.cog, close)
        self.assertEqual(predictions_field(bracket.edit.await_args.kwargs), "Closed · 1 submitted")
        self.assertEqual(predict_buttons(bracket.edit.await_args.kwargs), [])
        self.assertTrue(close.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(len(self.posts), 1)

    async def test_failed_bracket_update_keeps_predictions_open_and_recovery_publishes_then_cancel_removes_button(self):
        self.channel.send.side_effect, send = (ipy.errors.Forbidden(MagicMock(status=403, reason="Fixture refusal")),
                                               self.channel.send.side_effect)
        ctx = self.context()
        await self.cog.final_predictions_open.callback.func(self.cog, ctx)
        current = await final_for_view()
        self.assertEqual(current.prediction_state, "OPEN")
        self.assertTrue(current.publication_pending)
        self.assertIn(str(IMPORTANT_EMOJI), ctx.send.await_args.args[0])
        self.channel.send.side_effect = send
        await recover_final(current, bot=self.bot)
        current = await final_for_view()
        bracket = self.posts[current.bracket_message_id]
        self.assertEqual(predictions_field(bracket.payload), "Open · 0 submitted")
        self.assertFalse(current.publication_pending)
        await request_cancellation(current, "Fixture cancellation")
        await recover_final(await final_for_view(), bot=self.bot)
        self.assertEqual((await final_for_view()).state, "CANCELLED")
        self.assertEqual(predict_buttons(bracket.edit.await_args.kwargs), [])
        self.assertEqual(predictions_field(bracket.edit.await_args.kwargs), "Closed · 0 submitted")

    async def test_submission_during_bracket_send_stays_pending_until_republished(self):
        await self.cog.final_predictions_open.callback.func(self.cog, self.context())
        prediction = await self.reviewed_prediction()
        current = await final_for_view()
        bracket = self.posts[current.bracket_message_id]
        original = bracket.edit.side_effect

        async def submit_while_editing(**payload):
            await apply_prediction_action(PredictionAction(final_id=current.id, user_id="9",
                session_revision=prediction.session_revision, action="submit"))

        bracket.edit.side_effect = submit_while_editing
        await current.set({"publication_pending": True})
        with self.assertRaisesRegex(FinalError, "incomplete"):
            await publish_bracket(self.bot, await final_for_view())
        self.assertTrue((await final_for_view()).publication_pending)
        bracket.edit.side_effect = original
        await publish_bracket(self.bot, await final_for_view())
        self.assertFalse((await final_for_view()).publication_pending)
        self.assertEqual(predictions_field(bracket.edit.await_args.kwargs), "Open · 1 submitted")
        self.assertEqual(len(self.posts), 1)

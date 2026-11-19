from unittest.mock import MagicMock

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.db import EloFinal
from elo_bot.db.final_draft_models import FinalResultDraft
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, reset_bracket
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalGateModal(FinalUICase):
    async def test_open_result_modal_rejects_reset_and_changed_pairing_before_creating_a_draft(self):
        self.final = await confirm_bracket(self.final, "9")
        opened = self.context()
        await self.cog.final_match_forfeit.callback.func(self.cog, opened, match="QF1",
                                                       winner=MagicMock(id=1), reason="Fixture forfeit")
        custom_id = opened.send_modal.await_args.args[0].custom_id
        original = await final_for_view()
        for field in ("result_version", "contestant_revision", "attempt"):
            with self.subTest(field=field):
                await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
                    {"$inc": {f"nodes.QF1.{field}": 1}})
                stale = self.context()
                stale.custom_id = custom_id
                await self.cog.final_result_modal.callback.func(self.cog, stale,
                    player1="\n".join(["0.00"] * 7), player2="\n".join(["0.00"] * 7), reason="Fixture forfeit")
                self.assertEqual(await FinalResultDraft.count(), 0)
                self.assertTrue(stale.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertFalse(stale.send.await_args.kwargs.get("components"))
                await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
                    {"$set": {f"nodes.QF1.{field}": getattr(original.nodes["QF1"], field)}})
        await reset_bracket(await final_for_view())
        before = (await final_for_view()).model_dump()
        stale = self.context()
        stale.custom_id = custom_id
        await self.cog.final_result_modal.callback.func(self.cog, stale, player1="\n".join(["0.00"] * 7),
                                                       player2="\n".join(["0.00"] * 7), reason="Fixture forfeit")
        self.assertEqual(await FinalResultDraft.count(), 0)
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual((await final_for_view()).model_dump(), before)
        self.assertTrue(stale.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertFalse(stale.send.await_args.kwargs.get("components"))

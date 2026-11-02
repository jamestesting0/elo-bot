import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId

import common.finals.recovery as recovery
from database_models import EloFinal, EloFinalMatch
from database_models.final_models import FinalMatchResult, FinalResultSnapshot
from database_models.season_models import FinalResultProgress


class TestFinalSelfHostInvitations(unittest.IsolatedAsyncioTestCase):
    async def test_old_final_result_only_recovers_existing_pending_invitations(self):
        # Given: an already completed Final result being revisited by startup recovery.
        result = FinalMatchResult.model_construct(
            id="old-result", node_id="QF1", source_match_id=None, helper_credits=[],
            snapshot=FinalResultSnapshot(contestants=["123", "456"], scores={
                "123": ["3.100"] * 7, "456": ["2.90"] * 7,
            }),
        )
        final = EloFinal.model_construct(
            id=PydanticObjectId(), season_number=7, nodes={"QF1": EloFinalMatch(
                id="QF1", contestants=["123", "456"], result_id=result.id,
            )}, result_progress={result.id: FinalResultProgress(helper_completed=True, completed=True)},
            repair_pending=True,
        )
        bot = MagicMock()
        with patch.object(recovery, "select_financial_work", new=AsyncMock(return_value=(final, []))), \
                patch.object(recovery, "resolve_final", new=AsyncMock(return_value=final)), \
                patch.object(EloFinal, "get_pymongo_collection", return_value=MagicMock(update_one=AsyncMock())), \
                patch.object(recovery.FinalBettingMarket, "find", return_value=SimpleNamespace(
                    to_list=AsyncMock(return_value=[]))), \
                patch.object(FinalMatchResult, "find", return_value=SimpleNamespace(
                    to_list=AsyncMock(return_value=[result]))), \
                patch.object(recovery, "invite_eligible_players", new=AsyncMock()) as invitations, \
                patch.object(recovery, "publish_result", new=AsyncMock()):
            # When: an old result is recovered without a new completion.
            await recovery.recover_final(final, bot=bot)
        # Then: recovery cannot initiate an invitation for an existing eligible player.
        invitations.assert_awaited_once_with(bot, ["123", "456"], pending_only=True)

    async def test_final_checks_invitation_after_durable_completion_before_publication(self):
        # Given: a committed Final result whose Helper completion has not yet been checkpointed.
        result = FinalMatchResult.model_construct(
            id="result", node_id="QF1", source_match_id=None, helper_credits=[],
            snapshot=FinalResultSnapshot(contestants=["123", "456"], scores={
                "123": ["3.100"] * 7, "456": ["2.90"] * 7,
            }),
        )
        progress = FinalResultProgress()
        final = EloFinal.model_construct(
            id=PydanticObjectId(), season_number=7, nodes={"QF1": EloFinalMatch(
                id="QF1", contestants=["123", "456"], result_id=result.id,
            )}, result_progress={result.id: progress}, repair_pending=True,
        )
        events = []

        async def checkpoint(_query, operation):
            if operation.get("$set", {}).get("result_progress.result.completed"):
                events.append("completed")
                progress.completed = True
                progress.helper_completed = True

        async def invite(bot, users, *, pending_only):
            self.assertTrue(progress.completed)
            self.assertEqual(users, ["123", "456"])
            self.assertFalse(pending_only)
            events.append("invitation")

        async def publish(*_args):
            self.assertIn("invitation", events)
            events.append("published")

        collection = MagicMock(update_one=AsyncMock(side_effect=checkpoint))
        with patch.object(recovery, "select_financial_work", new=AsyncMock(return_value=(final, []))), \
                patch.object(recovery, "resolve_final", new=AsyncMock(return_value=final)), \
                patch.object(EloFinal, "get_pymongo_collection", return_value=collection), \
                patch.object(recovery.FinalBettingMarket, "find", return_value=SimpleNamespace(
                    to_list=AsyncMock(return_value=[]))), \
                patch.object(FinalMatchResult, "find", return_value=SimpleNamespace(
                    to_list=AsyncMock(return_value=[result]))), \
                patch.object(recovery, "invite_eligible_players", new=AsyncMock(side_effect=invite)) as invitations, \
                patch.object(recovery, "publish_result", new=AsyncMock(side_effect=publish)):
            # When: recovery checkpoints the completed result.
            pending = await recovery.recover_final(final, bot=MagicMock())

        # Then: invitation eligibility is checked before result-message publication.
        self.assertEqual(pending, [])
        self.assertEqual(events[:3], ["completed", "invitation", "published"])
        invitations.assert_awaited_once()

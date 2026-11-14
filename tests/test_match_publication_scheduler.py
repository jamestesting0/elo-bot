import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions import match as match_commands


class TestMatchPublicationScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_failed_result_retry_does_not_skip_message_repair(self) -> None:
        bot = MagicMock()
        cog = match_commands.MatchCommands(bot)
        matches = [MagicMock(token="FIRST"), MagicMock(token="SECOND")]
        model = MagicMock()
        model.find.return_value.to_list = AsyncMock(return_value=matches)
        cog._finalize_match_results = AsyncMock(side_effect=[
            ipy.errors.HTTPException(MagicMock(status=503)), True,
        ])
        with patch.object(match_commands, "Match", model), \
                patch.object(match_commands, "repair_match_messages", new_callable=AsyncMock) as repair:
            await cog.repair_match_publications.callback.func(cog)

        self.assertEqual([call.args[0] for call in cog._finalize_match_results.await_args_list],
                         ["FIRST", "SECOND"])
        repair.assert_awaited_once_with(bot)
        model.find.assert_called_once_with({
            "result_operation.completed": True,
            "result_operation.published": False,
            "result_operation.cancelled": False,
        })

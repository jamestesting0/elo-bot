import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests._patch_sites import patch_sites
import elobot.extensions.match as match_module


class TestMatchPublicationScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_failed_result_retry_does_not_skip_message_repair(self) -> None:
        bot = MagicMock()
        cog = match_module.MatchCommands(bot)
        matches = [MagicMock(token="FIRST"), MagicMock(token="SECOND")]
        model = MagicMock()
        model.find.return_value.to_list = AsyncMock(return_value=matches)
        cog._finalize_match_results = AsyncMock(side_effect=[
            ipy.errors.HTTPException(MagicMock(status=503)), True,
        ])
        with patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'), model), \
                patch('elobot.extensions.match.lifecycle.repair_match_messages', new_callable=AsyncMock) as repair:
            await cog.repair_match_publications.callback.func(cog)

        self.assertEqual([call.args[0] for call in cog._finalize_match_results.await_args_list],
                         ["FIRST", "SECOND"])
        repair.assert_awaited_once_with(bot)
        model.find.assert_called_once_with({
            "result_operation.completed": True,
            "result_operation.published": False,
            "result_operation.cancelled": False,
        })

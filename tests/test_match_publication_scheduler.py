import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests._patch_sites import patch_sites
import elo_bot.extensions.match as match_module


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
        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'), model), \
                patch('elo_bot.extensions.match.lifecycle.repair_match_messages', new_callable=AsyncMock) as repair:
            await cog.repair_match_publications.callback.func(cog)

        self.assertEqual([call.args[0] for call in cog._finalize_match_results.await_args_list],
                         ["FIRST", "SECOND"])
        repair.assert_awaited_once_with(bot)
        model.find.assert_called_once_with({
            "result_operation.completed": True,
            "result_operation.published": False,
            "result_operation.cancelled": False,
        })

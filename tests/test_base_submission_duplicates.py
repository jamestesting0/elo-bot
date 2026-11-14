import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from pymongo.errors import OperationFailure

from tests._patch_sites import patch_sites
import elobot.extensions.base as base_module


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseSubmissionDuplicates(unittest.IsolatedAsyncioTestCase):
    async def test_submit_checks_entire_bank_before_downloading_or_posting(self) -> None:
        # Given an existing base, including qualities outside the Helper/public bank.
        for quality in ("Private", "Average", "Good", "Great", "Excellent"):
            with self.subTest(quality=quality):
                bot = MagicMock(spec=ipy.Client)
                bot.ext = {}
                bot.fetch_channel = AsyncMock()
                cog = base_module.BaseCommands(bot)
                ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
                with patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout')) as bases, \
                        patch_sites(('elobot.extensions.base.submissions.download_base_screenshot', 'elobot.extensions.base.manage.download_base_screenshot', 'elobot.extensions.base.private.download_base_screenshot'), new=AsyncMock()) as download, \
                        patch_sites(('elobot.extensions.base.search.PendingBaseSubmission', 'elobot.extensions.base.submissions.PendingBaseSubmission')) as pending:
                    bases.find_one = AsyncMock(return_value=SimpleNamespace(quality=quality))
                    # When the user submits the same link.
                    await cog.base_submit.callback.func(
                        cog, ctx, BASE_LINK, SimpleNamespace(url="https://cdn.example/base.webp"), "Good",
                    )
                # Then the lookup has no quality/owner restriction and rejects before side effects.
                bases.find_one.assert_awaited_once_with(bases.link == BASE_LINK)
                download.assert_not_awaited()
                bot.fetch_channel.assert_not_awaited()
                pending.assert_not_called()
                self.assertIn("already exists in the base bank", ctx.send.await_args.args[0])

    async def test_failed_duplicate_lookup_does_not_publish_submission(self) -> None:
        bot = MagicMock(spec=ipy.Client)
        bot.ext = {}
        cog = base_module.BaseCommands(bot)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        with patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout')) as bases, \
                patch_sites(('elobot.extensions.base.submissions.download_base_screenshot', 'elobot.extensions.base.manage.download_base_screenshot', 'elobot.extensions.base.private.download_base_screenshot'), new=AsyncMock()) as download:
            bases.find_one = AsyncMock(side_effect=OperationFailure("lookup failed"))
            await cog.base_submit.callback.func(
                cog, ctx, BASE_LINK, SimpleNamespace(url="https://cdn.example/base.webp"), "Good",
            )
        download.assert_not_awaited()
        self.assertIn("could not confirm", ctx.send.await_args.args[0])

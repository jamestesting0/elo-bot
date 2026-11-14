import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import PyMongoError
from tests._season_stats_fixture import row

import elobot.extensions.season as commands
from elobot.config.emojis import ERROR_EMOJI
from elobot.features.seasons.presentation import stats_controls, stats_embed
from elobot.features.seasons.stats import SeasonWindow, StatsAccumulator


class TestSeasonStatsInteractions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.fetch_user = AsyncMock(return_value=SimpleNamespace(id=100, display_name="Player"))
        self.cog = commands.SeasonCommands(self.bot)
        self.windows = [SeasonWindow("current", 20, 30), SeasonWindow("3", 10, 20), SeasonWindow("1", 0, 10)]
        accumulator = StatsAccumulator()
        accumulator.add(row("1", 25, ("Capital Peak",)), self.windows[0])
        self.stats = accumulator.finish(self.windows[0])
        self.seasons = MagicMock()
        self.seasons.sort.return_value.to_list = AsyncMock(return_value=[
            SimpleNamespace(season_number=1, start=0, end=10),
            SimpleNamespace(season_number=3, start=10, end=20),
        ])
        self.patch_seasons = patch.object(commands.Season, "find_all", return_value=self.seasons)
        self.patch_time = patch("elobot.extensions.season.unix_time", return_value=30)
        self.patch_stats = patch("elobot.extensions.season.load_season_stats", new_callable=AsyncMock,
                                 return_value=self.stats)
        self.patch_seasons.start()
        self.patch_time.start()
        self.load = self.patch_stats.start()
        self.addCleanup(self.patch_seasons.stop)
        self.addCleanup(self.patch_time.stop)
        self.addCleanup(self.patch_stats.stop)

    def context(self, kind=ipy.SlashContext):
        ctx = MagicMock(spec=kind)
        ctx.author.id = 100
        ctx.guild = None
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()
        ctx.edit_origin = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.client = self.bot
        self.bot.http = SimpleNamespace(post_followup=AsyncMock())
        self.bot.app = SimpleNamespace(id=99)
        ctx.token = "TEST_TOKEN"
        ctx.message = SimpleNamespace(
            interaction=SimpleNamespace(user=SimpleNamespace(id=100)),
            embeds=[stats_embed(self.windows[0], self.stats)],
            components=stats_controls(self.windows, 0),
        )
        return ctx

    async def test_initial_public_and_hidden_response_uses_current_page(self):
        # Given / When / Then: either visibility produces the current page and the same totals.
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                ctx = self.context()
                await self.cog.season_stats.callback.func(self.cog, ctx, hidden=hidden)
                ctx.defer.assert_awaited_once_with(ephemeral=hidden)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].title, "**Current Season Stats**")
                self.assertEqual(payload["components"][0].components[1].label, "1/3")
                self.assertEqual(payload["ephemeral"], hidden)
                self.assertIn("`1`", payload["embed"].fields[0].value)
                self.load.assert_awaited_with(self.windows[0])

    async def test_initial_query_failure_does_not_publish_zero_statistics(self):
        # Given: the stats query fails after the command is deferred.
        self.load.side_effect = PyMongoError("unavailable")
        ctx = self.context()
        # When: the command handles the failure.
        with self.assertLogs(level="ERROR"):
            await self.cog.season_stats.callback.func(self.cog, ctx)
        # Then: a visible error resolves loading without a fabricated embed.
        self.assertIn(str(ERROR_EMOJI), ctx.send.await_args.args[0])
        self.assertNotIn("embed", ctx.send.await_args.kwargs)

    async def test_no_archives_still_has_a_current_page(self):
        # Given: no recorded season boundaries.
        self.seasons.sort.return_value.to_list.return_value = []
        ctx = self.context()
        # When: the stats command is invoked.
        await self.cog.season_stats.callback.func(self.cog, ctx)
        # Then: the current page has an honest unknown start and one-page controls.
        self.assertIn("Not recorded", ctx.send.await_args.kwargs["embed"].description)
        self.assertEqual(ctx.send.await_args.kwargs["components"][0].components[1].label, "1/1")

    async def test_initial_stats_do_not_resolve_player_profiles(self):
        self.bot.fetch_user.side_effect = AssertionError("Stats do not need player profiles")
        ctx = self.context()
        await self.cog.season_stats.callback.func(self.cog, ctx)
        self.bot.fetch_user.assert_not_awaited()
        self.assertIn("Players: `2`", ctx.send.await_args.kwargs["embed"].fields[0].value)

    async def test_right_arrow_edits_the_selected_archived_page(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        payload = ctx.edit_origin.await_args.kwargs
        self.assertEqual(payload["embed"].title, "**Season 3 Stats**")
        self.assertEqual(payload["components"][0].components[1].label, "2/3")
        self.load.assert_awaited_with(self.windows[1])
        ctx.defer.assert_awaited_once_with(edit_origin=True)

    async def test_last_page_left_arrow_returns_to_middle(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.message.embeds = [stats_embed(self.windows[-1], self.stats)]
        ctx.message.components = stats_controls(self.windows, 2)
        ctx.custom_id = "season_stats_left|1"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertEqual(ctx.edit_origin.await_args.kwargs["embed"].title, "**Season 3 Stats**")

    async def test_page_button_opens_the_shared_modal(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_page|current"
        await self.cog.season_stats_page_button.callback.func(self.cog, ctx)
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "season_stats_page_modal|current")
        self.assertEqual(modal.components[0].custom_id, "page")
        self.load.assert_not_awaited()

    async def test_modal_jumps_to_oldest_and_disables_right_arrow(self):
        ctx = self.context(ipy.ModalContext)
        ctx.custom_id = "season_stats_page_modal|current"
        await self.cog.season_stats_page_modal.callback.func(self.cog, ctx, page="3")
        payload = ctx.edit.await_args.kwargs
        self.assertEqual(payload["embed"].title, "**Season 1 Stats**")
        self.assertEqual(payload["components"][0].components[1].label, "3/3")
        self.assertTrue(payload["components"][0].components[2].disabled)
        ctx.defer.assert_awaited_once_with(edit_origin=True)

    async def test_invalid_page_input_is_private_without_editing_original(self):
        for value in ("", "word", "0", "-1", "1.5", "4"):
            with self.subTest(value=value):
                ctx = self.context(ipy.ModalContext)
                ctx.custom_id = "season_stats_page_modal|current"
                await self.cog.season_stats_page_modal.callback.func(self.cog, ctx, page=value)
                payload = self.bot.http.post_followup.await_args.args[0]
                self.assertEqual(payload["flags"], 64)
                self.assertIn(str(ERROR_EMOJI), payload["content"])
                self.assertEqual(ctx.message.embeds[0].title, "**Current Season Stats**")
                ctx.edit.assert_not_awaited()
                ctx.send.assert_not_awaited()

    async def test_other_user_is_rejected_before_database_reads(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        ctx.author.id = 999
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        ctx.defer.assert_not_awaited()
        self.seasons.sort.return_value.to_list.assert_not_awaited()
        self.load.assert_not_awaited()

    async def test_missing_message_cannot_submit_a_page_jump(self):
        ctx = self.context(ipy.ModalContext)
        ctx.message = None
        ctx.custom_id = "season_stats_page_modal|current"
        await self.cog.season_stats_page_modal.callback.func(self.cog, ctx, page="2")
        ctx.edit.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_reset_between_view_and_arrow_reaches_newest_archive(self):
        self.seasons.sort.return_value.to_list.return_value.append(SimpleNamespace(season_number=4, start=20, end=25))
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertEqual(ctx.edit_origin.await_args.kwargs["embed"].title, "**Season 4 Stats**")
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"][0].components[1].label, "2/4")

    async def test_stale_modal_after_reset_does_not_jump_to_a_different_season(self):
        self.seasons.sort.return_value.to_list.return_value.append(SimpleNamespace(season_number=4, start=20, end=25))
        ctx = self.context(ipy.ModalContext)
        ctx.custom_id = "season_stats_page_modal|current"
        await self.cog.season_stats_page_modal.callback.func(self.cog, ctx, page="2")
        self.assertIn("season list changed", self.bot.http.post_followup.await_args.args[0]["content"])
        ctx.edit.assert_not_awaited()

    async def test_failed_page_read_preserves_original_and_uses_private_followup(self):
        self.load.side_effect = PyMongoError("unavailable")
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        with self.assertLogs(level="ERROR"):
            await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertEqual(self.bot.http.post_followup.await_args.args[0]["flags"], 64)
        ctx.edit_origin.assert_not_awaited()
        ctx.send.assert_not_awaited()
        self.assertEqual(ctx.message.components[0].components[1].label, "1/3")

    async def test_failed_discord_edit_has_separate_visible_error(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        ctx.edit_origin.side_effect = ipy.errors.HTTPException(MagicMock(status=500, reason="unavailable"))
        with self.assertLogs(level="ERROR"):
            await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertIn("Unable to update", self.bot.http.post_followup.await_args.args[0]["content"])
        self.assertEqual(ctx.message.embeds[0].title, "**Current Season Stats**")

    async def test_old_scope_controls_cannot_overwrite_a_changed_page(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|3"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        ctx.defer.assert_not_awaited()
        self.load.assert_not_awaited()

    async def test_deleted_archive_does_not_replace_existing_page(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.message.embeds = [stats_embed(self.windows[1], self.stats)]
        ctx.message.components = stats_controls(self.windows, 1)
        ctx.custom_id = "season_stats_left|3"
        self.seasons.sort.return_value.to_list.return_value = [SimpleNamespace(season_number=1, start=0, end=10)]
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.assertIn("no longer available", self.bot.http.post_followup.await_args.args[0]["content"])
        ctx.edit_origin.assert_not_awaited()

    async def test_outward_arrow_cannot_wrap_from_first_to_last_page(self):
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_left|current"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        ctx.edit_origin.assert_not_awaited()
        self.assertEqual(self.bot.http.post_followup.await_args.args[0]["flags"], 64)

    async def test_recreated_cog_can_navigate_without_saved_sessions(self):
        other_bot = MagicMock(spec=ipy.Client)
        other_bot.ext = {}
        other_bot.fetch_user = self.bot.fetch_user
        other_cog = commands.SeasonCommands(other_bot)
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        await other_cog.season_stats_navigation.callback.func(other_cog, ctx)
        self.assertEqual(ctx.edit_origin.await_args.kwargs["embed"].title, "**Season 3 Stats**")

    async def test_paginated_stats_do_not_resolve_player_profiles(self):
        self.bot.fetch_user.side_effect = AssertionError("Stats do not need player profiles")
        ctx = self.context(ipy.ComponentContext)
        ctx.custom_id = "season_stats_right|current"
        await self.cog.season_stats_navigation.callback.func(self.cog, ctx)
        self.bot.fetch_user.assert_not_awaited()
        self.assertIn("Players: `2`", ctx.edit_origin.await_args.kwargs["embed"].fields[0].value)

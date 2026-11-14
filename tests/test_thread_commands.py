import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

sys.path.append(os.getcwd())

from elobot.extensions.match import MatchCommands
from elobot.features.matches.lifecycle import confirm_match
from elobot.extensions.thread import ThreadCommands
from elobot.config.ids import LVL1_ROLES, MATCH_CHANNEL, STAFF_ROLE
from elobot.db import BaseStore, Participant
from tests._patch_sites import patch_sites
import elobot.features.matches.lifecycle as lifecycle_module


class TestThreadCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.ext = {}
        self.cog = ThreadCommands(self.bot)
        self.match_cog = MatchCommands(self.bot)

    async def test_initial_match_message_only_has_submit_result_control(self):
        # Given a confirmed match ready for its private thread.
        first_participant = MagicMock(user_id="111", districts=["D1", "D2", "D3"], submit_bases=False)
        second_participant = MagicMock(user_id="222", districts=["D1", "D2", "D3"], submit_bases=False)
        match = MagicMock(
            participants=[first_participant, second_participant],
            base_submission=False,
            thread=None,
        )
        match.save_changes = AsyncMock()
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        first_player = MagicMock(mention="<@111>")
        first_player.__str__.return_value = "First Player"
        second_player = MagicMock(mention="<@222>")
        second_player.__str__.return_value = "Second Player"

        initial_message = MagicMock(id=456, content="<@111>\n<@222>")
        initial_message.edit = AsyncMock()
        initial_message.pin = AsyncMock()
        thread = MagicMock(id=321)
        thread.send = AsyncMock(return_value=initial_message)
        match_channel = MagicMock()
        match_channel.create_private_thread = AsyncMock(return_value=thread)
        self.bot.fetch_channel = AsyncMock(return_value=match_channel)
        self.bot.fetch_user = AsyncMock(side_effect=[first_player, second_player])

        # When the match thread and its initial message are created.
        with (
                patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                patch.object(lifecycle_module.random, "choice", return_value=[0, 1, 0]),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await confirm_match(self.bot, "TOKEN")

        # Then only result submission is offered before the match ends.
        action_rows = thread.send.await_args.kwargs["components"]
        self.assertEqual(
            [(button.label, button.custom_id) for button in action_rows[0].components],
            [("Submit Result", "submit_button|TOKEN")],
        )

    async def test_initial_match_message_notes_loaded_preset_bases(self):
        # Given one valid preset and one expired preset for the assigned districts.
        first_participant = MagicMock(user_id="111", districts=["D1", "D2", "D3"], submit_bases=False)
        second_participant = MagicMock(user_id="222", districts=["D1", "D2", "D3"], submit_bases=False)
        match = MagicMock(
            participants=[first_participant, second_participant],
            base_submission=True,
            thread=None,
        )
        match.save_changes = AsyncMock()
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=MagicMock(matched_count=1))

        valid_preset = MagicMock(
            link="https://example.test/valid",
            image_url="https://example.test/valid.png",
            expiration_date=9_999_999_999,
        )
        expired_preset = MagicMock(
            link="https://example.test/expired",
            image_url="https://example.test/expired.png",
            expiration_date=1,
        )
        preset_players = [
            MagicMock(base_presets={"D1": valid_preset}),
            MagicMock(base_presets={"D2": expired_preset}),
        ]

        discord_players = [
            MagicMock(id=111, mention="<@111>", display_name="First Player"),
            MagicMock(id=222, mention="<@222>", display_name="Second Player"),
        ]
        discord_players[0].__str__.return_value = "First Player"
        discord_players[1].__str__.return_value = "Second Player"

        initial_message = MagicMock(id=456, content="<@111>\n<@222>")
        initial_message.edit = AsyncMock()
        initial_message.pin = AsyncMock()
        thread = MagicMock(id=321)
        thread.send = AsyncMock(return_value=initial_message)
        match_channel = MagicMock()
        match_channel.create_private_thread = AsyncMock(return_value=thread)
        self.bot.fetch_channel = AsyncMock(return_value=match_channel)
        self.bot.fetch_user = AsyncMock(side_effect=discord_players)

        # When the match thread and its initial embed are created.
        with (
                patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
                patch.object(lifecycle_module.random, "choice", return_value=[0, 1, 0]),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(side_effect=preset_players)
            await confirm_match(self.bot, "TOKEN")

        # Then only the player whose preset was loaded is identified in Notes.
        embed = thread.send.await_args.kwargs["embed"]
        notes = next(field for field in embed.fields if field.name == "**Notes**")
        self.assertEqual(notes.value, "- **[First Player](<https://discordapp.com/users/111>)** has preset bases.")

    async def test_delete_thread_button_allows_staff_roles_before_or_after_rename(self):
        for role, channel_name in ((LVL1_ROLES[2], "🟡 Match Thread"), (STAFF_ROLE, "🔴 Match Thread")):
            with self.subTest(role=role, channel_name=channel_name):
                ctx = MagicMock(spec=ipy.ComponentContext)
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock(return_value=MagicMock(id=123))
                ctx.author.roles = [MagicMock(id=role)]
                ctx.channel.name = channel_name
                response = MagicMock(custom_id="cancel")
                response.edit_origin = AsyncMock()
                self.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=response))

                await self.match_cog.delete_thread_button.callback.func(self.match_cog, ctx)

                self.bot.wait_for_component.assert_awaited_once()
                response.edit_origin.assert_awaited_once()

    async def test_thread_delete_check_is_lvl1_only(self):
        # Given separate level-1 and level-2-only callers.
        lvl1_ctx = MagicMock(spec=ipy.SlashContext)
        lvl1_ctx.author.has_role.side_effect = lambda role_id: int(role_id) == LVL1_ROLES[2]
        lvl2_ctx = MagicMock(spec=ipy.SlashContext)
        lvl2_ctx.author.has_role.side_effect = lambda role_id: int(role_id) == STAFF_ROLE
        command_check = self.cog.thread_delete.checks[0]

        # When the command authorization check runs.
        lvl1_allowed = await command_check(lvl1_ctx)
        lvl2_allowed = await command_check(lvl2_ctx)

        # Then only the level-1 caller is authorized.
        self.assertTrue(lvl1_allowed)
        self.assertFalse(lvl2_allowed)

    async def test_thread_delete_allows_thread_outside_match_categories(self):
        # Given a thread outside the configured match and support categories.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock(return_value=MagicMock(id=123))
        ctx.channel.name = "Project Thread"
        ctx.channel.parent_channel.id = 999
        response = MagicMock(custom_id="cancel")
        response.edit_origin = AsyncMock()
        self.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=response))

        # When an authorized caller invokes /thread delete there.
        await self.cog.thread_delete.callback.func(self.cog, ctx)

        # Then the normal deletion confirmation is offered.
        self.bot.wait_for_component.assert_awaited_once()
        response.edit_origin.assert_awaited_once()

    @patch("elobot.extensions.thread.remove_helper_task", new_callable=AsyncMock)
    @patch("elobot.features.base_store.storage.clear_match_base_stores", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.free_match_clans", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.Match")
    async def test_thread_delete_cleans_up_an_unreserved_match_without_releasing_slots(
            self, mock_match, mock_free_match_clans, mock_clear_base_stores, mock_remove_helper_task):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.name = "🔴 Match Thread"
        ctx.channel.parent_channel.id = MATCH_CHANNEL
        ctx.channel.delete = AsyncMock()
        ctx.author.id = 999

        prompt = MagicMock(id=123)
        ctx.send.return_value = prompt
        res = MagicMock()
        res.custom_id = "confirm"
        res.channel.id = 987
        res.edit_origin = AsyncMock()
        self.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=res))

        match = MagicMock(token="TOKEN", thread="987", result_operation=None, autowin_winner_id=None,
                          participants=[Participant(user_id="123"), Participant(user_id="456")])
        match.delete = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        with patch("elobot.extensions.thread.asyncio.sleep", new=AsyncMock()):
            await self.cog.thread_delete.callback.func(self.cog, ctx)

        mock_clear_base_stores.assert_not_awaited()
        mock_free_match_clans.assert_awaited_once_with(match)
        match.delete.assert_not_awaited()
        collection.delete_one.assert_awaited_once()
        ctx.channel.delete.assert_awaited_once()
        self.assertEqual(mock_remove_helper_task.await_count, 2)

    @patch("elobot.extensions.thread.remove_helper_task", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.free_match_clans", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.clear_match_base_stores", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.Match")
    async def test_on_thread_delete_releases_reservations_before_cleanup(
            self, mock_match, mock_clear_base_stores, mock_free_match_clans, _mock_remove_helper_task):
        thread = MagicMock()
        thread.id = 987
        thread.parent_channel.id = MATCH_CHANNEL
        event = MagicMock(thread=thread)

        match = MagicMock()
        match.thread = "987"
        match.is_war = False
        match.host_clans = ["#CLAN", None]
        store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="held")
        match.participants = [Participant(user_id="123", base_store=store, opponent_bases_stored=True),
                              Participant(user_id="456")]
        match.delete = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        async def release(*_args, **_kwargs):
            for participant in match.participants:
                participant.base_store = None
                participant.opponent_bases_stored = False

        mock_clear_base_stores.side_effect = release
        await self.cog.on_thread_delete.callback(self.cog, event)

        mock_clear_base_stores.assert_awaited_once_with(
            match, reset_stored_flags=True, bot=self.bot, reason="thread_deleted",
        )
        mock_free_match_clans.assert_awaited_once_with(match)
        match.delete.assert_not_awaited()
        collection.delete_one.assert_awaited_once()
        self.assertEqual(_mock_remove_helper_task.await_count, 2)
        self.assertIsNone(match.participants[0].base_store)
        self.assertFalse(match.participants[0].opponent_bases_stored)

    @patch("elobot.extensions.thread.remove_helper_task", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.free_match_clans", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.clear_match_base_stores", new_callable=AsyncMock)
    @patch("elobot.extensions.thread.Match")
    async def test_on_thread_delete_accepts_uncached_thread_id(
            self, mock_match, mock_clear_base_stores, mock_free_match_clans, _mock_remove_helper_task):
        event = MagicMock(thread="987")
        match = MagicMock(
            thread="987",
            participants=[Participant(user_id="123"), Participant(user_id="456")],
        )
        match.delete = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.on_thread_delete.callback(self.cog, event)

        mock_match.find_one.assert_awaited_once()
        mock_clear_base_stores.assert_awaited_once_with(
            match, reset_stored_flags=True, bot=self.bot, reason="thread_deleted",
        )
        mock_free_match_clans.assert_awaited_once_with(match)
        collection.delete_one.assert_awaited_once()
        self.assertEqual(_mock_remove_helper_task.await_count, 2)


if __name__ == "__main__":
    unittest.main()

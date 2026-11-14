import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.results import UpdateResult

import elobot.extensions.match as commands
import tests._stored_base_controls_fixture as fixtures
from elobot.config.ids import STAFF_ROLE
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import Match, Player


class TestMatchControlGeneration(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        fixtures.StoredBaseControlsStateMixin.setUp(self)
        self.match.old_msgs = [200, None]
        self.match.times = [500, None]
        self.ctx.message.id = 100
        self.ctx.message.embeds = [ipy.Embed(title="**Match Scheduled**", fields=[
            ipy.EmbedField(name="Time", value="Old time"),
            ipy.EmbedField(name="Host", value="Old host"),
            ipy.EmbedField(name="Clan", value="Old clan"),
        ])]
        self.ctx.author = MagicMock(id=999, mention="<@999>", display_name="Host", roles=[MagicMock(id=STAFF_ROLE)])
        self.ctx.values = ["#CLAN"]
        self.ctx.send_modal = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>"))
        self.bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(tag="#CLAN"))

    async def test_old_host_clan_and_result_controls_cannot_target_new_message(self) -> None:
        for name, prefix in (
                ("host_match_button", "match_host"),
                ("host_clan_select", "host_clan_select"),
                ("result_store_button", "match_result_store"),
        ):
            with self.subTest(control=name):
                self.ctx.custom_id = f"{prefix}|TOKEN|0"
                self.ctx.send.reset_mock()
                self.ctx.send_modal.reset_mock()
                with (
                    patch.object(Match, "set", new_callable=AsyncMock) as write,
                    patch.object(commands, "try_reserve_resource_clan", new=AsyncMock(return_value=MagicMock())),
                    patch.object(commands, "claim_resource_clan", new=AsyncMock(return_value=True)) as claim,
                ):
                    await getattr(self.cog, name).callback.func(self.cog, self.ctx)
                write.assert_not_awaited()
                claim.assert_not_awaited()
                self.ctx.send_modal.assert_not_awaited()
                self.assertEqual(self.ctx.message.edit.await_args.kwargs["components"], [])

    async def test_already_open_old_result_modal_cannot_write_after_restart(self) -> None:
        self.ctx.custom_id = "store_modal|TOKEN|0"
        with patch.object(Match, "set", new_callable=AsyncMock) as write:
            await self.cog.result_store_modal.callback.func(
                self.cog, self.ctx, result1="3.100", result2="", result3="",
            )
        write.assert_not_awaited()
        self.assertEqual(self.ctx.message.edit.await_args.kwargs["components"], [])

    async def test_host_change_that_loses_its_run_cannot_report_success(self) -> None:
        self.ctx.message.id = 200
        self.ctx.custom_id = "match_host|TOKEN|0"
        collection = MagicMock(update_one=AsyncMock(return_value=UpdateResult({"n": 0}, True)))
        with patch.object(Match, "get_pymongo_collection", return_value=collection):
            await self.cog.host_match_button.callback.func(self.cog, self.ctx)
        query, update = collection.update_one.await_args.args
        self.assertEqual(query["old_msgs.0"], 200)
        self.assertEqual(query["times.0"], 500)
        self.assertEqual(update, {"$set": {"hosts.0": "999"}})
        self.assertIsNone(self.match.hosts[0])
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_result_modal_commit_rejects_restart_during_user_lookup(self) -> None:
        self.ctx.message.id = 200
        self.ctx.custom_id = "store_modal|TOKEN|0"
        collection = MagicMock(update_one=AsyncMock(return_value=UpdateResult({"n": 0}, True)))
        with patch.object(Match, "get_pymongo_collection", return_value=collection):
            await self.cog.result_store_modal.callback.func(
                self.cog, self.ctx, result1="3.100", result2="", result3="",
            )
        query, _ = collection.update_one.await_args.args
        self.assertEqual(query["old_msgs.0"], 200)
        self.assertEqual(query["times.0"], 500)
        self.assertEqual(self.match.participants[0].result, [None, None, None])
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_current_result_modal_commits_results_and_submitter_together(self) -> None:
        self.ctx.message.id = 200
        self.ctx.message.embeds[0].title = "**Match Ended**"
        self.ctx.custom_id = "store_modal|TOKEN|0"
        collection = MagicMock(update_one=AsyncMock(return_value=UpdateResult({"n": 1}, True)))
        with (patch.object(Match, "get_pymongo_collection", return_value=collection),
              patch.object(Player, "find_one", new=AsyncMock(return_value=None))):
            await self.cog.result_store_modal.callback.func(
                self.cog, self.ctx, result1="3.100", result2="2.90", result3="1.80",
            )
        collection.update_one.assert_awaited_once()
        _, update = collection.update_one.await_args.args
        self.assertEqual(update["$set"]["submitters.0"], "999")
        self.assertEqual(self.match.participants[0].result, ["3.100", "2.90", "1.80"])
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])


    async def test_current_host_control_clears_stale_publication_description(self) -> None:
        self.ctx.message.id = 200
        self.ctx.message._channel_id = int(self.match.thread)
        self.ctx.channel_id = int(self.match.thread)
        self.ctx.message.components = []
        self.ctx.message.embeds[0].description = "Updating…"
        self.ctx.custom_id = "match_host|TOKEN|0"
        collection = MagicMock(update_one=AsyncMock(return_value=UpdateResult({"n": 1}, True)),
                               find_one=AsyncMock(return_value={"_id": self.match.id}))
        with patch.object(Match, "get_pymongo_collection", return_value=collection):
            await self.cog.host_match_button.callback.func(self.cog, self.ctx)
        self.assertEqual(self.match.hosts[0], "999")
        payload = self.ctx.message.edit.await_args.kwargs["embed"]
        self.assertEqual(payload["description"], "")
        self.assertEqual(payload["fields"][1]["value"], "**[Host](<https://discordapp.com/users/999>)**")
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])


if __name__ == "__main__":
    unittest.main()

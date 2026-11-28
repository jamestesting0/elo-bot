from unittest.mock import AsyncMock, MagicMock, patch
from io import BytesIO
from types import SimpleNamespace

import interactions as ipy
from PIL import Image

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.finals.head_to_head import render_head_to_head
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, reset_bracket, set_nickname
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.publication import bracket_signature, publish_bracket
from elo_bot.core.finals import FinalError
from elo_bot.config.ids import FINAL_CHANNEL, LVL1_ROLES
from elo_bot.db import EloFinal, Match
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.tokens.bets import close_market, open_market
from elo_bot.features.tokens.presentation import publish_market, market_signature
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.finals.replacement import replace_entrant
from elo_bot.features.finals.result_publication import publish_result
from elo_bot.features.finals.predictions import PredictionAction, begin_prediction, change_prediction_state, apply_prediction_action
from elo_bot.db.final_models import FinalPrediction
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.db.token_models import FinalBettingMarket


class TestFinalPublication(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        rendering = patch("elo_bot.features.finals.publication.render_bracket", return_value=b"bracket-image")
        rendering.start()
        self.addCleanup(rendering.stop)
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.message = MagicMock(id=100, pin=AsyncMock(), edit=AsyncMock())
        self.channel = MagicMock(send=AsyncMock(return_value=self.message), fetch_message=AsyncMock(return_value=self.message))
        self.messages = {"100": self.message}

        async def send(**payload):
            message_id = 99 + self.channel.send.await_count
            message = self.message if message_id == 100 else MagicMock(
                id=message_id, pin=AsyncMock(), edit=AsyncMock())
            message.author.id = 42
            message.nonce = payload.get("nonce")
            self.messages[str(message.id)] = message
            return message

        self.send = send
        self.channel.send.side_effect = send
        self.channel.id = FINAL_CHANNEL
        self.channel.fetch_message.side_effect = lambda message_id: self.messages[str(message_id)]
        self.channel.fetch_messages = AsyncMock(side_effect=lambda **kwargs: list(self.messages.values()))
        self.channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot = MagicMock(fetch_channel=AsyncMock(return_value=self.channel))
        self.bot.user.id = 42
        self.message.author.id = 42

    async def save_result(self):
        source = await self.scheduled_pairing(self.final, "QF1")
        await source.set({"thread": "777"})
        await bind_thread(self.final, source, "777")
        self.final = await final_for_view()
        node = self.final.nodes["QF1"]
        return await submit_result(FinalResultSubmission(
            final_id=self.final.id, node_id=node.id, actor_id="manager", operation_key="publication-result",
            expected_version=0, contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={user: ["3.100" if side == 0 else "2.80"] * 7
                    for side, user in enumerate(node.contestants)},
        ))

    async def test_completed_recovery_removes_pending_copy_without_duplicate_result_post(self):
        result = await self.save_result()
        self.assertTrue((await final_for_view()).repair_pending)
        self.assertEqual(await recover_final(self.final, bot=self.bot), [])
        current = await final_for_view()
        progress = current.result_progress[result.id]
        self.assertFalse(current.repair_pending)
        self.assertFalse(progress.published_repair_pending)
        self.assertTrue(progress.completed)
        self.assertEqual(self.channel.send.await_count, 3)
        self.assertNotEqual(current.bracket_message_id, progress.message_id)
        self.assertIn("**Recovery**", [field.name for field in self.channel.send.await_args_list[0].kwargs["embed"].fields])
        self.assertNotIn("**Recovery**", [field.name for field in self.message.edit.await_args.kwargs["embed"].fields])
        self.message.edit.assert_awaited_once()
        await recover_final(current, bot=self.bot)
        self.message.edit.assert_awaited_once()

    async def test_legacy_pending_post_edit_failure_retains_result_and_retry_clears_copy(self):
        result = await self.save_result()
        await recover_final(self.final, bot=self.bot)
        path = f"result_progress.{result.id}.published_repair_pending"
        await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id}, {"$unset": {path: ""}})
        self.message.edit.reset_mock()
        self.message.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        pending = await recover_final(await final_for_view(), bot=self.bot)
        self.assertEqual(pending, ["QF1 result saved; publication remains pending."])
        current = await final_for_view()
        self.assertEqual(current.nodes["QF1"].result_id, result.id)
        self.assertFalse(current.repair_pending)
        self.assertIsNotNone(current.result_progress[result.id].error)
        self.message.edit.assert_awaited_once()
        self.message.edit.side_effect = None
        self.assertEqual(await recover_final(current, bot=self.bot), [])
        current = await final_for_view()
        self.assertIsNone(current.result_progress[result.id].error)
        self.assertFalse(current.result_progress[result.id].published_repair_pending)
        self.assertNotIn("**Recovery**", [field.name for field in self.message.edit.await_args.kwargs["embed"].fields])
        self.assertEqual(self.channel.send.await_count, 3)

    async def test_pin_failure_preserves_bound_post_and_retry_does_not_duplicate(self):
        self.message.pin.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await final_for_view()
        self.assertEqual(current.bracket_message_id, "100")
        self.assertTrue(current.publication_pending)
        self.assertIsNotNone(current.publication_error)
        self.message.pin.side_effect = None
        await publish_bracket(self.bot, current)
        self.channel.send.assert_awaited_once()
        self.message.edit.assert_awaited_once()
        self.assertFalse((await final_for_view()).publication_pending)

    async def test_recovery_does_not_repost_a_deleted_cancelled_bracket(self):
        await self.final.set({"active": False, "state": "CANCELLED", "bracket_message_id": "999",
                              "bracket_send_attempted": True, "publication_pending": True})
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(MagicMock(status=404))
        self.channel.fetch_messages.return_value = []
        self.channel.fetch_messages.side_effect = None

        self.assertEqual(await recover_final(self.final, bot=self.bot), [])

        self.channel.send.assert_not_awaited()
        saved = await EloFinal.get(self.final.id)
        self.assertIsNone(saved.bracket_message_id)
        self.assertFalse(saved.publication_pending)

    async def test_header_change_alone_does_not_recreate_a_deleted_active_bracket(self):
        message = await publish_bracket(self.bot, self.final)
        current = await final_for_view()
        await current.set({"bracket_signature": "older-presentation"})
        message.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403))
        self.assertTrue(await recover_final(current, bot=self.bot))
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(MagicMock(status=404))
        self.channel.fetch_messages.side_effect = None
        self.channel.fetch_messages.return_value = []

        self.assertEqual(await recover_final(current, bot=self.bot), [])

        self.channel.send.assert_awaited_once()
        message.pin.assert_awaited_once()
        self.assertIsNone((await final_for_view()).bracket_message_id)

    async def test_recover_refreshes_bound_legacy_headers_without_reposting(self):
        result = await self.save_result()
        await recover_final(self.final, bot=self.bot)
        current = await final_for_view()
        lineup = await self.channel.send()
        await current.set({"lineup_message_id": str(lineup.id)})
        source = await self.scheduled_pairing(current, "QF2", is_war=False)
        announcement = await self.channel.send()
        await source.set({"thread": "778", "msg": str(announcement.id)})
        await bind_thread(current, source, "778")
        current = await final_for_view()
        await current.set({"bracket_signature": await bracket_signature(current)})
        progress = current.result_progress[result.id]
        legacy = (
            (lineup, "**Season 7 Elo Final · Lineup**", "**Lineup**"),
            (self.messages[current.bracket_message_id], "**Season 7 Final · Bracket**", "**Bracket**"),
            (self.messages[progress.message_id], "**Season 7 Final · QF1**", "**QF1 · Result**"),
            (self.messages[progress.public_message_id], "**Season 7 Final · QF1**", "**QF1 · Result**"),
        )
        for post, title, _ in legacy:
            embed = ipy.Embed(title=title, description="Original description")
            embed.set_image(url="attachment://original.png")
            post.embeds = [embed]
            post.edit.reset_mock()
        announcement._channel_id = FINAL_CHANNEL
        announcement.embeds = [ipy.Embed(title="**Match Scheduled**", author=ipy.EmbedAuthor(name="Elo Final · QF2"),
                                       description="Original announcement")]
        announcement.components = []
        self.bot.ext = {}
        cog = FinalCommands(self.bot)
        ctx = MagicMock(spec=ipy.SlashContext, bot=self.bot, guild_id=current.guild_id,
                        defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        sends = self.channel.send.await_count

        await cog.final_recover.callback.func(cog, ctx)

        self.assertIn("Final updates are complete.", ctx.send.await_args.args[0])
        self.assertEqual(self.channel.send.await_count, sends)
        for post, _, title in legacy:
            post.edit.assert_awaited_once()
            payload = post.edit.await_args.kwargs
            self.assertEqual(set(payload), {"embeds"})
            embed = payload["embeds"][0]
            self.assertEqual(embed.title, title)
            self.assertEqual(embed.author.name, "Season 7 Final")
            self.assertEqual(embed.description, "Original description")
            self.assertEqual(embed.to_dict()["image"]["url"], "attachment://original.png")
        published = (await Match.get(source.id)).public_message_publication
        self.assertFalse(published.pending)
        self.assertEqual(published.embeds[0]["title"], "**QF2 · Match Scheduled**")
        self.assertEqual(published.embeds[0]["author"]["name"], "Season 7 Final")
        self.assertEqual(published.embeds[0]["description"], "Original announcement")

    async def test_bracket_reset_and_recreation_preserve_the_saved_nickname_on_the_same_post(self):
        self.channel.guild.fetch_member.side_effect = lambda user: SimpleNamespace(
            id=user, display_name=f"Discord {user}", username=f"discord{user}")
        await publish_bracket(self.bot, self.final)
        await set_nickname(self.final, "1", "Captain | Finalist")
        await reset_bracket(self.final)
        await recover_final(self.final, bot=self.bot)
        self.assertIn("Captain | Finalist", self.message.edit.await_args.kwargs["embed"].description)

        recreated = await confirm_bracket(await final_for_view(), "manager")
        await recover_final(recreated, bot=self.bot)
        current = await final_for_view()
        self.assertEqual(current.player_nicknames["1"], "Captain | Finalist")
        self.assertIn("1", current.nickname_overrides)
        self.assertEqual(current.bracket_message_id, str(self.message.id))
        self.assertIn("**[Captain | Finalist](<https://discordapp.com/users/1?tab=wishlist>)**",
                      self.message.edit.await_args.kwargs["embed"].fields[0].value)
        self.channel.send.assert_awaited_once()

    async def test_nickname_change_refreshes_both_published_result_cards_in_place(self):
        result = await self.save_result()
        await recover_final(self.final, bot=self.bot)
        current = await final_for_view()
        progress = current.result_progress[result.id]
        post_ids = (progress.message_id, progress.public_message_id)
        initial_sends = self.channel.send.await_count
        await set_nickname(current, "1", "Captain")

        await recover_final(current, bot=self.bot)

        progress = (await final_for_view()).result_progress[result.id]
        self.assertEqual((progress.message_id, progress.public_message_id), post_ids)
        self.assertEqual(progress.published_nicknames, {"1": "Captain"})
        self.assertEqual(self.channel.send.await_count, initial_sends)
        for post_id in post_ids:
            embed = self.messages[post_id].edit.await_args.kwargs["embed"]
            self.assertIn("**[Captain](<https://discordapp.com/users/1?tab=wishlist>)**", str(embed.to_dict()))

    async def test_nickname_command_updates_the_open_match_title_and_public_players_field(self):
        source = await self.scheduled_pairing(self.final, "QF1")
        await source.set({"thread": "777", "msg": "100"})
        await bind_thread(self.final, source, "777")
        self.message._channel_id = FINAL_CHANNEL
        self.message.components = []
        self.message.embeds = [ipy.Embed(title="**Match Scheduled**", fields=[ipy.EmbedField("**Players**", "Old names")])]
        self.channel.__class__ = ipy.GuildText
        self.channel.id = FINAL_CHANNEL
        thread = MagicMock(spec=ipy.ThreadChannel, id=777, edit=AsyncMock())
        thread.name = "🔵 QF1 · discord1 vs. discord8"
        self.bot.fetch_channel.side_effect = lambda channel_id: thread if channel_id == 777 else self.channel
        self.bot.fetch_user = AsyncMock(side_effect=lambda user: ipy.User(
            id=user, client=self.bot, username=f"discord{user}", avatar=None))
        self.bot.ext = {}
        cog = FinalCommands(self.bot)
        ctx = MagicMock(spec=ipy.SlashContext, bot=self.bot, guild_id=self.final.guild_id,
                        defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        player = await self.bot.fetch_user(1)

        await cog.final_player_nick.callback.func(cog, ctx, player=player, nickname="Captain")

        thread.edit.assert_awaited_once_with(name="🔵 QF1 · Captain vs. discord8")
        fields = self.message.edit.await_args.kwargs["embed"]["fields"]
        self.assertIn("**[Captain](<https://discordapp.com/users/1?tab=wishlist>)**", fields[0]["value"])
        published = (await Match.get(source.id)).public_message_publication
        self.assertFalse(published.pending)
        self.channel.send.assert_not_awaited()

    async def test_uncertain_send_is_recovered_by_nonce_without_second_post(self):
        async def uncertain(**kwargs):
            self.message.nonce = kwargs["nonce"]
            raise ipy.errors.HTTPException(MagicMock(status=502, reason="Bad Gateway"))
        self.channel.send.side_effect = uncertain
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await final_for_view()
        self.assertIsNone(current.bracket_message_id)
        self.channel.fetch_messages = AsyncMock(return_value=[self.message])
        await publish_bracket(self.bot, current)
        self.channel.send.assert_awaited_once()
        self.assertEqual((await final_for_view()).bracket_message_id, "100")

    async def test_changed_revision_during_send_remains_pending_for_current_render(self):
        async def change(**kwargs):
            await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
                {"$set": {"player_nicknames.1": "Changed"}, "$inc": {"revision": 1}})
            return self.message
        self.channel.send.side_effect = change
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await final_for_view()
        self.assertEqual(current.bracket_message_id, "100")
        self.assertTrue(current.publication_pending)
        self.assertNotEqual(current.bracket_signature, await bracket_signature(current))
        await publish_bracket(self.bot, current)
        self.assertEqual((await final_for_view()).bracket_signature, await bracket_signature(current))

    async def test_closed_market_recovery_disables_the_published_betting_control(self):
        market = await open_market(self.final, "QF1")
        await publish_market(self.bot, self.final, market)
        payload = self.channel.send.await_args.kwargs
        self.assertFalse(payload["components"][0].disabled)
        self.assertEqual(payload["embed"].images[0].url, "attachment://final-matchup.png")
        self.assertEqual(payload["file"].description, "Player 1 (seed #1) vs. Player 8 (seed #8)")
        await close_market(market.id)
        await recover_final(self.final, bot=self.bot)
        self.assertTrue(self.message.edit.await_args.kwargs["components"][0].disabled)
        self.assertEqual(self.message.edit.await_args.kwargs["embed"].images[0].url,
                         "attachment://final-matchup.png")
        self.assertEqual(self.message.edit.await_args.kwargs["file"].description,
                         "Player 1 (seed #1) vs. Player 8 (seed #8)")
        self.assertNotIn("CLOSED", self.message.edit.await_args.kwargs["embed"].description)
        current = await FinalBettingMarket.get(market.id)
        self.assertEqual(current.announcement_signature, market_signature(current, await final_for_view()))
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertNotEqual((await final_for_view()).bracket_message_id, current.announcement_id)

    async def test_market_poster_uses_current_nicknames_and_seeds_and_refreshes_the_same_post(self):
        images = []

        async def send(**payload):
            with Image.open(BytesIO(payload["file"].file.getvalue())) as image:
                images.append(image.size)
            return await self.send(**payload)

        self.channel.send.side_effect = send
        market = await open_market(self.final, "QF1")
        await publish_market(self.bot, self.final, market)
        self.assertEqual(images, [(1672, 941)])
        self.channel.send.side_effect = self.send
        stored = await FinalBettingMarket.get(market.id)
        nickname = "Al Pal | (っ◔ᴗ◔)っ ♥ asmo ♥"
        member = SimpleNamespace(id=1, display_name=nickname, username="al.pal",
                                 display_avatar=SimpleNamespace(fetch=AsyncMock(return_value=b"")))
        self.channel.guild.fetch_member.side_effect = {"1": member}.get
        await self.final.set({"player_nicknames.1": nickname, "seed_elo.8": 2000})

        expected = render_head_to_head(("Al Pal asmo", "Player 8"), (None, None), (2, 1))

        async def edit(**payload):
            self.assertEqual(payload["file"].file.getvalue(), expected)

        self.messages[stored.announcement_id].edit.side_effect = edit
        with patch("elo_bot.features.finals.graphic_labels.FALLBACK_FONTS", ()):
            self.assertEqual(await recover_final(await final_for_view(), bot=self.bot), [])
        updated = await FinalBettingMarket.get(market.id)
        payload = self.messages[stored.announcement_id].edit.await_args.kwargs
        self.assertEqual(payload["file"].description, f"{nickname} (seed #2) vs. Player 8 (seed #1)")
        self.assertEqual(updated.announcement_id, stored.announcement_id)
        self.assertEqual(payload["attachments"], [])
        self.assertIsNone(updated.publication_error)

    async def test_failed_market_poster_keeps_betting_usable_and_retries_the_same_post(self):
        market = await open_market(self.final, "QF1")
        with patch("elo_bot.features.tokens.presentation.final_matchup_attachment", new=AsyncMock(return_value=None)):
            with self.assertRaises(FinalError):
                await publish_market(self.bot, self.final, market)
        stored = await FinalBettingMarket.get(market.id)
        self.assertEqual(stored.state, "OPEN")
        self.assertIsNotNone(stored.publication_error)
        payload = self.channel.send.await_args.kwargs
        self.assertEqual(payload["embed"].images, [])
        self.assertFalse(payload["components"][0].disabled)
        self.assertIsNone(payload["file"])
        await publish_market(self.bot, self.final, stored)
        updated = await FinalBettingMarket.get(market.id)
        self.assertEqual(updated.announcement_id, stored.announcement_id)
        self.assertIsNone(updated.publication_error)
        self.assertEqual(self.message.edit.await_args.kwargs["embed"].images[0].url,
                         "attachment://final-matchup.png")
        self.channel.send.assert_awaited_once()

    async def test_replaced_market_refunds_and_updates_its_poster_without_inventing_a_departed_seed(self):
        market = await open_market(self.final, "QF1")
        await publish_market(self.bot, self.final, market)
        stored = await FinalBettingMarket.get(market.id)
        replaced = await replace_entrant(await final_for_view(), "8", "9", "Replacement")
        self.assertEqual(await recover_final(replaced.final, bot=self.bot), [])
        refunded = await FinalBettingMarket.get(market.id)
        self.assertEqual(refunded.state, "REFUNDED")
        self.assertIsNone(refunded.publication_error)
        self.assertEqual(refunded.announcement_id, stored.announcement_id)
        self.assertEqual(self.messages[stored.announcement_id].edit.await_args.kwargs["file"].description,
                         "Player 1 (seed #1) vs. 8")
        current = await open_market(replaced.final, "QF1")
        await publish_market(self.bot, replaced.final, current)
        self.assertEqual(self.channel.send.await_args.kwargs["file"].description,
                         "Player 1 (seed #1) vs. Replacement (seed #8)")

    async def test_market_changed_during_send_remains_pending_then_recovers_latest_state(self):
        market = await open_market(self.final, "QF1")
        async def close(**kwargs):
            await close_market(market.id)
            return self.message
        self.channel.send.side_effect = close
        with self.assertRaises(FinalError):
            await publish_market(self.bot, self.final, market)
        self.channel.send.side_effect = self.send
        await recover_final(self.final, bot=self.bot)
        self.assertTrue(self.message.edit.await_args.kwargs["components"][0].disabled)
        self.assertEqual(self.channel.send.await_count, 2)
    async def test_prediction_click_during_publication_does_not_invalidate_the_bracket(self):
        await change_prediction_state(self.final, True)

        async def prediction_click(**payload):
            draft = await begin_prediction(await final_for_view(), "9")
            await apply_prediction_action(PredictionAction(final_id=self.final.id, user_id="9",
                session_revision=draft.session_revision, action="pick", node_id="QF1", choice="1"))
            return self.message

        self.channel.send.side_effect = prediction_click
        await publish_bracket(self.bot, await final_for_view())
        current = await final_for_view()
        self.assertFalse(current.publication_pending)
        self.assertEqual(current.bracket_signature, await bracket_signature(current))
        self.assertEqual((await FinalPrediction.get(f"{current.id}:9")).draft, {"QF1": "1"})
        self.channel.send.assert_awaited_once()

    async def test_unlocatable_posts_are_replaced_and_their_saved_ids_are_reused(self):
        self.channel.fetch_messages = AsyncMock(return_value=[])
        await self.final.set({"bracket_send_attempted": True})
        await publish_bracket(self.bot, self.final)
        self.assertEqual((await final_for_view()).bracket_message_id, "100")
        await publish_bracket(self.bot, self.final)
        self.assertEqual(self.channel.send.await_count, 1)
        market = await open_market(self.final, "QF2")
        await market.set({"announcement_attempted": True, "announcement_id": "999"})
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))
        await publish_market(self.bot, self.final, market)
        self.assertEqual((await FinalBettingMarket.get(market.id)).announcement_id, "101")
        self.channel.fetch_message.side_effect = lambda message_id: self.messages[str(message_id)]
        await publish_market(self.bot, self.final, market)
        self.assertEqual(self.channel.send.await_count, 2)
        result = await self.save_result()
        await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id}, {"$set": {
            f"result_progress.{result.id}.send_attempted": True}})
        await publish_result(self.bot, self.final, result)
        progress = (await final_for_view()).result_progress[result.id]
        self.assertEqual(progress.message_id, "102")
        self.assertTrue(progress.published)
        self.assertIsNone(progress.error)
        await publish_result(self.bot, self.final, result)
        self.assertEqual(self.channel.send.await_count, 4)

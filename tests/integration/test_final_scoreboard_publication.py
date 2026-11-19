from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.core.finals import FinalError
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.finals.result_publication import publish_result
from elo_bot.features.finals.results import FinalResultSubmission, submit_result

THREAD_ID = 777


class TestFinalScoreboardPublication(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        bracket = patch("elo_bot.features.finals.publication.render_bracket", return_value=b"bracket-image")
        bracket.start()
        self.addCleanup(bracket.stop)
        self.render = patch("elo_bot.features.finals.result_publication.render_scoreboard",
                            return_value=b"scoreboard-image")
        self.render_mock = self.render.start()
        self.addCleanup(self.render.stop)
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.next_id = 500
        self.channels = {THREAD_ID: self.channel(), self.final.channel_id: self.channel()}
        self.public_failure = None

        async def fetch_channel(channel_id):
            if int(channel_id) == self.final.channel_id and self.public_failure:
                raise self.public_failure
            return self.channels[int(channel_id)]

        self.bot = MagicMock(fetch_channel=AsyncMock(side_effect=fetch_channel))
        self.bot.user.id = 42

    def channel(self):
        channel = MagicMock()
        channel.messages = {}

        async def send(**payload):
            self.next_id += 1
            message = MagicMock(id=self.next_id, edit=AsyncMock(), pin=AsyncMock(), nonce=payload.get("nonce"))
            message.author.id = 42
            channel.messages[str(message.id)] = message
            return message

        channel.send = AsyncMock(side_effect=send)
        channel.fetch_message = AsyncMock(side_effect=lambda message_id: channel.messages[str(message_id)])
        channel.fetch_messages = AsyncMock(return_value=[])
        channel.guild.fetch_member = AsyncMock(return_value=None)
        return channel

    @property
    def thread(self):
        return self.channels[THREAD_ID]

    @property
    def public(self):
        return self.channels[self.final.channel_id]

    async def save(self, *, outcome="Played", version=0, key="scoreboard-result", reason=None, winner=None):
        node = (await final_for_view()).nodes["QF1"]
        if version == 0:
            source = await self.scheduled_pairing(self.final, "QF1")
            await source.set({"thread": str(THREAD_ID)})
            await bind_thread(self.final, source, str(THREAD_ID))
            node = (await final_for_view()).nodes["QF1"]
        zero = outcome == "Forfeit"
        return await submit_result(FinalResultSubmission(
            final_id=self.final.id, node_id="QF1", actor_id="manager", operation_key=key,
            expected_version=version, contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={user: ["0.00" if zero else ("3.100" if side == 0 else "2.80")] * 7
                    for side, user in enumerate(node.contestants)},
            outcome=outcome, reason=reason, winner=winner,
        ))

    @staticmethod
    def payload(channel):
        return channel.send.await_args.kwargs

    async def test_played_result_posts_scoreboard_in_thread_and_final_channel(self):
        result = await self.save()
        await publish_result(self.bot, self.final, result)
        for channel in (self.thread, self.public):
            payload = self.payload(channel)
            self.assertEqual(payload["file"].file_name, "scoreboard.png")
            self.assertEqual(payload["embed"].image.url, "attachment://scoreboard.png")
            self.assertNotIn("Barbarian Camp", payload["embed"].description)
            self.assertNotIn("**Total**", [field.name for field in payload["embed"].fields])
            self.assertEqual(payload["allowed_mentions"].to_dict(), ipy.AllowedMentions.none().to_dict())
        progress = (await final_for_view()).result_progress[result.id]
        self.assertTrue(progress.published)
        self.assertEqual({progress.message_id, progress.public_message_id}, {"501", "502"})

    async def test_forfeit_has_no_scoreboard(self):
        node = (await final_for_view()).nodes["QF1"]
        result = await self.save(outcome="Forfeit", reason="No show", winner=node.contestants[0])
        await publish_result(self.bot, self.final, result)
        self.render_mock.assert_not_called()
        for channel in (self.thread, self.public):
            self.assertIsNone(self.payload(channel)["file"])
            self.assertIn("Forfeit", self.payload(channel)["embed"].description)

    async def test_render_failure_falls_back_to_text_scores(self):
        self.render_mock.side_effect = FinalError("The scoreboard image is unavailable. The text result is still available.")
        result = await self.save()
        await publish_result(self.bot, self.final, result)
        embed = self.payload(self.public)["embed"]
        self.assertIsNone(self.payload(self.public)["file"])
        self.assertIn("Barbarian Camp", embed.description)
        self.assertIn("**Image**", [field.name for field in embed.fields])

    async def test_failed_public_post_stays_pending_until_retry_without_duplicate_thread_post(self):
        self.public_failure = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        result = await self.save()
        with self.assertRaises(FinalError):
            await publish_result(self.bot, self.final, result)
        progress = (await final_for_view()).result_progress[result.id]
        self.assertFalse(progress.published)
        self.assertIsNone(progress.public_message_id)
        self.thread.send.assert_awaited_once()
        self.public_failure = None
        await publish_result(self.bot, self.final, result)
        progress = (await final_for_view()).result_progress[result.id]
        self.assertTrue(progress.published)
        self.assertIsNotNone(progress.public_message_id)
        self.thread.send.assert_awaited_once()
        self.public.send.assert_awaited_once()

    async def test_correction_edits_both_posts_with_a_new_scoreboard(self):
        first = await self.save()
        await publish_result(self.bot, self.final, first)
        self.assertEqual(await recover_final(await final_for_view(), bot=self.bot), [])
        progress = (await final_for_view()).result_progress[first.id]
        thread_post = self.thread.messages[progress.message_id]
        public_post = self.public.messages[progress.public_message_id]
        sends = (self.thread.send.await_count, self.public.send.await_count)
        renders = self.render_mock.call_count
        corrected = await self.save(version=1, key="scoreboard-correction", reason="Score typo")
        await publish_result(self.bot, self.final, corrected)
        self.assertEqual((self.thread.send.await_count, self.public.send.await_count), sends)
        for post in (thread_post, public_post):
            self.assertEqual(post.edit.await_args.kwargs["file"].file_name, "scoreboard.png")
        self.assertEqual(self.render_mock.call_count, renders + 1)
        corrected_progress = (await final_for_view()).result_progress[corrected.id]
        self.assertEqual((corrected_progress.message_id, corrected_progress.public_message_id),
                         (progress.message_id, progress.public_message_id))

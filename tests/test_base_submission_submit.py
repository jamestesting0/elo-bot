import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

sys.path.append(os.getcwd())

import elobot.extensions.base as base_commands
from elobot.config.emojis import CROSS_EMOJI, TICK_EMOJI
from tests._patch_sites import BASE_MODULE_FAMILY, patch_family
import validators
from elobot.config.ids import BASE_SUBMISSION_CHANNEL
from elobot.features.base_submission.presentation import build_review_components


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseSubmissionSubmit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = base_commands
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.fetch_channel = AsyncMock()
        self.bot.fetch_user = AsyncMock()
        self.cog = self.module.BaseCommands(self.bot)
        self.enterContext(patch_family(
            BASE_MODULE_FAMILY, "BaseLayout", MagicMock(find_one=AsyncMock(return_value=None)),
        ))

    @staticmethod
    def _field(embed: ipy.Embed, name: str) -> str:
        return next(field.value for field in embed.fields if field.name == name)

    async def test_submit_checks_bank_then_posts_review_without_modifying_existing_bases(self) -> None:
        # Given: a valid player submission and an available review channel.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author = MagicMock(id=123, mention="<@123>", display_name="Submitter Name")
        screenshot = MagicMock(spec=ipy.Attachment, url="https://cdn.example/source.png")
        builder = MagicMock(spec=ipy.User, id=456, display_name="Builder Name")
        review_channel = MagicMock()
        review_channel.send = AsyncMock()
        review_message = MagicMock(id=9001)
        review_message.edit = AsyncMock()
        events: list[str] = []
        def enable(**kwargs):
            events.append("enable")
            self.assertTrue(self.cog._review_locks[9001].statistics().locked)
            return MagicMock(components=[kwargs["components"]])

        review_message.edit.side_effect = enable
        ctx.send.side_effect = lambda *_args, **_kwargs: events.append("acknowledge")
        review_message.attachments = []
        review_channel.send.return_value = review_message
        self.bot.fetch_channel.return_value = review_channel
        uploaded_image = MagicMock()
        uploaded_image.attachments = [MagicMock(url="https://cdn.example/durable.webp")]
        self.bot.owner.send = AsyncMock(return_value=uploaded_image)
        with (
            patch.object(validators, "url", return_value=True),
            patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock(return_value=True)) as download,
            patch_family(BASE_MODULE_FAMILY, "delete_file", new=AsyncMock()) as cleanup,
            patch.object(os.path, "exists", return_value=True),
            patch_family(BASE_MODULE_FAMILY, "BaseLayout") as base_layout,
            patch_family(BASE_MODULE_FAMILY, "PendingBaseSubmission", create=True) as pending_model,
        ):
            base_layout.find_one = AsyncMock(return_value=None)
            pending = MagicMock()
            pending.insert = AsyncMock()
            pending.insert.side_effect = lambda: events.append("persist")
            pending_model.return_value = pending
            # When: the player submits the base for review.
            await self.cog.base_submit.callback.func(
                self.cog, ctx, BASE_LINK, screenshot, "Good", builder, "Fresh anti-meta layout",
            )

        # Then: the review post is complete after checking the bank without changing existing bases.
        self.bot.fetch_channel.assert_awaited_once_with(BASE_SUBMISSION_CHANNEL)
        download.assert_awaited_once()
        cleanup.assert_awaited_once()
        base_layout.assert_not_called()
        base_layout.find_one.assert_awaited_once_with(base_layout.link == BASE_LINK)
        self.assertEqual(pending_model.call_args.kwargs["id"], "9001")
        self.assertEqual(pending_model.call_args.kwargs["link"], BASE_LINK)
        self.assertEqual(pending_model.call_args.kwargs["image_url"], "https://cdn.example/durable.webp")
        self.assertEqual(pending_model.call_args.kwargs["proposed_quality"], "Good")
        pending.insert.assert_awaited_once()
        self.bot.owner.send.assert_awaited_once()
        sent = review_channel.send.await_args.kwargs
        self.assertNotIn("file", sent)
        self.assertEqual(sent["embed"].description, BASE_LINK)
        self.assertEqual(
            self._field(sent["embed"], "Submitter"),
            "**[Submitter Name](<https://discordapp.com/users/123>)**",
        )
        self.assertEqual(
            self._field(sent["embed"], "Builder"),
            "**[Builder Name](<https://discordapp.com/users/456>)**",
        )
        self.assertNotIn("<@", self._field(sent["embed"], "Submitter"))
        self.assertNotIn("ID:", self._field(sent["embed"], "Builder"))
        self.assertEqual(self._field(sent["embed"], "Submission Reason"), "Fresh anti-meta layout")
        self.assertEqual(sent["embed"].images[0].url, "https://cdn.example/durable.webp")
        buttons = sent["components"].components
        self.assertEqual([button.style for button in buttons],
                         [ipy.ButtonStyle.SUCCESS, ipy.ButtonStyle.DANGER, ipy.ButtonStyle.SECONDARY])
        self.assertEqual([button.emoji for button in buttons], [TICK_EMOJI, CROSS_EMOJI, ipy.PartialEmoji(name="🔍")])
        self.assertTrue(all(button.disabled for button in buttons))
        enabled = review_message.edit.await_args.kwargs["components"].components
        self.assertTrue(all(not button.disabled for button in enabled))
        self.assertEqual(events, ["persist", "enable", "acknowledge"])
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertFalse(self.cog._review_locks)

    async def test_missing_durable_upload_attachment_stops_before_review_post(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author = MagicMock(id=123, display_name="Submitter Name")
        screenshot = MagicMock(spec=ipy.Attachment, url="https://cdn.example/source.png")
        uploaded_image = MagicMock(attachments=[])
        self.bot.owner.send = AsyncMock(return_value=uploaded_image)

        with (
            patch.object(validators, "url", return_value=True),
            patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock(return_value=True)),
            patch_family(BASE_MODULE_FAMILY, "delete_file", new=AsyncMock()),
            patch.object(os.path, "exists", return_value=True),
            patch_family(BASE_MODULE_FAMILY, "PendingBaseSubmission", create=True) as pending_model,
        ):
            await self.cog.base_submit.callback.func(self.cog, ctx, BASE_LINK, screenshot, "Good")

        self.bot.fetch_channel.assert_not_awaited()
        pending_model.assert_not_called()
        self.assertIn("uploading the image", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_failed_control_enable_reports_saved_submission_and_preserves_retry(self):
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.author = MagicMock(id=123, display_name="Submitter")
        review_message = MagicMock(id=9001, edit=AsyncMock(side_effect=ipy.errors.HTTPException(
            MagicMock(status=500), "synthetic edit failure")))
        channel = MagicMock(send=AsyncMock(return_value=review_message))
        self.bot.fetch_channel.return_value = channel
        self.bot.owner.send = AsyncMock(return_value=MagicMock(attachments=[MagicMock(url="https://image/base")]))
        with (patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock(return_value=True)),
              patch.object(os.path, "exists", return_value=False),
              patch_family(BASE_MODULE_FAMILY, "PendingBaseSubmission") as model):
            pending = MagicMock(insert=AsyncMock(), delete=AsyncMock())
            model.return_value = pending
            await self.cog.base_submit.callback.func(
                self.cog, ctx, BASE_LINK, MagicMock(url="https://image/input"), "Good")
        pending.insert.assert_awaited_once()
        pending.delete.assert_not_awaited()
        self.assertIn("saved", ctx.send.await_args.args[0])
        self.assertIn("buttons", ctx.send.await_args.args[0])
        self.assertIn("retry", ctx.send.await_args.args[0])
        self.assertNotIn("was submitted for review", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_success_reply_requires_enabled_controls_in_discord_response(self):
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.author = MagicMock(id=123, display_name="Submitter")
        posted = MagicMock(id=9001, components=[build_review_components(disabled=True)])
        posted.edit = AsyncMock(return_value=posted)
        self.bot.fetch_channel.return_value = MagicMock(send=AsyncMock(return_value=posted))
        self.bot.owner.send = AsyncMock(return_value=MagicMock(attachments=[MagicMock(url="https://image/base")]))
        with (patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock(return_value=True)),
              patch.object(os.path, "exists", return_value=False),
              patch_family(BASE_MODULE_FAMILY, "PendingBaseSubmission") as model):
            model.return_value = MagicMock(insert=AsyncMock())
            await self.cog.base_submit.callback.func(
                self.cog, ctx, BASE_LINK, MagicMock(url="https://image/input"), "Good")
        model.return_value.insert.assert_awaited_once()
        self.assertIn("saved", ctx.send.await_args.args[0])
        self.assertIn("retry", ctx.send.await_args.args[0])
        self.assertNotIn("was submitted for review", ctx.send.await_args.args[0])

    async def test_malformed_link_never_downloads_or_posts(self) -> None:
        # Given: a URL that is not a Clash layout link.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        screenshot = MagicMock(spec=ipy.Attachment, url="https://cdn.example/source.png")
        with (
            patch.object(validators, "url", return_value=True),
            patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock()) as download,
        ):
            # When: the malformed link is submitted.
            await self.cog.base_submit.callback.func(self.cog, ctx, "https://example.com/layout", screenshot, "Good")

        # Then: no download, channel fetch, or database access can occur.
        download.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    async def test_invalid_url_never_downloads_or_posts(self) -> None:
        # Given: text that is not a URL.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        screenshot = MagicMock(spec=ipy.Attachment, url="https://cdn.example/source.png")
        with (
            patch.object(validators, "url", return_value=False),
            patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock()) as download,
        ):
            # When: the invalid URL is submitted.
            await self.cog.base_submit.callback.func(self.cog, ctx, "not a URL", screenshot, "Good")

        # Then: no download or review post occurs.
        download.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

sys.path.append(os.getcwd())

import cogs.base_commands as base_commands
from common.constants import BASE_TIERS, COMMAND_CHANNEL, CROSS_EMOJI, TICK_EMOJI
from tests._base_submission_fixtures import configure_pending_resolution


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseSubmissionCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = base_commands
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.user = MagicMock(id=42)
        self.bot.fetch_channel = AsyncMock()
        self.bot.fetch_user = AsyncMock()
        self.cog = self.module.BaseCommands(self.bot)
        self.enterContext(patch.object(
            self.module, "BaseLayout", MagicMock(find_one=AsyncMock(return_value=None)),
        ))
        self.pending = self._pending()
        self.pending_model = MagicMock()
        self.pending_model.get = AsyncMock(return_value=self.pending)
        pending_patch = patch.object(self.module, "PendingBaseSubmission", self.pending_model, create=True)
        pending_patch.start()
        self.addCleanup(pending_patch.stop)

    @staticmethod
    def _field(embed: ipy.Embed, name: str) -> str:
        return next(field.value for field in embed.fields if field.name == name)

    @staticmethod
    def _pending() -> MagicMock:
        pending = MagicMock()
        pending.link = BASE_LINK
        pending.submitter_id = "123"
        pending.submitter_display = "[Submitter Name](<https://discordapp.com/users/123>)"
        pending.builder_id = "456"
        pending.builder_display = "[Builder Name](<https://discordapp.com/users/456>)"
        pending.district = "Barbarian Camp"
        pending.proposed_quality = "Good"
        pending.reason = "Fresh anti-meta layout"
        pending.image_url = "https://cdn.example/review.webp"
        pending.delete = AsyncMock()
        configure_pending_resolution(pending)
        return pending

    @staticmethod
    def _review(status: str = "Pending") -> MagicMock:
        embed = ipy.Embed(
            title="Base Submission Review",
            description=BASE_LINK,
            fields=[
                ipy.EmbedField(
                    name="Submitter",
                    value="[Submitter Name](<https://discordapp.com/users/123>)",
                ),
                ipy.EmbedField(
                    name="Builder",
                    value="[Builder Name](<https://discordapp.com/users/456>)",
                ),
                ipy.EmbedField(name="District", value="Barbarian Camp"),
                ipy.EmbedField(name="Proposed Quality", value="Good"),
                ipy.EmbedField(name="Submission Reason", value="Fresh anti-meta layout"),
                ipy.EmbedField(name="Review Status", value=status),
            ],
        )
        embed.set_image("attachment://base-submission.webp")
        message = MagicMock(spec=ipy.Message)
        message.id = 9001
        message.embeds = [embed]
        message.attachments = [MagicMock(url="https://cdn.example/review.webp")]
        message.edit = AsyncMock()
        message.components = [ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.SUCCESS, label="Accept", custom_id="base_submission_accept"),
            ipy.Button(style=ipy.ButtonStyle.DANGER, label="Reject", custom_id="base_submission_reject"),
        )]
        return message

    @staticmethod
    def _context(message: MagicMock, *, modal: bool) -> MagicMock:
        context_type = ipy.ModalContext if modal else ipy.ComponentContext
        ctx = MagicMock(spec=context_type)
        ctx.message = message
        ctx.author = MagicMock(id=789, mention="<@789>", display_name="Reviewer Name")
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.channel = MagicMock()
        ctx.channel.fetch_message = AsyncMock(return_value=message)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.send_modal = AsyncMock()
        return ctx

    async def test_accept_button_opens_raw_quality_and_required_submitter_builder_modal(self) -> None:
        # Given: pending submission data exists independently of the rendered review.
        review = self._review()
        review.embeds = []
        review.attachments = []
        ctx = self._context(review, modal=False)

        # When: a moderator selects Accept.
        await self.cog.accept_submission_button.callback.func(self.cog, ctx)

        # Then: the raw modal requires one builder and defaults it to the submitter.
        ctx.send_modal.assert_awaited_once()
        modal = ctx.send_modal.await_args.args[0]
        labels = modal["data"]["components"]
        self.assertEqual([label["type"] for label in labels], [18, 18, 18])
        options = labels[0]["component"]["options"]
        self.assertEqual([option["value"] for option in options], list(BASE_TIERS))
        self.assertEqual([option["value"] for option in options if option["default"]], ["Good"])
        builder = labels[1]["component"]
        self.assertEqual(builder["type"], int(ipy.ComponentType.USER_SELECT))
        self.assertEqual(builder["custom_id"], "builder")
        self.assertEqual(builder["default_values"], [{"id": "123", "type": "user"}])
        self.assertEqual((builder["min_values"], builder["max_values"]), (1, 1))
        self.assertTrue(builder["required"])
        self.assertEqual(labels[2]["component"]["style"], 2)
        self.assertFalse(labels[2]["component"]["required"])
        self.pending_model.get.assert_awaited_once_with("9001")

    async def test_reject_button_opens_required_reason_modal(self) -> None:
        # Given: a pending review whose component snapshot omits the attachment.
        review = self._review()
        review.attachments = []
        ctx = self._context(review, modal=False)

        # When: a moderator selects Reject.
        await self.cog.reject_submission_button.callback.func(self.cog, ctx)

        # Then: the rejection modal still opens and requires a paragraph reason.
        ctx.send_modal.assert_awaited_once()
        modal = ctx.send_modal.await_args.args[0]
        reason = modal.components[0]
        self.assertEqual(modal.custom_id, "base_submission_reject_modal")
        self.assertEqual(reason.style, ipy.TextStyles.PARAGRAPH)
        self.assertTrue(reason.required)

    async def test_reject_uses_pending_submission_without_parsing_review_message(self) -> None:
        # Given: the modal message has no embed fields or attachments to parse.
        review = self._review()
        review.embeds = []
        review.attachments = []
        ctx = self._context(review, modal=True)
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter

        # When: a moderator submits a reason through the modal.
        await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Too weak")

        # Then: pending state and modal input alone complete the decision.
        self.pending_model.get.assert_awaited_once_with("9001")
        ctx.channel.fetch_message.assert_not_awaited()
        review.edit.assert_awaited_once()
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")
        ctx.edit.assert_not_awaited()
        submitter.send.assert_awaited_once()
        self.assertNotIn("invalid", " ".join(str(call) for call in ctx.send.await_args_list).lower())

    async def test_reject_updates_source_review_with_disabled_emoji_buttons(self) -> None:
        # Given: a pending review and a submitter who can receive the decision.
        review = self._review()
        review.edit = AsyncMock()
        ctx = self._context(review, modal=True)
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter

        # When: a moderator rejects the submission.
        await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Copied layout")

        # Then: the source review itself is finalized with disabled, recognizable controls.
        review.edit.assert_awaited_once()
        ctx.edit.assert_not_awaited()
        resolved = review.edit.await_args.kwargs
        self.assertIn("Rejected by **[Member 789](<https://discordapp.com/users/789>)**",
                      self._field(resolved["embed"], "Review Status"))
        buttons = resolved["components"].components
        self.assertTrue(all(button.disabled for button in buttons))
        self.assertEqual([button.emoji for button in buttons], [TICK_EMOJI, CROSS_EMOJI, ipy.PartialEmoji(name="🔍")])
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")

    async def test_accept_inserts_selected_quality_and_notifies_submitter(self) -> None:
        # Given: a pending review for a new base.
        review = self._review()
        ctx = self._context(review, modal=True)
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter
        with patch.object(self.module, "BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock(return_value=None)
            base_layout.get = AsyncMock(return_value=None)
            created = MagicMock()
            created.insert = AsyncMock()
            base_layout.return_value = created

            # When: the moderator confirms Great with a note.
            await self.cog.accept_submission_modal.callback.func(
                self.cog, ctx,
                quality=["Great"],
                builder=[],
                note="Verified in FCs",
            )

        # Then: the chosen quality and review attachment are persisted and announced.
        self.assertEqual(base_layout.call_args.kwargs["quality"], "Great")
        self.assertEqual(base_layout.call_args.kwargs["image_url"], "https://cdn.example/review.webp")
        self.assertEqual(base_layout.call_args.kwargs["records"], [])
        self.assertEqual(base_layout.call_args.kwargs["builder_id"], "456")
        created.insert.assert_awaited_once()
        notification = submitter.send.await_args.kwargs["embed"]
        self.assertEqual(notification.title, "Base Submission Accepted")
        self.assertEqual(self._field(notification, "Quality"), "Great")
        self.assertEqual(self._field(notification, "Moderator Note"), "Verified in FCs")
        self.assertEqual(notification.images[0].url, "https://cdn.example/review.webp")
        resolved = review.edit.await_args.kwargs
        self.assertIn("Accepted by **[Member 789](<https://discordapp.com/users/789>)**",
                      self._field(resolved["embed"], "Review Status"))
        self.assertTrue(all(button.disabled for button in resolved["components"].components))
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "accepted")
        ctx.edit.assert_not_awaited()
        self.assertIn("accepted", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_accept_persists_selected_builder_or_elobot_sentinel(self) -> None:
        for selected, expected in (("999", "999"), ("42", None)):
            with self.subTest(selected=selected):
                self.pending = self._pending()
                self.pending_model.get.return_value = self.pending
                ctx = self._context(self._review(), modal=True)
                submitter = MagicMock(mention="<@123>", send=AsyncMock())
                self.bot.fetch_user.return_value = submitter
                with patch.object(self.module, "BaseLayout") as base_layout:
                    base_layout.find_one = AsyncMock(return_value=None)
                    base_layout.get = AsyncMock(return_value=None)
                    created = MagicMock(insert=AsyncMock())
                    base_layout.return_value = created

                    await self.cog.accept_submission_modal.callback.func(
                        self.cog, ctx, quality=["Great"], builder=[selected], note="",
                    )

                self.assertEqual(base_layout.call_args.kwargs["builder_id"], expected)
                created.insert.assert_awaited_once()
                self.assertEqual(self.pending.resolution.phase, "accepted")
                if expected is not None:
                    resolved = ctx.message.edit.await_args.kwargs["embed"]
                    self.assertEqual(self._field(resolved, "Builder"),
                                     "**[Member 999](<https://discordapp.com/users/999>)**")

    async def test_accept_existing_base_link_rejects_review_without_changing_base(self) -> None:
        # Given: the submitted link already exists in the base database.
        ctx = self._context(self._review(), modal=True)
        existing = MagicMock(records=[["1", 2, 83]], quality="Average")
        existing.save_changes = AsyncMock()
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter
        with patch.object(self.module, "BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock(return_value=existing)

            # When: the moderator tries to accept the duplicate link.
            await self.cog.accept_submission_modal.callback.func(self.cog, ctx, quality=["Excellent"], note="")

        # Then: the persisted rejection explains the duplicate without changing the existing base.
        existing.save_changes.assert_not_awaited()
        self.assertEqual(existing.quality, "Average")
        self.assertEqual(self.pending.resolution.phase, "rejected")
        self.pending.delete.assert_not_awaited()
        ctx.message.edit.assert_awaited_once()
        edited = ctx.message.edit.await_args.kwargs
        self.assertEqual(self._field(edited["embed"], "Review Status"),
                         "Rejected by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertIn("already exists in the base bank", self._field(edited["embed"], "Rejection Reason"))
        self.assertTrue(all(button.disabled for button in edited["components"].components))
        submitter.send.assert_awaited_once()
        self.assertIn("rejected", ctx.send.await_args.args[0])
        self.assertEqual(self.cog._review_locks, {})

    async def test_reject_notifies_via_fallback_without_base_layout_access(self) -> None:
        # Given: a pending review whose submitter has closed DMs.
        review = self._review()
        ctx = self._context(review, modal=True)
        response = MagicMock(status=403, reason="Forbidden")
        response.headers = {}
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock(side_effect=ipy.errors.Forbidden(response, "DMs closed"))
        self.bot.fetch_user.return_value = submitter
        fallback = MagicMock()
        fallback.send = AsyncMock()
        self.bot.fetch_channel.return_value = fallback
        with patch.object(self.module, "BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock()
            # When: the moderator rejects with a required reason.
            await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Copied layout")

        # Then: no database call occurs and the fallback embed carries the reason.
        base_layout.assert_not_called()
        base_layout.find_one.assert_not_awaited()
        self.bot.fetch_channel.assert_awaited_once_with(COMMAND_CHANNEL)
        notification = fallback.send.await_args.kwargs["embed"]
        self.assertEqual(notification.title, "Base Submission Rejected")
        self.assertEqual(self._field(notification, "Reason"), "Copied layout")
        self.assertEqual(notification.images[0].url, "https://cdn.example/review.webp")
        resolved = review.edit.await_args.kwargs
        self.assertIn("Rejected by **[Member 789](<https://discordapp.com/users/789>)**",
                      self._field(resolved["embed"], "Review Status"))
        self.assertTrue(all(button.disabled for button in resolved["components"].components))
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")
        ctx.edit.assert_not_awaited()
        self.assertIn("rejected", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_competing_decisions_have_exactly_one_winner(self) -> None:
        # Given: two moderators submit decisions for the same pending review.
        review = self._review()
        first = self._context(review, modal=True)
        second = self._context(review, modal=True)

        async def edit_review(**kwargs: ipy.Embed | ipy.ActionRow) -> None:
            review.embeds = [kwargs["embed"]]
            review.components = [kwargs["components"]]

        review.edit.side_effect = edit_review
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter
        with patch.object(self.module, "BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock(return_value=None)
            base_layout.get = AsyncMock(return_value=None)
            created = MagicMock()
            created.insert = AsyncMock()
            base_layout.return_value = created

            # When: Accept and Reject compete.
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(partial(
                    self.cog.accept_submission_modal.callback.func, self.cog, first, quality=["Great"], note="",
                ))
                tasks.start_soon(partial(
                    self.cog.reject_submission_modal.callback.func, self.cog, second, reason="Too weak",
                ))

        # Then: only the winning decision performs final side effects.
        accepted = self._field(review.embeds[0], "Review Status").startswith("Accepted")
        self.assertEqual(created.insert.await_count, int(accepted))
        self.assertEqual(submitter.send.await_count, 1)
        self.assertEqual(review.edit.await_count, 2)
        self.assertEqual(first.edit.await_count + second.edit.await_count, 0)
        self.assertEqual(first.send.await_count + second.send.await_count, 2)
        self.assertEqual(self.cog._review_locks, {})

    async def test_modal_decision_does_not_read_review_status_field(self) -> None:
        # Given: pending state exists while the rendered status contains stale text.
        review = self._review("Accepted by <@789>")
        ctx = self._context(review, modal=True)
        submitter = MagicMock(mention="<@123>")
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter
        with patch.object(self.module, "BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock()
            # When: the moderator submits a rejection reason through the modal.
            await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Modal decision")

        # Then: pending state and modal input win without consulting rendered status.
        base_layout.assert_not_called()
        base_layout.find_one.assert_not_awaited()
        review.edit.assert_awaited_once()
        self.assertIn("Rejected", self._field(review.edit.await_args.kwargs["embed"], "Review Status"))
        submitter.send.assert_awaited_once()
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")
        ctx.edit.assert_not_awaited()
        self.assertEqual(self.cog._review_locks, {})

    async def test_stale_open_modal_is_rejected_when_pending_submission_is_gone(self) -> None:
        # Given: another moderator already consumed the pending submission.
        ctx = self._context(self._review(), modal=True)
        ctx.message.embeds = []
        self.pending_model.get.return_value = None

        # When: a stale modal is submitted after the buttons were disabled.
        await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Late review")

        # Then: no side effect occurs and the transient lock is released.
        self.bot.fetch_user.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        self.assertIn("no longer pending", ctx.send.await_args.args[0].lower())
        self.assertEqual(self.cog._review_locks, {})


if __name__ == "__main__":
    unittest.main()

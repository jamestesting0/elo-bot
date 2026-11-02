import unittest
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import cogs.base_commands as base_commands
from common.utils import ComponentsV2ModalContext
from tests._base_submission_fixtures import configure_pending_resolution


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseSubmissionModalDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.module = base_commands
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.fetch_user = AsyncMock()
        self.cog = self.module.BaseCommands(self.bot)

        self.review_message = MagicMock(spec=ipy.Message)
        self.review_message.id = 9001
        self.review_message.edit = AsyncMock()
        self.reviewer = MagicMock(id=789)

        self.client = MagicMock()
        self.client.app.id = 42
        self.client.cache.place_message_data.return_value = self.review_message
        self.client.cache.get_message.return_value = self.review_message
        self.client.cache.get_member.return_value = None
        self.client.cache.get_guild.return_value = MagicMock(
            fetch_member=AsyncMock(
                side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Member {user_id}"),
            ),
        )
        self.client.cache.get_user.return_value = self.reviewer
        self.client.http.post_initial_response = AsyncMock()

        self.pending = MagicMock(
            link=BASE_LINK,
            image_url="https://cdn.example/review.webp",
            submitter_id="123",
            submitter_display="[Submitter](<https://discordapp.com/users/123>)",
            builder_id="456",
            builder_display="[Builder](<https://discordapp.com/users/456>)",
            district="Barbarian Camp",
            proposed_quality="Good",
            reason="Fresh anti-meta layout",
        )
        self.pending.delete = AsyncMock()
        configure_pending_resolution(self.pending)
        self.enterContext(patch.object(
            self.module, "BaseLayout", MagicMock(find_one=AsyncMock(return_value=None)),
        ))

    def _modal_context(self, custom_id: str, components: list[dict]) -> ComponentsV2ModalContext:
        payload = {
            "id": "1750000000000000000",
            "application_id": "42",
            "type": int(ipy.InteractionType.MODAL_RESPONSE),
            "token": "redacted",
            "version": 1,
            "locale": "en-US",
            "channel_id": "77",
            "guild_id": "66",
            "user": {
                "id": "789",
                "username": "reviewer",
                "discriminator": "0",
                "avatar": None,
            },
            "message": {"id": "9001"},
            "data": {"custom_id": custom_id, "components": components},
        }
        ctx = ComponentsV2ModalContext.from_dict(self.client, payload)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    def _component_context(self, custom_id: str) -> ipy.ComponentContext:
        payload = {
            "id": "1750000000000000000",
            "application_id": "42",
            "type": int(ipy.InteractionType.MESSAGE_COMPONENT),
            "token": "redacted",
            "version": 1,
            "locale": "en-US",
            "channel_id": "77",
            "user": {
                "id": "789",
                "username": "reviewer",
                "discriminator": "0",
                "avatar": None,
            },
            "message": {"id": "9001"},
            "data": {
                "custom_id": custom_id,
                "component_type": int(ipy.ComponentType.BUTTON),
            },
        }
        return ipy.ComponentContext.from_dict(self.client, payload)

    async def test_accept_button_posts_components_v2_modal_payload(self) -> None:
        ctx = self._component_context("base_submission_accept")
        with patch.object(self.module, "PendingBaseSubmission") as pending_model:
            pending_model.get = AsyncMock(return_value=self.pending)
            await self.cog.accept_submission_button.call_callback(
                partial(self.cog.accept_submission_button.callback.func, self.cog),
                ctx,
            )

        payload = self.client.http.post_initial_response.await_args.args[0]
        self.assertEqual(payload["type"], int(ipy.CallbackType.MODAL))
        self.assertEqual(payload["data"]["custom_id"], "base_submission_accept_modal")
        labels = payload["data"]["components"]
        self.assertEqual([label["type"] for label in labels], [18, 18, 18])
        selected = [
            option["value"]
            for option in labels[0]["component"]["options"]
            if option["default"]
        ]
        self.assertEqual(selected, ["Good"])
        builder = labels[1]["component"]
        self.assertEqual(builder["type"], int(ipy.ComponentType.USER_SELECT))
        self.assertEqual(builder["custom_id"], "builder")
        self.assertEqual(builder["default_values"], [{"id": "123", "type": "user"}])
        self.assertEqual((builder["min_values"], builder["max_values"]), (1, 1))
        self.assertTrue(builder["required"])

    async def test_reject_button_posts_required_reason_modal_payload(self) -> None:
        ctx = self._component_context("base_submission_reject")
        with patch.object(self.module, "PendingBaseSubmission") as pending_model:
            pending_model.get = AsyncMock(return_value=self.pending)
            await self.cog.reject_submission_button.call_callback(
                partial(self.cog.reject_submission_button.callback.func, self.cog),
                ctx,
            )

        payload = self.client.http.post_initial_response.await_args.args[0]
        self.assertEqual(payload["type"], int(ipy.CallbackType.MODAL))
        self.assertEqual(payload["data"]["custom_id"], "base_submission_reject_modal")
        reason = payload["data"]["components"][0]["components"][0]
        self.assertEqual(reason["custom_id"], "reason")
        self.assertTrue(reason["required"])

    async def test_accept_callback_receives_quality_and_note_from_raw_modal_payload(self) -> None:
        ctx = self._modal_context(
            "base_submission_accept_modal",
            [
                {
                    "type": 18,
                    "component": {
                        "type": 3,
                        "custom_id": "quality",
                        "values": ["Excellent"],
                    },
                },
                {
                    "type": 18,
                    "component": {
                        "type": 5,
                        "custom_id": "builder",
                        "values": ["999"],
                    },
                },
                {
                    "type": 18,
                    "component": {
                        "type": 4,
                        "custom_id": "note",
                        "value": "Chosen in the modal",
                    },
                },
            ],
        )
        submitter = MagicMock()
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter

        with (
            patch.object(self.module, "PendingBaseSubmission") as pending_model,
            patch.object(self.module, "BaseLayout") as base_layout,
        ):
            pending_model.get = AsyncMock(return_value=self.pending)
            base_layout.find_one = AsyncMock(return_value=None)
            base_layout.get = AsyncMock(return_value=None)
            created = MagicMock()
            created.insert = AsyncMock()
            base_layout.return_value = created

            await self.cog.accept_submission_modal.call_callback(
                partial(self.cog.accept_submission_modal.callback.func, self.cog),
                ctx,
            )

        self.assertEqual(
            ctx.responses,
            {"quality": ["Excellent"], "builder": ["999"], "note": "Chosen in the modal"},
        )
        self.assertEqual(base_layout.call_args.kwargs["quality"], "Excellent")
        self.assertEqual(base_layout.call_args.kwargs["builder_id"], "999")
        notification = submitter.send.await_args.kwargs["embed"]
        self.assertEqual(next(field.value for field in notification.fields if field.name == "Moderator Note"),
                         "Chosen in the modal")
        self.review_message.edit.assert_awaited_once()
        status = next(
            field.value
            for field in self.review_message.edit.await_args.kwargs["embed"].fields
            if field.name == "Review Status"
        )
        self.assertEqual(status, "Accepted by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertTrue(all(
            button.disabled
            for button in self.review_message.edit.await_args.kwargs["components"].components
        ))

    async def test_reject_callback_receives_reason_from_raw_modal_payload(self) -> None:
        ctx = self._modal_context(
            "base_submission_reject_modal",
            [{
                "type": 1,
                "components": [{
                    "type": 4,
                    "custom_id": "reason",
                    "value": "Reason entered in the modal",
                }],
            }],
        )
        submitter = MagicMock()
        submitter.send = AsyncMock()
        self.bot.fetch_user.return_value = submitter

        with (
            patch.object(self.module, "PendingBaseSubmission") as pending_model,
            patch.object(self.module, "BaseLayout") as base_layout,
        ):
            pending_model.get = AsyncMock(return_value=self.pending)

            await self.cog.reject_submission_modal.call_callback(
                partial(self.cog.reject_submission_modal.callback.func, self.cog),
                ctx,
            )

        self.assertEqual(ctx.responses, {"reason": "Reason entered in the modal"})
        base_layout.assert_not_called()
        notification = submitter.send.await_args.kwargs["embed"]
        self.assertEqual(next(field.value for field in notification.fields if field.name == "Reason"),
                         "Reason entered in the modal")
        self.review_message.edit.assert_awaited_once()
        status = next(
            field.value
            for field in self.review_message.edit.await_args.kwargs["embed"].fields
            if field.name == "Review Status"
        )
        self.assertEqual(status, "Rejected by **[Member 789](<https://discordapp.com/users/789>)**")
        self.pending.delete.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")


if __name__ == "__main__":
    unittest.main()

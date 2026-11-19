import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.extensions.base.search import BaseSearchCommands
from elo_bot.features.matches import controls

EPHEMERAL = ipy.MessageFlags.EPHEMERAL


class TestInteractionAcknowledgement(unittest.IsolatedAsyncioTestCase):
    """Real SDK contexts with only Discord's HTTP layer mocked, so response routing is the SDK's own."""

    def context(self, kind: type[ipy.ComponentContext] | type[ipy.ModalContext]):
        client = MagicMock()
        client.app.id = 42
        message = MagicMock(spec=ipy.Message, id=5, _channel_id=900, embeds=[ipy.Embed(title="Match")])
        client.cache.get_message.return_value = message
        client.cache.place_message_data.return_value = message
        for name in ("post_initial_response", "post_followup", "edit_interaction_message"):
            setattr(client.http, name, AsyncMock(return_value=None))
        client.http.get_interaction_message = AsyncMock(return_value={"id": "5", "channel_id": "900"})
        ctx = kind(client=client)
        ctx.token, ctx.id, ctx.channel_id, ctx.message_id = "token", 1, 900, 5
        return ctx, client.http

    def responses(self, http) -> dict[str, list[tuple[int | None, int | None]]]:
        def shape(call):
            payload = call.args[0]
            return payload.get("type"), payload.get("data", payload).get("flags")
        return {name: [shape(call) for call in getattr(http, name).await_args_list]
                for name in ("post_initial_response", "post_followup", "edit_interaction_message")}

    async def test_outdated_control_answers_privately_before_retiring_the_public_message(self) -> None:
        for kind in (ipy.ComponentContext, ipy.ModalContext):
            for deferred in (False, True):
                with self.subTest(context=kind.__name__, deferred=deferred):
                    ctx, http = self.context(kind)
                    if deferred:
                        await ctx.defer(ephemeral=True)
                    answered_before_edit = []

                    async def retire(_message, **kwargs) -> None:
                        answered_before_edit.append(ctx.responded)
                        self.assertEqual(kwargs["components"], [])

                    with patch.object(controls, "edit_channel_message", new=AsyncMock(side_effect=retire)):
                        await controls.reject_outdated_match_control(ctx)
                    self.assertEqual(answered_before_edit, [True])
                    expected = {
                        "post_initial_response": [(ipy.CallbackType.DEFERRED_CHANNEL_MESSAGE_WITH_SOURCE, EPHEMERAL)]
                        if deferred else [(ipy.CallbackType.CHANNEL_MESSAGE_WITH_SOURCE, EPHEMERAL)],
                        "post_followup": [],
                        "edit_interaction_message": [(None, EPHEMERAL)] if deferred else [],
                    }
                    self.assertEqual(self.responses(http), expected)

    async def test_expired_search_retires_button_controls_then_replies_in_a_private_followup(self) -> None:
        cog = BaseSearchCommands.__new__(BaseSearchCommands)
        cog._similarity = MagicMock(sessions={})
        for kind, page, expected in (
                (ipy.ComponentContext, None, {
                    "post_initial_response": [(ipy.CallbackType.UPDATE_MESSAGE, None)],
                    "post_followup": [(None, EPHEMERAL)], "edit_interaction_message": []}),
                (ipy.ModalContext, "2", {
                    "post_initial_response": [(ipy.CallbackType.CHANNEL_MESSAGE_WITH_SOURCE, EPHEMERAL)],
                    "post_followup": [], "edit_interaction_message": []}),
        ):
            with self.subTest(context=kind.__name__):
                ctx, http = self.context(kind)
                ctx.custom_id = "base_similar|expired|next"
                await cog._navigate_similar_page(ctx, page_number=page)
                self.assertEqual(self.responses(http), expected)
                ctx.message.edit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

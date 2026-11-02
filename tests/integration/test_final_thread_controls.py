from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from cogs.match_commands import confirm_match
from common.constants import STAFF_ROLE
from common.finals.lifecycle import confirm_bracket
from common.finals.matches import prepare_match
from database_models import Match


class TestFinalThreadControls(FinalScenarioCase):
    async def setup_transport(self, *, existing=False):
        final = await confirm_bracket(await self.roster(5), "manager")
        source = await prepare_match(final, next(key for key, node in final.nodes.items()
                                                if not node.feeders and not node.bye))
        message = SimpleNamespace(id=888, author=SimpleNamespace(id=999),
            content=f"<@1> <@2>\n<@&{STAFF_ROLE}>", pinned=True,
            embeds=[ipy.Embed()], nonce=f"match|{source.token}",
            components=[ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result",
                                                custom_id=f"submit_button|{source.token}"))], pin=AsyncMock())

        async def edit(**payload):
            for name, value in payload.items():
                setattr(message, name, value)

        message.edit = AsyncMock(side_effect=edit)
        thread = MagicMock(id=777, send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
        channel = MagicMock(create_private_thread=AsyncMock(return_value=thread))
        users = {int(user): MagicMock(mention=f"<@{user}>") for user in final.entrant_ids}
        for user, view in users.items():
            view.__str__.return_value = f"Player {user}"
        bot = MagicMock(fetch_channel=AsyncMock(return_value=thread if existing else channel),
                        fetch_user=AsyncMock(side_effect=lambda user: users[int(user)]))
        bot.user.id = 999
        if existing:
            await source.set({"thread": "777", "thread_message_id": 888})
        return source, bot, thread, message

    async def test_new_final_thread_exposes_both_forms_after_durable_binding(self):
        source, bot, thread, message = await self.setup_transport()
        self.assertEqual(await confirm_match(bot, source.token), "777")
        saved = await Match.get(source.id)
        self.assertEqual(saved.thread_message_id, 888)
        self.assertEqual(len(saved.final_districts), 7)
        buttons = thread.send.await_args.kwargs["components"][0].components
        self.assertEqual([button.label for button in buttons], ["Submit Bases", "Submit Result"])
        self.assertEqual(buttons[0].custom_id, f"submit_bases|{source.token}")
        self.assertEqual(len(saved.participants[0].bases), 7)
        self.assertFalse(any(saved.times))

    async def test_retry_repairs_missing_form_control_without_duplicate_thread_or_button(self):
        source, bot, thread, message = await self.setup_transport(existing=True)
        await confirm_match(bot, source.token)
        await confirm_match(bot, source.token)
        self.assertEqual([button.label for row in message.components for button in row.components],
                         ["Submit Bases", "Submit Result"])
        message.edit.assert_awaited_once()
        thread.send.assert_not_awaited()
        self.assertEqual((await Match.get(source.id)).thread_message_id, 888)
        self.assertEqual(await Match.count(), 1)

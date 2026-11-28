from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import init_beanie

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.matches.lifecycle import confirm_match
from elo_bot.config.ids import STAFF_ROLE
from elo_bot.discord.channel_edits import schedule_channel_name_edit
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.matches import prepare_match
from elo_bot.db import HelperTask, Match, ServerState
from elo_bot.extensions.match import MatchCommands


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
        users = {int(user): MagicMock(id=user, display_name=f"Player {user}", mention=f"<@{user}>") for user in final.entrant_ids}
        for user, view in users.items():
            view.__str__.return_value = f"user{user}"
            view.display_avatar.fetch = AsyncMock(return_value=b"")
        bot = MagicMock(fetch_channel=AsyncMock(return_value=thread if existing else channel),
                        fetch_user=AsyncMock(side_effect=lambda user: users[int(user)]))
        bot.user.id = 999
        bot.interactions_by_scope = {ipy.GLOBAL_SCOPE: {
            f"match base {name}": ipy.SlashCommand(name="match", group_name="base", sub_cmd_name=name,
                                                  cmd_id={ipy.GLOBAL_SCOPE: 123456789012345678})
            for name in ("submit", "scout")
        }}
        bot.mention_command.side_effect = lambda name: ipy.Client.mention_command(bot, name)
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
        self.assertEqual([button.label for button in buttons], ["Submit Result"])
        self.assertEqual(buttons[0].custom_id, f"submit_button|{source.token}")
        self.assertEqual(len(saved.participants[0].bases), 7)
        self.assertFalse(any(saved.times))
        name = bot.fetch_channel.return_value.create_private_thread.await_args.kwargs["name"]
        first, second = (participant.user_id for participant in saved.participants)
        self.assertEqual(name, f"🟡 {source.final_node_id} · user{first} vs. user{second}")
        embed = thread.send.await_args.kwargs["embed"]
        self.assertEqual(embed.title, f"**Season 7 Final · {source.final_node_id}**")
        self.assertEqual(embed.images, [])
        self.assertIsNone(thread.send.await_args.kwargs.get("file"))
        self.assertNotIn(" vs. ", embed.description)
        submit, scout = embed.description.splitlines()
        self.assertIn("</match base submit:123456789012345678>", submit)
        self.assertIn("**30 minutes**", submit)
        self.assertIn("</match base scout:123456789012345678>", scout)
        self.assertNotIn(f"<@&{STAFF_ROLE}>", embed.description)
        self.assertEqual(len(embed.fields[0].value.splitlines()), 7)
        self.assertIn("quarterfinals and semifinals", embed.fields[2].value)
        self.assertNotIn("**Notes**", [field.name for field in embed.fields])
        thread.name, thread.edit = name, AsyncMock()
        await schedule_channel_name_edit(thread, "🟢")
        self.assertEqual(thread.edit.await_args.kwargs["name"], "🟢 " + name.partition(" ")[2])

    async def test_retry_reuses_bound_thread_without_duplicate_post_or_button(self):
        source, bot, thread, message = await self.setup_transport(existing=True)
        await confirm_match(bot, source.token)
        await confirm_match(bot, source.token)
        self.assertEqual([button.label for row in message.components for button in row.components],
                         ["Submit Result"])
        message.edit.assert_not_awaited()
        thread.send.assert_not_awaited()
        self.assertEqual((await Match.get(source.id)).thread_message_id, 888)
        self.assertEqual(await Match.count(), 1)

    async def test_staff_ping_from_a_final_player_appears_in_the_hosting_queue_until_retired(self):
        await init_beanie(database=self.database, document_models=[HelperTask], skip_indexes=True)
        source, bot, _, _ = await self.setup_transport(existing=True)
        commands = MatchCommands(bot)
        author = source.participants[0].user_id
        channel = MagicMock(spec=ipy.ThreadChannel, id=int(source.thread), parent_id=source.routing_channel_id)
        message = MagicMock(channel=channel, content=f"<@&{STAFF_ROLE}>", author=SimpleNamespace(id=author))

        await commands.on_message_create.callback(commands, ipy.events.MessageCreate(message=message))

        task = await HelperTask.find_one({"thread": source.thread})
        self.assertIsNotNone(task)
        self.assertEqual(task.player_id, author)

        queue_message = MagicMock(embeds=[ipy.Embed(title="Hosting Queue")], edit=AsyncMock())
        queue = MagicMock(fetch_message=AsyncMock(return_value=queue_message))
        queue.guild.fetch_member = AsyncMock(return_value=None)
        bot.fetch_channel.side_effect = lambda channel_id: channel if str(channel_id) == source.thread else queue
        state = SimpleNamespace(hosting_queue_embed=SimpleNamespace(channel=300, message=301))
        with patch.object(ServerState, "current", new=AsyncMock(return_value=state)):
            await commands.update_hosting_queue_embed.callback.func(commands)
            self.assertIn(f"<#{source.thread}>", queue_message.embeds[0].description)
            await source.set({"final_superseded": True})
            await commands.update_hosting_queue_embed.callback.func(commands)

        self.assertEqual(queue_message.embeds[0].description, "")
        self.assertIsNone(await HelperTask.get(task.id))
        await commands.on_message_create.callback(commands, ipy.events.MessageCreate(message=message))
        self.assertEqual(await HelperTask.count(), 0)

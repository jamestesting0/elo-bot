import os
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from beanie import init_beanie
from pymongo import AsyncMongoClient

from elo_bot.db.timestamp_models import (
    TIMESTAMP_DRAFT_INDEXES, TIMESTAMP_POST_INDEXES, TimestampDraft, TimestampPost, TimestampPreference,
)
from elo_bot.discord.contexts import ComponentsV2ModalContext
from elo_bot.extensions.timestamp import TimestampCommands


NOW = datetime(2026, 11, 16, 20, tzinfo=UTC)
MESSAGE_PERMISSIONS = ipy.Permissions.SEND_MESSAGES | ipy.Permissions.SEND_MESSAGES_IN_THREADS


def timestamp(day: int, hour: int, minute: int = 0) -> str:
    return f"<t:{int(datetime(2026, 11, day, hour, minute, tzinfo=UTC).timestamp())}:F>"


class TimestampMongoCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = os.environ["INTEGRATION_MONGO_URI"]
        self.assertIn(urlsplit(uri).hostname, {"localhost", "127.0.0.1", "::1"},
                      "Timestamp integration tests require disposable loopback MongoDB.")
        self.mongo = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
        self.database = self.mongo[f"elo_timestamp_it_{uuid4().hex}"]
        self.addAsyncCleanup(self.mongo.close)
        self.addAsyncCleanup(self.mongo.drop_database, self.database.name)
        await init_beanie(database=self.database, document_models=[TimestampDraft, TimestampPost, TimestampPreference],
                          skip_indexes=True)
        await TimestampDraft.get_pymongo_collection().create_indexes(TIMESTAMP_DRAFT_INDEXES)
        await TimestampPost.get_pymongo_collection().create_indexes(TIMESTAMP_POST_INDEXES)
        self.cog = TimestampCommands(ipy.Client())
        self.sequence = int(ipy.Snowflake.from_datetime(NOW))
        self.channel = self.parent = MagicMock(spec=ipy.GuildText)
        self.parent.id = ipy.Snowflake(77)
        self.post_id = int(ipy.Snowflake.from_datetime(datetime.now(UTC)))
        self.member_permissions = self.app_permissions = MESSAGE_PERMISSIONS
        self.http = MagicMock(
            post_initial_response=AsyncMock(), edit_interaction_message=AsyncMock(return_value={"id": "9001"}),
            get_interaction_message=AsyncMock(return_value={"id": "9001"}), delete_interaction_message=AsyncMock(),
            execute_webhook=AsyncMock(side_effect=self.webhook_message), request=AsyncMock(),
        )
        webhook_client = MagicMock(http=self.http)
        webhook_client.cache.place_message_data.side_effect = lambda data: SimpleNamespace(id=ipy.Snowflake(data["id"]))
        self.webhook = ipy.Webhook(webhook_client, id=43, type=ipy.WebhookTypes.INCOMING, application_id="42",
                                   name="Elo Timestamp", token="owned-token")
        self.parent.fetch_webhooks.return_value = [self.webhook]

    def webhook_message(self, *_args, **_kwargs):
        self.post_id += 1
        return {"id": str(self.post_id)}

    def assert_public_timestamp(self, expected):
        self.http.execute_webhook.assert_awaited_once()
        self.assertEqual(self.http.execute_webhook.await_args.args[2]["content"], expected)

    def use_private_thread(self):
        self.channel = MagicMock(spec=ipy.GuildPrivateThread, id=ipy.Snowflake(77), parent_channel=self.parent, locked=False)

    def context(self, context_type, data, *, user=9):
        self.sequence += 1
        client = MagicMock(http=self.http)
        client.app.id = 42
        client.user.id = 42
        author = SimpleNamespace(id=user, display_name="James (Finals)",
                                 display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/avatars/9/example.png"))
        client.cache.get_user.return_value = author
        client.cache.get_member.return_value = author
        client.cache.get_channel.return_value = self.channel
        client.cache.place_message_data.return_value = MagicMock(id=9001, flags=ipy.MessageFlags.EPHEMERAL)
        kind = {ipy.SlashContext: 2, ipy.ComponentContext: 3, ipy.AutocompleteContext: 4, ComponentsV2ModalContext: 5}
        payload = {"id": str(self.sequence), "application_id": "42", "type": kind[context_type],
                   "token": "offline", "locale": "en-US", "guild_id": "11", "channel_id": "77", "data": data,
                   "app_permissions": str(int(self.app_permissions)),
                   "member": {"id": str(user), "user": {"id": str(user)},
                              "permissions": str(int(self.member_permissions))}}
        if kind[context_type] in {3, 5}:
            payload["message"] = {"id": "9001"}
        return context_type.from_dict(client, payload)

    async def slash(self, when, country=None):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "timestamp"})
        await self.cog.timestamp.callback.func(self.cog, ctx, when, country)
        return ctx

    def choose(self, menu, index, *, user=9):
        return self.context(ipy.ComponentContext, {"custom_id": menu["custom_id"], "component_type": 3,
                                                  "values": [menu["options"][index]["value"]]}, user=user)

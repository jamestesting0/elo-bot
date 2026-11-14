import asyncio
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.settings.document import DocumentSettings
from pydantic import ValidationError

from elobot.app import runtime
from elobot.features.sticky import commands as sticky_commands
from elobot.db.sticky_models import StickyMessage


def guild_text(channel_id="10"):
    channel = MagicMock()
    channel.__class__ = ipy.GuildText
    channel.id = channel_id
    channel.delete_message = AsyncMock()
    return channel


def message_event(channel, message_id):
    event = MagicMock()
    event.message.channel = channel
    event.message.id = message_id
    return event


def sticky_document(**overrides):
    fields = {
        "id": "10",
        "message_id": "11",
        "webhook_id": "50",
        "content": "hello",
        "author_name": "James",
        "avatar_url": "https://cdn/a.png",
    }
    fields.update(overrides)
    return StickyMessage(**fields)


class TestStickyMessageContract(unittest.TestCase):
    def test_fields_collection_and_rejected_values(self):
        collection = MagicMock()
        with patch.object(StickyMessage, "get_pymongo_collection", return_value=collection):
            stored = sticky_document()
            self.assertIsNone(sticky_document(id="11", avatar_url=None).avatar_url)
            self.assertEqual(stored.model_dump(by_alias=True)["_id"], "10")
            invalid = (
                {"id": "channel"},
                {"channel_id": "channel"},
                {"message_id": "message"},
                {"webhook_id": "webhook"},
                {"content": ""},
                {"content": "x" * 2001},
                {"author_name": ""},
                {"author_name": "x" * 81},
            )
            for overrides in invalid:
                with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                    sticky_document(**overrides)

        self.assertEqual(StickyMessage.Settings.name, "sticky_messages")
        self.assertTrue(StickyMessage.Settings.use_state_management)
        self.assertEqual(StickyMessage.Settings.indexes, ["channel_id"])


class TestStickyMessageMove(unittest.IsolatedAsyncioTestCase):
    async def test_move_updates_only_the_current_message(self):
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        with patch.object(StickyMessage, "get_pymongo_collection", return_value=collection):
            stored = sticky_document()
            moved = await stored.move("12", "13")

        self.assertTrue(moved)
        self.assertEqual(stored.message_id, "12")
        self.assertEqual(stored.webhook_id, "13")
        collection.update_one.assert_awaited_once_with(
            {"_id": "10", "message_id": "11"},
            {"$set": {"message_id": "12", "webhook_id": "13"}},
        )

    async def test_move_leaves_the_document_when_the_message_changed(self):
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
        with patch.object(StickyMessage, "get_pymongo_collection", return_value=collection):
            stored = sticky_document()
            moved = await stored.move("12", "13")

        self.assertFalse(moved)
        self.assertEqual(stored.message_id, "11")
        self.assertEqual(stored.webhook_id, "50")

    async def test_release_deletes_only_the_current_message(self):
        collection = MagicMock()
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        with patch.object(StickyMessage, "get_pymongo_collection", return_value=collection):
            stored = sticky_document()
            released = await stored.release()

        self.assertTrue(released)
        collection.delete_one.assert_awaited_once_with({"_id": "10", "message_id": "11"})

    async def test_release_leaves_the_document_when_the_message_changed(self):
        collection = MagicMock()
        collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=0))
        with patch.object(StickyMessage, "get_pymongo_collection", return_value=collection):
            stored = sticky_document()
            released = await stored.release()

        self.assertFalse(released)
        self.assertEqual(stored.message_id, "11")


class TestStickyCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modules = (sticky_commands,)

    def context(self, channel):
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel = channel
        ctx.author = MagicMock()
        ctx.author.display_name = "James"
        ctx.author.display_avatar = MagicMock(url="https://cdn/a.png")
        ctx.author.has_role.return_value = True
        return ctx

    async def test_requires_staff(self):
        for module in self.modules:
            ctx = self.context(guild_text())
            ctx.author.has_role.return_value = False
            self.assertFalse(await module.sticky.checks[0](ctx))
            ctx.author.has_role.return_value = True
            self.assertTrue(await module.sticky.checks[0](ctx))

    async def test_blank_message_is_rejected_before_any_webhook_or_write(self):
        for module in self.modules:
            ctx = self.context(guild_text())
            with patch.object(module, "sticky_webhook", new_callable=AsyncMock) as webhook_for:
                await module.sticky.callback(ctx, "   ")
            webhook_for.assert_not_awaited()
            ctx.defer.assert_not_awaited()
            ctx.send.assert_awaited_once_with(
                f"{module.ERROR_EMOJI} The sticky message cannot be empty.", ephemeral=True,
            )

    async def test_stores_webhook_message_with_author_name_and_avatar(self):
        for module in self.modules:
            channel = guild_text()
            ctx = self.context(channel)
            ctx.author.display_name = "A" * 90
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.insert_one = AsyncMock()
            with (
                patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[]),
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                await module.sticky.callback(ctx, "  hello  ")

            webhook.send.assert_awaited_once_with(
                "hello", username="A" * 80, avatar_url="https://cdn/a.png", wait=True,
            )
            collection.insert_one.assert_awaited_once_with(
                {
                    "_id": "60",
                    "channel_id": "10",
                    "message_id": "60",
                    "webhook_id": "50",
                    "content": "hello",
                    "author_name": "A" * 80,
                    "avatar_url": "https://cdn/a.png",
                },
            )
            channel.delete_message.assert_not_awaited()
            sent.delete.assert_not_awaited()
            ctx.send.assert_awaited_once_with(f"{module.SUCCESS_EMOJI} Sticky message set.", ephemeral=True)

    async def test_missing_avatar_is_stored_as_null(self):
        for module in self.modules:
            ctx = self.context(guild_text())
            ctx.author.display_avatar = None
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.insert_one = AsyncMock()
            with (
                patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[]),
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                await module.sticky.callback(ctx, "hello")

            self.assertIsNone(webhook.send.await_args.kwargs["avatar_url"])
            self.assertIsNone(collection.insert_one.await_args.args[0]["avatar_url"])

    async def test_failed_save_deletes_the_webhook_message_and_does_not_confirm(self):
        for module in self.modules:
            ctx = self.context(guild_text())
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.insert_one = AsyncMock(side_effect=RuntimeError("db down"))
            with (
                patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[]),
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
                self.assertRaisesRegex(RuntimeError, "db down"),
            ):
                await module.sticky.callback(ctx, "hello")

            sent.delete.assert_awaited_once()
            ctx.send.assert_not_awaited()

    async def test_failed_webhook_send_does_not_write(self):
        for module in self.modules:
            ctx = self.context(guild_text())
            webhook = MagicMock(send=AsyncMock(side_effect=RuntimeError("discord down")))
            collection = MagicMock()
            collection.insert_one = AsyncMock()
            with (
                patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[]),
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
                self.assertRaisesRegex(RuntimeError, "discord down"),
            ):
                await module.sticky.callback(ctx, "hello")

            collection.insert_one.assert_not_awaited()
            ctx.send.assert_not_awaited()

    async def test_adding_keeps_the_previous_message_and_confirms_after_the_write(self):
        for module in self.modules:
            channel = guild_text()
            ctx = self.context(channel)
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            order = []

            async def update_one(*args, **kwargs):
                order.append("save")

            async def delete_message(message_id):
                order.append(("delete", str(message_id)))
                self.assertEqual(order[0], "save")

            collection.insert_one = AsyncMock(side_effect=update_one)
            channel.delete_message = AsyncMock(side_effect=delete_message)
            webhook_for = AsyncMock(return_value=(webhook, 77))
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", webhook_for),
            ):
                existing = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[existing]):
                    await module.sticky.callback(ctx, "hello")

            self.assertEqual(order, ["save"])
            channel.delete_message.assert_not_awaited()
            self.assertEqual(webhook.send.await_args.kwargs["thread"], 77)
            webhook_for.assert_awaited_once_with(channel, "50")
            ctx.send.assert_awaited_once_with(f"{module.SUCCESS_EMOJI} Sticky message set.", ephemeral=True)


def delete_event(channel_id, message_id):
    event = MagicMock()
    event.message.id = message_id
    event.message._channel_id = channel_id
    return event


def bulk_delete_event(channel_id, message_ids):
    event = MagicMock()
    event.channel_id = channel_id
    event.ids = message_ids
    return event


class TestStickyListener(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.modules = (sticky_commands,)
        for module in self.modules:
            module._bot_sticky_deletes.clear()

    async def test_new_message_deletes_and_resends_the_sticky(self):
        for module in self.modules:
            channel = guild_text()
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
            order = []
            channel.delete_message = AsyncMock(side_effect=lambda message_id: order.append(("delete", str(message_id))))
            webhook.send.side_effect = lambda *args, **kwargs: order.append("send") or sent
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, 77)),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            self.assertEqual(order, [("delete", "11"), "send"])
            webhook.send.assert_awaited_once_with(
                "hello", username="James", avatar_url="https://cdn/a.png", wait=True, thread=77,
            )
            collection.update_one.assert_awaited_once_with(
                {"_id": "10", "message_id": "11"},
                {"$set": {"message_id": "60", "webhook_id": "50"}},
            )
            sent.delete.assert_not_awaited()

    async def test_the_sticky_message_itself_is_left_alone(self):
        for module in self.modules:
            channel = guild_text()
            webhook = MagicMock(send=AsyncMock())
            collection = MagicMock()
            collection.update_one = AsyncMock()
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "11"))

            channel.delete_message.assert_not_awaited()
            webhook.send.assert_not_awaited()

    async def test_channel_without_a_sticky_is_ignored(self):
        for module in self.modules:
            channel = guild_text()
            other = MagicMock()
            other.id = "10"
            with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock) as get:
                await module.on_sticky_message.callback(message_event(other, "20"))
                get.return_value = []
                await module.on_sticky_message.callback(message_event(channel, "20"))

            get.assert_awaited_once_with("10")
            channel.delete_message.assert_not_awaited()

    async def test_missing_sticky_message_is_removed_and_not_resent(self):
        for module in self.modules:
            channel = guild_text()
            channel.delete_message = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404, reason="Missing")))
            webhook = MagicMock(id="50", send=AsyncMock())
            collection = MagicMock()
            collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
            collection.update_one = AsyncMock()
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            webhook.send.assert_not_awaited()
            collection.update_one.assert_not_awaited()
            collection.delete_one.assert_awaited_once_with({"_id": "10", "message_id": "11"})

    async def test_human_delete_removes_the_sticky_and_later_messages_do_not_restick_it(self):
        for module in self.modules:
            collection = MagicMock()
            channel = guild_text()
            webhook = MagicMock(id="50", send=AsyncMock())
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                state = {"stored": sticky_document()}

                async def get(channel_id):
                    return [state["stored"]] if state["stored"] is not None else []

                async def delete_one(query):
                    self.assertEqual(query, {"_id": "10", "message_id": "11"})
                    state["stored"] = None
                    return MagicMock(deleted_count=1)

                collection.delete_one = AsyncMock(side_effect=delete_one)
                with patch.object(module.StickyMessage, "for_channel", side_effect=get):
                    await module.on_sticky_deleted.callback(delete_event("10", "11"))
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            collection.delete_one.assert_awaited_once()
            channel.delete_message.assert_not_awaited()
            webhook.send.assert_not_awaited()

    async def test_bot_restick_delete_does_not_remove_the_sticky(self):
        for module in self.modules:
            channel = guild_text()
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
            collection.delete_one = AsyncMock()

            async def delete_message(message_id):
                await module.on_sticky_deleted.callback(delete_event("10", str(message_id)))

            channel.delete_message = AsyncMock(side_effect=delete_message)
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            webhook.send.assert_awaited_once()
            collection.delete_one.assert_not_awaited()
            self.assertEqual(module._bot_sticky_deletes, set())

    async def test_delete_of_another_message_keeps_the_sticky(self):
        for module in self.modules:
            collection = MagicMock()
            collection.delete_one = AsyncMock()
            with patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_deleted.callback(delete_event("10", "99"))
                    await module.on_sticky_deleted.callback(delete_event(ipy.MISSING, "11"))

            collection.delete_one.assert_not_awaited()

    async def test_human_bulk_delete_removes_the_sticky(self):
        for module in self.modules:
            collection = MagicMock()
            collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
            with patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_bulk_deleted.callback(bulk_delete_event("10", ["8", "11"]))

            collection.delete_one.assert_awaited_once_with({"_id": "10", "message_id": "11"})

    async def test_failed_bot_delete_still_allows_a_human_to_unstick(self):
        for module in self.modules:
            channel = guild_text()
            channel.delete_message = AsyncMock(
                side_effect=ipy.errors.HTTPException(MagicMock(status=403, reason="Forbidden")),
            )
            webhook = MagicMock(id="50", send=AsyncMock())
            collection = MagicMock()
            collection.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
            collection.update_one = AsyncMock()
            with patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection):
                stored = sticky_document()
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
                patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]),
            ):
                with self.assertRaises(ipy.errors.HTTPException):
                    await module.on_sticky_message.callback(message_event(channel, "20"))
                self.assertEqual(module._bot_sticky_deletes, set())
                await module.on_sticky_deleted.callback(delete_event("10", "11"))

            webhook.send.assert_not_awaited()
            collection.update_one.assert_not_awaited()
            collection.delete_one.assert_awaited_once_with({"_id": "10", "message_id": "11"})

    async def test_lost_update_deletes_the_resent_message(self):
        for module in self.modules:
            channel = guild_text()
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.update_one = AsyncMock(return_value=MagicMock(matched_count=0))
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            sent.delete.assert_awaited_once()

    async def test_failed_move_deletes_the_resent_message(self):
        for module in self.modules:
            channel = guild_text()
            sent = MagicMock(id="60", delete=AsyncMock())
            webhook = MagicMock(id="50", send=AsyncMock(return_value=sent))
            collection = MagicMock()
            collection.update_one = AsyncMock(side_effect=RuntimeError("db down"))
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
                self.assertRaisesRegex(RuntimeError, "db down"),
            ):
                stored = sticky_document()
                with patch.object(module.StickyMessage, "for_channel", new_callable=AsyncMock, return_value=[stored]):
                    await module.on_sticky_message.callback(message_event(channel, "20"))

            sent.delete.assert_awaited_once()

    async def test_concurrent_messages_leave_the_later_repost(self):
        for module in self.modules:
            channel = guild_text("10")
            state = {"message_id": "11", "next_id": 100}
            sent_messages = []
            deleted = []

            async def delete_message(message_id):
                deleted.append(str(message_id))

            async def send(*args, **kwargs):
                state["next_id"] += 1
                sent = MagicMock(id=str(state["next_id"]), delete=AsyncMock())
                sent_messages.append(sent)
                return sent

            async def update_one(query, update):
                if query["message_id"] != state["message_id"]:
                    return MagicMock(matched_count=0)
                state["message_id"] = update["$set"]["message_id"]
                return MagicMock(matched_count=1)

            async def get(channel_id):
                return [sticky_document(id=channel_id, message_id=state["message_id"])]

            channel.delete_message = AsyncMock(side_effect=delete_message)
            webhook = MagicMock(id="50", send=AsyncMock(side_effect=send))
            collection = MagicMock()
            collection.update_one = AsyncMock(side_effect=update_one)
            with (
                patch.object(module.StickyMessage, "get_pymongo_collection", return_value=collection),
                patch.object(module.StickyMessage, "for_channel", side_effect=get),
                patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
            ):
                await asyncio.wait_for(asyncio.gather(
                    module.on_sticky_message.callback(message_event(channel, "20")),
                    module.on_sticky_message.callback(message_event(channel, "21")),
                ), timeout=5)

            self.assertEqual(state["message_id"], "102")
            self.assertEqual(deleted, ["11", "101"])
            for sent in sent_messages:
                sent.delete.assert_not_awaited()


class TestMultipleStickies(unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def runtime(self, module, legacy=False):
        rows = {}
        visible = {}
        channel = guild_text()
        ctx = TestStickyCommand().context(channel)
        next_id = 100

        def matches(row, query):
            return all(
                any(matches(row, clause) for clause in value) if key == "$or" else row.get(key) == value
                for key, value in query.items()
            )

        def find(*, filter, **kwargs):
            return MagicMock(to_list=AsyncMock(return_value=[
                dict(row) for row in rows.values() if matches(row, filter)
            ]))

        async def insert_one(row):
            self.assertNotIn(row["_id"], rows)
            rows[row["_id"]] = dict(row)
            return MagicMock(inserted_id=row["_id"])

        async def update_one(query, update):
            row = next((row for row in rows.values() if matches(row, query)), None)
            if row is None:
                return MagicMock(matched_count=0)
            row.update(update["$set"])
            return MagicMock(matched_count=1)

        async def delete_one(query):
            row = next((row for row in rows.values() if matches(row, query)), None)
            if row is None:
                return MagicMock(deleted_count=0)
            del rows[row["_id"]]
            return MagicMock(deleted_count=1)

        async def delete_message(message_id):
            if visible.pop(str(message_id), None) is None:
                raise ipy.errors.NotFound(MagicMock(status=404, reason="Missing"))

        async def send(content, **kwargs):
            nonlocal next_id
            next_id += 1
            message_id = str(next_id)
            visible[message_id] = content
            return MagicMock(id=message_id, delete=AsyncMock(side_effect=lambda: visible.pop(message_id)))

        collection = MagicMock(
            find=MagicMock(side_effect=find),
            insert_one=AsyncMock(side_effect=insert_one), update_one=AsyncMock(side_effect=update_one),
            delete_one=AsyncMock(side_effect=delete_one),
        )
        channel.delete_message = AsyncMock(side_effect=delete_message)
        webhook = MagicMock(id="50", send=AsyncMock(side_effect=send))
        module._sticky_locks.clear()
        module._bot_sticky_deletes.clear()
        with (
            patch.object(StickyMessage, "get_pymongo_collection", return_value=collection),
            patch.object(StickyMessage, "get_settings", return_value=DocumentSettings(use_state_management=True)),
            patch.object(module, "sticky_webhook", new_callable=AsyncMock, return_value=(webhook, None)),
        ):
            if legacy:
                rows["10"] = sticky_document().model_dump(by_alias=True)
                rows["10"].pop("channel_id", None)
                visible["11"] = "hello"
            yield SimpleNamespace(
                rows=rows, visible=visible, channel=channel, ctx=ctx, webhook=webhook, collection=collection,
            )

    async def test_two_commands_keep_both_messages_and_records(self):
        for module in (sticky_commands,):
            with self.runtime(module) as state:
                await module.sticky.callback(state.ctx, "first")
                await module.sticky.callback(state.ctx, "second")

                self.assertEqual(list(state.visible.values()), ["first", "second"])
                self.assertEqual([row["content"] for row in state.rows.values()], ["first", "second"])
                self.assertEqual({row["channel_id"] for row in state.rows.values()}, {"10"})
                state.channel.delete_message.assert_not_awaited()

    async def test_reposts_legacy_and_new_stickies_in_order(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                state.webhook.send.reset_mock()

                await module.on_sticky_message.callback(message_event(state.channel, "200"))

                self.assertEqual(list(state.visible.values()), ["hello", "second"])
                self.assertEqual([call.args[0] for call in state.webhook.send.await_args_list], ["hello", "second"])
                self.assertEqual({row["message_id"] for row in state.rows.values()}, set(state.visible))
                self.assertEqual(set(state.rows), {"10", "101"})

    async def test_deleting_one_sticky_keeps_the_other_across_a_repost(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                state.visible.pop("11")

                await module.on_sticky_deleted.callback(delete_event("10", "11"))
                await module.on_sticky_message.callback(message_event(state.channel, "200"))

                self.assertEqual(list(state.visible.values()), ["second"])
                self.assertEqual(set(state.rows), {"101"})
                self.assertEqual(state.rows["101"]["message_id"], next(iter(state.visible)))

    async def test_failed_add_preserves_existing_sticky(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                state.collection.insert_one.side_effect = RuntimeError("db down")
                state.collection.update_one.side_effect = RuntimeError("db down")

                with self.assertRaisesRegex(RuntimeError, "db down"):
                    await module.sticky.callback(state.ctx, "second")

                self.assertEqual(state.visible, {"11": "hello"})
                self.assertEqual(set(state.rows), {"10"})
                state.ctx.send.assert_not_awaited()

    async def test_failed_confirmation_keeps_both_committed_stickies(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                state.ctx.send.side_effect = ipy.errors.HTTPException(MagicMock(status=500, reason="Unavailable"))

                with self.assertRaises(ipy.errors.HTTPException):
                    await module.sticky.callback(state.ctx, "second")

                self.assertEqual(list(state.visible.values()), ["hello", "second"])
                self.assertEqual(set(state.rows), {"10", "101"})
                self.assertEqual({row["message_id"] for row in state.rows.values()}, set(state.visible))

    async def test_bulk_deleting_selected_stickies_keeps_the_remaining_message(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                await module.sticky.callback(state.ctx, "third")
                state.visible.pop("11")
                state.visible.pop("101")

                await module.on_sticky_bulk_deleted.callback(bulk_delete_event("10", ["11", "101", "999"]))

                self.assertEqual(state.visible, {"102": "third"})
                self.assertEqual(set(state.rows), {"102"})

    async def test_missing_first_sticky_does_not_prevent_reposting_the_second(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                state.visible.pop("11")

                await module.on_sticky_message.callback(message_event(state.channel, "200"))

                self.assertEqual(state.visible, {"102": "second"})
                self.assertEqual(set(state.rows), {"101"})
                self.assertEqual(state.rows["101"]["message_id"], "102")

    async def test_current_and_delayed_sticky_events_do_not_trigger_another_repost(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                await module.on_sticky_message.callback(message_event(state.channel, "200"))
                state.webhook.send.reset_mock()
                state.channel.delete_message.reset_mock()

                for message_id in ("11", "101", "102", "103"):
                    event = message_event(state.channel, message_id)
                    event.message.webhook_id = "50"
                    await module.on_sticky_message.callback(event)
                await module.on_sticky_deleted.callback(delete_event("10", "11"))

                state.webhook.send.assert_not_awaited()
                state.channel.delete_message.assert_not_awaited()
                self.assertEqual(set(state.rows), {"10", "101"})
                self.assertEqual(state.visible, {"102": "hello", "103": "second"})

    async def test_other_webhook_messages_still_repost_all_stickies(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                event = message_event(state.channel, "200")
                event.message.webhook_id = "99"

                await module.on_sticky_message.callback(event)

                self.assertEqual(state.visible, {"102": "hello", "103": "second"})
                self.assertEqual({row["message_id"] for row in state.rows.values()}, set(state.visible))

    async def test_channel_lookup_keeps_channels_separate_and_sorts_original_message_ids(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                state.rows["100"] = sticky_document(id="100", channel_id="10").model_dump(by_alias=True)
                state.rows["99"] = sticky_document(id="99", channel_id="10").model_dump(by_alias=True)
                state.rows["20"] = sticky_document(id="20").model_dump(by_alias=True)
                state.rows["150"] = sticky_document(id="150", channel_id="20").model_dump(by_alias=True)

                messages = await StickyMessage.for_channel("10")

                self.assertEqual([message.id for message in messages], ["10", "99", "100"])

    async def test_overlapping_messages_repost_each_sticky_without_losing_records(self):
        for module in (sticky_commands,):
            with self.runtime(module, legacy=True) as state:
                await module.sticky.callback(state.ctx, "second")
                both_queried = asyncio.Event()
                read = StickyMessage.for_channel
                reads = 0

                async def for_channel(channel_id):
                    nonlocal reads
                    result = await read(channel_id)
                    reads += 1
                    if reads == 2:
                        both_queried.set()
                    return result

                with patch.object(StickyMessage, "for_channel", side_effect=for_channel):
                    async with module.sticky_channel_lock("10"):
                        tasks = [asyncio.create_task(module.on_sticky_message.callback(message_event(state.channel, id)))
                                 for id in ("200", "201")]
                        await asyncio.wait_for(both_queried.wait(), timeout=1)
                    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)

                self.assertEqual(state.visible, {"104": "hello", "105": "second"})
                self.assertEqual(set(state.rows), {"10", "101"})
                self.assertEqual({row["message_id"] for row in state.rows.values()}, set(state.visible))
                self.assertEqual(module._bot_sticky_deletes, set())


class TestStickyWebhook(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_the_stored_webhook_then_the_named_webhook_then_creates_one(self):
        for module in (sticky_commands,):
            stored = MagicMock()
            stored.id = "5"
            stored.token = "stored"
            stored.name = "Other"
            named = MagicMock()
            named.id = "6"
            named.token = "named"
            named.name = module.STICKY_WEBHOOK_NAME
            blank = MagicMock()
            blank.id = "5"
            blank.token = ""
            blank.name = module.STICKY_WEBHOOK_NAME
            channel = guild_text()
            channel.fetch_webhooks = AsyncMock(return_value=[blank, named, stored])
            channel.create_webhook = AsyncMock()

            webhook, thread_id = await module.sticky_webhook(channel, "5")
            self.assertIs(webhook, stored)
            self.assertIsNone(thread_id)

            webhook, thread_id = await module.sticky_webhook(channel, "999")
            self.assertIs(webhook, named)
            self.assertIsNone(thread_id)
            channel.create_webhook.assert_not_awaited()

            channel.fetch_webhooks = AsyncMock(return_value=[])
            created = MagicMock(id="7", token="created", name=module.STICKY_WEBHOOK_NAME)
            channel.create_webhook = AsyncMock(return_value=created)
            webhook, thread_id = await module.sticky_webhook(channel, None)
            self.assertIs(webhook, created)
            channel.create_webhook.assert_awaited_once_with(module.STICKY_WEBHOOK_NAME)

    async def test_thread_uses_the_parent_webhook_and_thread_id(self):
        for module in (sticky_commands,):
            parent = MagicMock()
            created = MagicMock(id="7", token="created", name=module.STICKY_WEBHOOK_NAME)
            parent.fetch_webhooks = AsyncMock(return_value=[])
            parent.create_webhook = AsyncMock(return_value=created)
            channel = MagicMock()
            channel.__class__ = ipy.ThreadChannel
            channel.id = "9"
            channel.parent_id = "8"
            channel.parent_channel = None
            fetch = AsyncMock(return_value=parent, name="fetch_channel")
            with patch.object(runtime, "bot", MagicMock(fetch_channel=fetch), create=True):
                webhook, thread_id = await module.sticky_webhook(channel, None)

            fetch.assert_awaited_once_with("8")
            self.assertIs(webhook, created)
            self.assertEqual(thread_id, "9")
            parent.create_webhook.assert_awaited_once_with(module.STICKY_WEBHOOK_NAME)


if __name__ == "__main__":
    unittest.main()

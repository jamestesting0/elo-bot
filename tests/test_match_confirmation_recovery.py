import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import PyMongoError

from elobot.extensions import match as match_commands
from elobot.config.ids import MATCH_CHANNEL, STAFF_ROLE
from elobot.db import BaseLink, BaseStore, Match, Participant
from tests._patch_sites import patch_sites
import elobot.features.matches.lifecycle as lifecycle_module
import elobot.extensions.match as match_module


class TestMatchConfirmationRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        districts = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
        self.persisted = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1,
            participants=[Participant(user_id=str(i), confirmed=True, districts=districts) for i in (111, 222)],
        )
        self.persisted._save_state()
        self.writes = []

        async def persist(document, updates, **_kwargs):
            self.writes.append(copy.deepcopy(updates))
            self.persisted = document.model_copy(deep=True)
            self.persisted._save_state()
            return document

        setter = patch.object(Match, "set", new=persist)
        setter.start()
        self.addCleanup(setter.stop)
        self.binding_queries = []

        async def bind(query, update):
            self.binding_queries.append(copy.deepcopy(query))
            expected = {"_id": self.persisted.id, "thread": self.persisted.thread,
                        "thread_message_id": self.persisted.thread_message_id}
            if query != expected:
                return SimpleNamespace(matched_count=0)
            self.persisted.thread_message_id = update["$set"]["thread_message_id"]
            self.persisted._save_state()
            return SimpleNamespace(matched_count=1)

        collection = SimpleNamespace(update_one=AsyncMock(side_effect=bind))
        self.collection = collection
        self.bind = bind
        mongo = patch.object(Match, "get_pymongo_collection", return_value=collection)
        mongo.start()
        self.addCleanup(mongo.stop)
        model = patch_sites(('elobot.extensions.match.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
        self.model = model.start()
        self.addCleanup(model.stop)
        self.model.find_one = AsyncMock(side_effect=lambda *_args: self.persisted.model_copy(deep=True))
        players = patch_sites(('elobot.extensions.match.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))
        self.players = players.start()
        self.addCleanup(players.stop)
        self.players.find_one = AsyncMock(return_value=SimpleNamespace(base_presets={}))
        self.messages = []
        self.thread = MagicMock(id=123)
        self.thread.fetch_message = AsyncMock(side_effect=lambda message_id: next(
            (m for m in self.messages if getattr(m, "id", None) == message_id), None,
        ))
        self.thread.fetch_pinned_messages = AsyncMock(side_effect=lambda: [m for m in self.messages if m.pinned])

        async def history(*, limit):
            self.assertEqual(limit, 0)
            for message in reversed(self.messages):
                yield message

        self.thread.history = history
        self.thread.send = AsyncMock(side_effect=self.send_message)
        self.channel = MagicMock(create_private_thread=AsyncMock(return_value=self.thread))
        self.bot = MagicMock()
        self.bot.user.id = 999
        self.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: (
            self.channel if int(channel_id) == MATCH_CHANNEL else self.thread
        ))
        self.bot.fetch_user = AsyncMock(side_effect=lambda user_id: SimpleNamespace(
            id=user_id, mention=f"<@{user_id}>", display_name=f"Player {user_id}",
        ))

    async def send_message(self, content, *, embed, components, **_kwargs):
        message = SimpleNamespace(
            id=456, author=SimpleNamespace(id=999), content=content, embeds=[embed], components=components,
            pinned=False, edit=AsyncMock(), pin=AsyncMock(),
        )

        async def edit(**payload):
            message.content = payload.get("content", message.content)

        async def pin():
            message.pinned = True

        message.edit.side_effect = edit
        message.pin.side_effect = pin
        self.messages.append(message)
        return message

    @staticmethod
    def unavailable() -> ipy.errors.DiscordError:
        return ipy.errors.DiscordError(response=SimpleNamespace(status=503, reason="Service Unavailable"))

    async def test_confirmation_only_offers_base_submission_when_enabled(self) -> None:
        for enabled in (False, True):
            with self.subTest(base_submission=enabled):
                self.persisted.base_submission = enabled
                self.persisted.thread = None
                self.persisted.thread_message_id = None
                self.persisted._save_state()
                self.messages.clear()

                await match_commands.confirm_match(self.bot, "TOKEN")

                description = self.messages[0].embeds[0].description
                self.assertEqual("</match base submit:1288780187042971700>" in description, enabled)

    async def test_confirmation_resumes_existing_thread_after_failed_send(self) -> None:
        # Given: Discord fails the announcement after the thread identity is saved.
        self.thread.send.side_effect = self.unavailable()
        with self.assertRaises(ipy.errors.DiscordError):
            await match_commands.confirm_match(self.bot, "TOKEN")
        self.assertEqual(self.persisted.thread, "123")
        districts = list(self.persisted.final_districts)
        base = BaseLink(link="submitted-after-failure", image="image")
        store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="keep")
        self.persisted.participants[0].bases[0] = base
        self.persisted.participants[1].base_store = store
        self.persisted._save_state()
        self.thread.send.side_effect = self.send_message

        # When: confirmation is retried from the persisted match.
        thread_id = await match_commands.confirm_match(self.bot, "TOKEN")

        # Then: recovery publishes in the same thread without rerolling or overwriting bases/storage.
        self.assertEqual(thread_id, "123")
        self.channel.create_private_thread.assert_awaited_once()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.persisted.thread_message_id, 456)
        self.assertTrue(self.messages[0].pinned)
        self.assertEqual(self.persisted.final_districts, districts)
        self.assertEqual(self.persisted.participants[0].bases[0], base)
        self.assertEqual(self.persisted.participants[1].base_store, store)
        self.assertEqual(self.players.find_one.await_count, 2)
        self.assertEqual(self.thread.send.await_args.kwargs["nonce"], "match|TOKEN")
        self.assertTrue(self.thread.send.await_args.kwargs["enforce_nonce"])

    async def test_confirmation_repins_after_send_committed_but_response_was_lost(self) -> None:
        # Given: Discord creates the announcement but returns a 503 to the client.
        async def lost_response(*args, **kwargs):
            await self.send_message(*args, **kwargs)
            raise self.unavailable()

        self.thread.send.side_effect = lost_response
        with self.assertRaises(ipy.errors.DiscordError):
            await match_commands.confirm_match(self.bot, "TOKEN")

        # When: confirmation is retried.
        await match_commands.confirm_match(self.bot, "TOKEN")

        # Then: the existing message is finalized and pinned instead of duplicated.
        self.thread.send.assert_awaited_once()
        self.channel.create_private_thread.assert_awaited_once()
        self.assertTrue(self.messages[0].pinned)
        self.assertIn(f"<@&{STAFF_ROLE}>", self.messages[0].content)

    async def test_confirmation_recovers_failed_pin_without_resending(self) -> None:
        # Given: the announcement sends and edits successfully, but pinning fails.
        async def failed_pin(*args, **kwargs):
            message = await self.send_message(*args, **kwargs)
            message.pin.side_effect = [self.unavailable(), None]
            return message

        self.thread.send.side_effect = failed_pin
        with self.assertRaises(ipy.errors.DiscordError):
            await match_commands.confirm_match(self.bot, "TOKEN")

        # When: confirmation is retried.
        await match_commands.confirm_match(self.bot, "TOKEN")

        # Then: only the failed pin is repeated.
        self.thread.send.assert_awaited_once()
        self.assertEqual(self.messages[0].pin.await_count, 2)
        self.messages[0].edit.assert_awaited_once()

    async def test_confirmation_does_not_duplicate_a_ready_announcement(self) -> None:
        # Given: the first confirmation completed normally.
        await match_commands.confirm_match(self.bot, "TOKEN")

        # When: the periodic recovery revisits the match.
        await match_commands.confirm_match(self.bot, "TOKEN")

        # Then: no additional thread, message, edit, or pin is produced.
        self.channel.create_private_thread.assert_awaited_once()
        self.thread.send.assert_awaited_once()
        self.messages[0].edit.assert_awaited_once()
        self.messages[0].pin.assert_awaited_once()

    async def test_recovery_ignores_an_unrelated_pinned_message(self) -> None:
        # Given: the thread exists but its only pin belongs to someone else.
        self.persisted.thread = "123"
        self.persisted.final_districts = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
        self.persisted._save_state()
        other = SimpleNamespace(author=SimpleNamespace(id=111), pinned=True, components=[], edit=AsyncMock())
        self.messages.append(other)

        # When: incomplete setup is recovered.
        await match_commands.confirm_match(self.bot, "TOKEN")

        # Then: a match-owned announcement is created and the unrelated pin is preserved.
        self.assertEqual(len(self.messages), 2)
        other.edit.assert_not_awaited()
        self.assertTrue(self.messages[1].pinned)
        self.channel.create_private_thread.assert_not_awaited()

    async def test_automatic_recovery_includes_existing_threads_and_continues_after_failure(self) -> None:
        # Given: two confirmed matches have existing threads, and one remains unavailable.
        first, second = [self.persisted.model_copy(update={"token": token, "thread": "123"})
                         for token in ("FIRST", "SECOND")]
        self.model.find.return_value.to_list = AsyncMock(return_value=[first, second])
        cog = match_module.MatchCommands(self.bot)
        with patch_sites(('elobot.extensions.match.confirm_match', 'elobot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock) as confirm:
            confirm.side_effect = [self.unavailable(), "123"]
            # When: the scheduled recovery runs.
            with self.assertLogs(level="ERROR"):
                await cog.auto_match_confirm.callback.func(cog)

        # Then: both matches are attempted, and the database query includes incomplete existing threads.
        self.assertEqual([call.args[1] for call in confirm.await_args_list], ["FIRST", "SECOND"])
        query = self.model.find.call_args.args[0]
        self.assertEqual(query["$or"][0], {"thread": None, "is_challenge": False})
        self.assertEqual(query["$or"][1]["times"], [None, None])
        self.assertNotIn("is_challenge", query["$or"][1])

    async def test_missing_cached_thread_preserves_reservation_and_recovers_next_match(self) -> None:
        first = self.persisted.model_copy(deep=True)
        first.token, first.thread = "FIRST", "123"
        first.participants[0].base_store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="keep")
        second = self.persisted.model_copy(deep=True)
        second.token, second.thread = "SECOND", "456"
        second._save_state()
        self.persisted = second
        self.model.find.return_value.to_list = AsyncMock(return_value=[first, second])
        self.model.find_one.side_effect = [first.model_copy(deep=True), second.model_copy(deep=True)]
        self.bot.fetch_channel.side_effect = [None, self.thread]
        cog = match_module.MatchCommands(self.bot)

        with self.assertLogs(level="ERROR"):
            await cog.auto_match_confirm.callback.func(cog)

        self.assertEqual(first.participants[0].base_store.reservation_id, "keep")
        self.assertEqual(first.thread, "123")
        self.assertEqual(self.persisted.thread_message_id, 456)
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(self.messages[0].pinned)
        self.channel.create_private_thread.assert_not_awaited()

    async def test_failed_confirmation_save_does_not_stop_next_match(self) -> None:
        first = self.persisted.model_copy(deep=True)
        first.token, first.thread, first.matched_time = "FIRST", None, 1
        first.participants[1].confirmed = False
        second = self.persisted.model_copy(deep=True)
        second.token, second.thread = "SECOND", "456"
        self.model.find.return_value.to_list = AsyncMock(return_value=[first, second])
        self.model.find_one.side_effect = None
        self.model.find_one.return_value = first.model_copy(deep=True)
        cog = match_module.MatchCommands(self.bot)
        with patch.object(Match, "save_changes", new_callable=AsyncMock, side_effect=PyMongoError("write failed")):
            with patch_sites(('elobot.extensions.match.confirm_match', 'elobot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock) as confirm:
                with self.assertLogs(level="ERROR"):
                    await cog.auto_match_confirm.callback.func(cog)
        self.assertEqual([call.args[1] for call in confirm.await_args_list], ["SECOND"])

    async def test_manual_confirmation_persists_first_timestamp(self) -> None:
        self.persisted.confirmation_tracking = True
        self.persisted.participants[0].confirmed = False
        self.persisted.participants[1].confirmed = False
        self.persisted._save_state()
        cog = match_module.MatchCommands(self.bot)
        ctx = SimpleNamespace(send=AsyncMock())
        match = self.persisted.model_copy(deep=True)
        with patch_sites(('elobot.extensions.match.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock:
            clock.now.return_value.timestamp.return_value = 1500.0
            await cog._confirm_match_participant(ctx, match, match.participants[0], match.participants[1], "TOKEN")
            clock.now.return_value.timestamp.return_value = 1600.0
            await cog._confirm_match_participant(ctx, match, match.participants[0], match.participants[1], "TOKEN")

        participant = self.persisted.participants[0]
        self.assertEqual((participant.confirmed, participant.confirmed_at, participant.confirmation_method),
                         (True, 1500.0, "manual"))

    async def test_preexisting_match_does_not_gain_confirmation_tracking(self) -> None:
        self.persisted.participants[0].confirmed = False
        self.persisted.participants[1].confirmed = False
        self.persisted._save_state()
        match = self.persisted.model_copy(deep=True)
        cog = match_module.MatchCommands(self.bot)
        await cog._confirm_match_participant(
            SimpleNamespace(send=AsyncMock()), match, match.participants[0], match.participants[1], "TOKEN",
        )
        participant = self.persisted.participants[0]
        self.assertTrue(participant.confirmed)
        self.assertEqual((participant.confirmed_at, participant.confirmation_method), (None, None))

    async def test_auto_confirmation_waits_twelve_hours_and_records_elapsed_time(self) -> None:
        self.persisted.matched_time = 1000.0
        self.persisted.confirmation_tracking = True
        self.persisted.participants[1].confirmed = False
        self.persisted.participants[1].districts = [None, None, None]
        self.persisted._save_state()
        self.model.find.return_value.to_list = AsyncMock(
            side_effect=lambda: [self.persisted.model_copy(deep=True)],
        )
        cog = match_module.MatchCommands(self.bot)
        with patch_sites(('elobot.extensions.match.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock, \
                patch_sites(('elobot.extensions.match.confirm_match', 'elobot.features.matches.lifecycle.confirm_match'), new_callable=AsyncMock):
            clock.now.return_value.timestamp.return_value = 44199.0
            await cog.auto_match_confirm.callback.func(cog)
            self.assertFalse(self.persisted.participants[1].confirmed)
            clock.now.return_value.timestamp.return_value = 44200.0
            await cog.auto_match_confirm.callback.func(cog)

        participant = self.persisted.participants[1]
        self.assertEqual((participant.confirmed, participant.confirmed_at, participant.confirmation_method),
                         (True, 44200.0, "automatic"))
        self.assertEqual(participant.districts, self.persisted.participants[0].districts)

    async def test_auto_confirmation_does_not_replace_a_newer_manual_confirmation(self) -> None:
        self.persisted.matched_time = 1000.0
        self.persisted.participants[1].confirmed = False
        self.persisted._save_state()
        stale = self.persisted.model_copy(deep=True)
        self.model.find.return_value.to_list = AsyncMock(return_value=[stale])
        self.persisted.participants[1].confirmed = True
        self.persisted.participants[1].confirmed_at = 44000.0
        self.persisted.participants[1].confirmation_method = "manual"
        self.persisted._save_state()
        cog = match_module.MatchCommands(self.bot)
        with patch_sites(('elobot.extensions.match.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock:
            clock.now.return_value.timestamp.return_value = 44200.0
            await cog.auto_match_confirm.callback.func(cog)

        participant = self.persisted.participants[1]
        self.assertEqual((participant.confirmed_at, participant.confirmation_method), (44000.0, "manual"))

    async def test_automatic_cancellation_waits_twelve_hours(self) -> None:
        self.persisted.matched_time = 1000.0
        for participant in self.persisted.participants:
            participant.confirmed = False
        self.model.find.return_value.to_list = AsyncMock(return_value=[self.persisted])
        cog = match_module.MatchCommands(self.bot)
        with patch_sites(('elobot.extensions.match.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as clock, \
                patch.object(match_commands, "cancel_match", new_callable=AsyncMock) as cancel:
            clock.now.return_value.timestamp.return_value = 44199.0
            await cog.auto_match_cancel.callback.func(cog)
            cancel.assert_not_awaited()
            clock.now.return_value.timestamp.return_value = 44200.0
            await cog.auto_match_cancel.callback.func(cog)
            cancel.assert_awaited_once_with(self.bot, "TOKEN")

    async def test_failed_message_id_write_is_recovered_without_resending(self) -> None:
        self.collection.update_one.side_effect = PyMongoError("write failed")
        with self.assertRaises(PyMongoError):
            await match_commands.confirm_match(self.bot, "TOKEN")
        self.assertIsNone(self.persisted.thread_message_id)
        self.messages[0].pin.assert_not_awaited()
        self.collection.update_one.side_effect = self.bind

        await match_commands.confirm_match(self.bot, "TOKEN")

        self.thread.send.assert_awaited_once()
        self.messages[0].pin.assert_awaited_once()
        self.assertEqual(self.persisted.thread_message_id, 456)

    async def test_recovery_skips_a_match_scheduled_while_waiting_for_the_shared_lock(self) -> None:
        from elobot.features.matches.message_lock import match_message_lock

        self.persisted.thread = "123"
        self.persisted._save_state()
        self.model.find.return_value.to_list = AsyncMock(return_value=[self.persisted.model_copy(deep=True)])
        cog = match_module.MatchCommands(self.bot)
        started = anyio.Event()

        async def recover():
            started.set()
            await cog.auto_match_confirm.callback.func(cog)

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks, match_message_lock("123"):
                tasks.start_soon(recover)
                await started.wait()
                self.persisted.times = [3_000_000_000, 3_000_000_000]

        self.thread.send.assert_not_awaited()
        self.thread.fetch_pinned_messages.assert_not_awaited()

    async def test_concurrent_confirmation_does_not_duplicate_thread_or_message(self) -> None:
        entered, release, retrying = anyio.Event(), anyio.Event(), anyio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.send_message(*args, **kwargs)

        async def retry():
            retrying.set()
            await match_commands.confirm_match(self.bot, "TOKEN")

        self.thread.send.side_effect = send
        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(match_commands.confirm_match, self.bot, "TOKEN")
                await entered.wait()
                tasks.start_soon(retry)
                await retrying.wait()
                release.set()

        self.channel.create_private_thread.assert_awaited_once()
        self.thread.send.assert_awaited_once()

    async def test_stale_binding_does_not_replace_a_newer_announcement(self) -> None:
        stale = self.persisted.model_copy(deep=True)
        self.persisted.thread = "321"
        self.persisted.thread_message_id = 789

        bound = await lifecycle_module.attach_thread_message(stale, 456)

        self.assertFalse(bound)
        self.assertEqual(self.persisted.thread_message_id, 789)
        self.assertIsNone(stale.thread_message_id)

    async def test_legacy_pin_is_adopted_without_resending(self) -> None:
        await match_commands.confirm_match(self.bot, "TOKEN")
        self.persisted.thread_message_id = None
        self.persisted._save_state()
        self.thread.send.reset_mock()

        await match_commands.confirm_match(self.bot, "TOKEN")

        self.assertEqual(self.persisted.thread_message_id, 456)
        self.thread.send.assert_not_awaited()
        self.thread.fetch_message.assert_not_awaited()

# noqa: SIZE_OK — shared fixtures keep the autostore and finalization race scenarios consistent
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
import anyio
from beanie import PydanticObjectId
from pymongo.errors import OperationFailure

import cogs.match_commands as match_commands
from cogs.match_commands import MatchCommands
from cogs.war_hosting import result_store, result_store_task, result_store_ui
from cogs.war_hosting.helper_store import HelperScoreStoreOutcome
from elobot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET
from elobot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, STAR_EMOJI
from common.players import self_hosting
from elobot.db import Match, Player
from elobot.db.final_models import FinalPlayerProfile
from elobot.db.match_models import Participant


COMPLETE = ["3.100", "2.90", "1.80"]
DISTRICTS = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
BOT_USER_ID = 9999


class TestMatchResultAutomation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        result_store.RESULT_AUTOSTORE_LOCKS.clear()
        result_store.RESULT_FINALIZATION_LOCKS.clear()
        self.bot = MagicMock()
        self.bot.user.id = BOT_USER_ID
        self.bot.owner.id = 999
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>", send=AsyncMock()))
        self.cog = MatchCommands(self.bot)
        self.autoqueue_patcher = patch.object(
            match_commands, "autoqueue_completed_match_participants", new_callable=AsyncMock
        )
        self.autoqueue_patcher.start()
        self.addCleanup(self.autoqueue_patcher.stop)
        self.player_lookup_patcher = patch.object(Player, "find_one", new=AsyncMock(return_value=None))
        self.player_lookup_patcher.start()
        self.addCleanup(self.player_lookup_patcher.stop)

    @staticmethod
    def _match(
            *,
            results: list[list[str | None]] | None = None,
    ) -> MagicMock:
        match = MagicMock(spec=Match)
        match.id = PydanticObjectId("0123456789abcdef01234567")
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.token = "TOKEN"
        match.thread = "321"
        match.is_war = True
        match.times = [100, 100]
        match.hosts = ["host-one", "host-two"]
        match.host_clans = ["#ONE", "#TWO"]
        match.old_msgs = [None, None]
        match.final_districts = DISTRICTS.copy()
        match.submitters = [None, None]
        values = results or [[None, None, None], COMPLETE.copy()]
        match.participants = [
            MagicMock(user_id="111", result=values[0]),
            MagicMock(user_id="222", result=values[1]),
        ]
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=SimpleNamespace(matched_count=1))
        return match

    @staticmethod
    def _slash_ctx(actor: int = 111) -> MagicMock:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = actor
        ctx.author.roles = []
        ctx.channel.id = 321
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    def test_command_metadata_has_player_and_detection_type_options(self) -> None:
        command = MatchCommands.match_result_autostore
        options = {str(option.name): option for option in command.options}
        self.assertEqual(set(options), {"player", "detection_type"})
        self.assertEqual([choice.value for choice in options["player"].choices], [0, 1])
        self.assertEqual(
            [(str(choice.name), choice.value) for choice in options["detection_type"].choices],
            [("Strict", "Strict"), ("Relaxed", "Relaxed")],
        )
        self.assertFalse(options["detection_type"].required)
        concurrency = MatchCommands.match_result_autostore.max_concurrency
        self.assertEqual(concurrency.concurrent, 1)
        self.assertIs(concurrency.bucket, CHANNEL_USER_CONCURRENCY_BUCKET)

    async def test_autostore_slash_routes_selected_side_and_detection_limit(self) -> None:
        for options, expected_limit in (({}, 2), ({"detection_type": "Relaxed"}, -2)):
            with self.subTest(options=options):
                match = self._match()
                ctx = self._slash_ctx(actor=777)
                with (
                    patch.object(match_commands, "Match") as match_model,
                    patch.object(MatchCommands, "store_match_result_automatically", new_callable=AsyncMock) as store,
                ):
                    match_model.find_one = AsyncMock(return_value=match)
                    await self.cog.match_result_autostore.callback(ctx, player=1, **options)
                ctx.defer.assert_awaited_once_with(ephemeral=True)
                store.assert_awaited_once_with(ctx, match, 1, max_allowed_attacks=expected_limit)

    async def test_autostore_slash_rejects_player_storing_own_result(self) -> None:
        # Given
        match = self._match()
        ctx = self._slash_ctx(actor=111)

        # When
        with (
                patch.object(match_commands, "Match") as match_model,
                patch.object(MatchCommands, "store_match_result_automatically", new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.match_result_autostore.callback(ctx, player=0)

        # Then
        store.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertIn("own result", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_match_autostore_persists_trusted_result_without_confirmation_ui(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        user = MagicMock(mention="<@111>", send=AsyncMock())
        self.bot.fetch_user.return_value = user
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE),
            changed=True,
            skipped=(),
            job_id="a" * 32,
            status="succeeded",
            summary="✅ Stored trusted scores.",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )

        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(
                    result_store.helper_store,
                    "store_helper_score",
                    new=AsyncMock(return_value=outcome),
                ) as store,
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)

        stored = fresh.get_pymongo_collection().update_one.await_args_list[0].args[1]["$set"]
        self.assertEqual(store.await_args.args[2].max_allowed_attacks, 2)
        self.assertEqual(stored["participants.0.result"], COMPLETE)
        self.assertNotIn("result_confirmed", stored)
        self.assertEqual(stored["submitters.0"], "777")
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.assertNotIn("confirm", ctx.send.await_args.args[0].lower())
        dm_row = user.send.await_args.kwargs["components"]
        dm_buttons = dm_row.components
        self.assertEqual([button.label for button in dm_buttons], ["Play Again"])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(match_commands.SUCCESS_EMOJI)))
        finalize.assert_awaited_once_with("TOKEN")

    async def test_second_stored_side_delivers_invitation_before_opponent_finishes(self) -> None:
        source = self._match(results=[[None, None, None], [None, None, None]])
        fresh = self._match(results=[[None, None, None], [None, None, None]])
        first = Match.model_construct(
            id=PydanticObjectId("0123456789abcdef01234568"),
            participants=[Participant(user_id="111", result=COMPLETE.copy()), Participant(user_id="222")],
        )
        second = Match.model_construct(
            id=fresh.id,
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        )
        player = Player.model_construct(
            id="player", user_id="111", tag="#PLAYER", elo=1000, highest_elo=1000, matches=[],
        )
        thread = MagicMock()
        thread.guild.id = 456
        thread.send = AsyncMock(return_value=SimpleNamespace(id=900))
        self.bot.http.request = AsyncMock(side_effect=ipy.errors.NotFound(
            SimpleNamespace(status=404, reason="Not Found"),
        ))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        self.bot.fetch_member = AsyncMock(return_value=MagicMock(has_role=MagicMock(return_value=False)))
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded",
            summary="ok", trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )

        async def update_player(_query, operation):
            for key, value in operation["$set"].items():
                setattr(player, key, value)
            return SimpleNamespace(matched_count=1)

        async def update_match(_query, operation):
            second.participants[0].result = operation["$set"]["participants.0.result"]
            return SimpleNamespace(matched_count=1)

        fresh.get_pymongo_collection().update_one.side_effect = update_match
        collection = MagicMock(update_one=AsyncMock(side_effect=update_player))
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(Player, "find_one", new=AsyncMock(side_effect=lambda *_a, **_k: player.model_copy())),
                patch.object(Player, "get_pymongo_collection", return_value=collection),
                patch.object(FinalPlayerProfile, "find_one", new=AsyncMock(return_value=None)),
                patch.object(self_hosting.Match, "find",
                             return_value=MagicMock(to_list=AsyncMock(return_value=[first, second]))),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(self._slash_ctx(actor=777), source, 0)

        self.assertEqual(fresh.participants[0].result, COMPLETE)
        self.assertEqual(fresh.participants[1].result, [None, None, None])
        self.assertEqual(second.participants[0].result, COMPLETE)
        self.assertEqual(player.self_host_invitation_message_id, 900)
        self.assertIn("<@111>", thread.send.await_args.args[0])

    async def test_failed_result_write_does_not_check_or_send_invitation(self) -> None:
        source = self._match(results=[[None, None, None], [None, None, None]])
        fresh = self._match(results=[[None, None, None], [None, None, None]])
        fresh.get_pymongo_collection().update_one.side_effect = OperationFailure("write failed")
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded",
            summary="ok", trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        ctx = self._slash_ctx(actor=777)
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(Player, "find_one", new=AsyncMock()) as player_lookup,
                self.assertLogs(level="ERROR"),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)
        player_lookup.assert_not_awaited()
        self.assertEqual(fresh.participants[0].result, [None, None, None])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_persisted_scheduled_autostore_marks_started_message_ended_after_write(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh.old_msgs[0] = 444
        source.old_msgs[0] = 444
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        events = []
        fresh.get_pymongo_collection().update_one.side_effect = (
            lambda _query, _updates: events.append("write") or SimpleNamespace(matched_count=1)
        )
        message = MagicMock(spec=ipy.Message)
        message.embeds = [ipy.Embed(title="**Match Started**")]
        message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("edit"))
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        request = result_store.AutomaticResultStoreRequest(
            match=source,
            player=0,
            interactive=False,
            expected_started_at=100,
            scheduled_delay_minutes=40,
        )

        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(
                    result_store.helper_store,
                    "store_helper_score",
                    new=AsyncMock(return_value=outcome),
                ),
                patch.object(result_store_ui, "notify_stored_result", new_callable=AsyncMock),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            with patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=fresh)):
                await result_store.store_match_result_automatically(self.cog, ctx, request)

        self.assertEqual(events, ["write", "edit"])
        self.bot.fetch_channel.assert_awaited_once_with(321)
        channel.fetch_message.assert_awaited_once_with(444)
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embeds"][0].title, "**Match Ended**")
        finalize.assert_awaited_once_with("TOKEN")

    async def test_scheduled_autostore_updates_started_embed_before_notification_failure(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh.old_msgs[0] = 444
        source.old_msgs[0] = 444
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        events = []
        fresh.get_pymongo_collection().update_one.side_effect = (
            lambda _query, _updates: events.append("write") or SimpleNamespace(matched_count=1)
        )
        message = MagicMock(spec=ipy.Message)
        message.embeds = [ipy.Embed(title="**Match Started**")]
        message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("edit"))
        channel = MagicMock(fetch_message=AsyncMock(return_value=message))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        response = MagicMock(status=503, reason="Service Unavailable", headers={})
        notification_error = ipy.errors.HTTPException(response, "Discord notification failed")
        request = result_store.AutomaticResultStoreRequest(
            match=source,
            player=0,
            interactive=False,
            expected_started_at=100,
            scheduled_delay_minutes=40,
        )

        async def fail_notification(_request) -> None:
            events.append("notify")
            raise notification_error

        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(result_store_ui, "notify_stored_result", side_effect=fail_notification),
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=fresh)),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            with self.assertRaises(ipy.errors.HTTPException):
                await result_store.store_match_result_automatically(self.cog, ctx, request)

        self.assertEqual(events, ["write", "edit", "notify"])
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embeds"][0].title, "**Match Ended**")

    async def test_stored_result_dm_formats_complete_result(self) -> None:
        match = self._match(results=[COMPLETE.copy(), COMPLETE.copy()])
        user = MagicMock(send=AsyncMock())

        await result_store_ui.send_stored_result_dm(
            result_store_ui.StoredResultDmRequest(match=match, player=0, user=user)
        )

        result = user.send.await_args.kwargs["embed"].fields[0].value
        self.assertNotIn(str(IMPORTANT_EMOJI), result)
        self.assertEqual(
            [button.label for button in user.send.await_args.kwargs["components"].components],
            ["Play Again"],
        )
        for district, score in zip(DISTRICTS, COMPLETE):
            stars, destruction = score.split(".")
            self.assertIn(f"{district}: {stars}{STAR_EMOJI} {destruction}%", result)

    async def test_incomplete_merged_result_writes_nothing_without_confirmation_surface(self) -> None:
        source = self._match(results=[[None, "2.90", None], COMPLETE.copy()])
        fresh = self._match(results=[[None, "2.90", None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=("2.85", "2.90", ""),
            changed=True,
            skipped=(),
            job_id="job",
            status="failed",
            summary="partial",
            trusted=((DISTRICTS[0], "2.85"),),
        )
        finish = AsyncMock()
        notify = AsyncMock()

        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(MatchCommands, "_finish_stored_result_side", new=finish),
                patch.object(result_store_ui, "notify_stored_result", new=notify),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)

        fresh.get_pymongo_collection().update_one.assert_not_awaited()
        self.assertEqual(fresh.participants[0].result, [None, "2.90", None])
        self.assertEqual(fresh.submitters, [None, None])
        content = ctx.send.await_args.args[0]
        ctx.send.assert_awaited_once()
        self.assertTrue(content.startswith(str(ERROR_EMOJI)))
        self.assertIn("result", content.lower())
        self.assertIn("three", content.lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.bot.fetch_user.assert_not_awaited()
        notify.assert_not_awaited()
        finish.assert_not_awaited()

    async def test_conflicting_stored_result_returns_error_without_changes(self) -> None:
        old = ["3.100", "2.90", "1.70"]
        source = self._match(results=[old.copy(), COMPLETE.copy()])
        fresh = self._match(results=[old.copy(), COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=((DISTRICTS[2], COMPLETE[2]),),
        )
        finish = AsyncMock()
        notify = AsyncMock()
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(MatchCommands, "_finish_stored_result_side", new=finish),
                patch.object(result_store_ui, "notify_stored_result", new=notify),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)

        fresh.get_pymongo_collection().update_one.assert_not_awaited()
        self.assertEqual(fresh.participants[0].result, old)
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.bot.fetch_user.assert_not_awaited()
        notify.assert_not_awaited()
        finish.assert_not_awaited()

    async def test_self_store_persists_trusted_result_and_shows_result_without_confirmation(self) -> None:
        source = self._match(results=[["3.100", None, None], COMPLETE.copy()])
        fresh = self._match(results=[["3.100", None, None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=111)
        user = MagicMock(mention="<@111>", send=AsyncMock())
        self.bot.fetch_user.return_value = user
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="a" * 32,
            status="succeeded", summary="ok", trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(
                    result_store.helper_store,
                    "store_helper_score",
                    new=AsyncMock(return_value=outcome),
                ) as store,
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_self_result(ctx, source, 0)

        updates = fresh.get_pymongo_collection().update_one.await_args.args[1]["$set"]
        self.assertEqual(store.await_args.args[2].max_allowed_attacks, 2)
        self.assertEqual(updates["participants.0.result"], COMPLETE)
        self.assertNotIn("result_confirmed", updates)
        self.assertEqual(updates["submitters.0"], str(BOT_USER_ID))
        self.assertNotIn("participants.0.pending_result", updates)
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        content = ctx.send.await_args.args[0]
        self.assertTrue(content.startswith(str(match_commands.SUCCESS_EMOJI)))
        for district, score in zip(DISTRICTS, COMPLETE):
            stars, destruction = score.split(".")
            with self.subTest(district=district):
                self.assertIn(f"{district}: {stars}{STAR_EMOJI} {destruction}%", content)
        dm_buttons = user.send.await_args.kwargs["components"].components
        self.assertEqual([button.label for button in dm_buttons], ["Play Again"])
        finalize.assert_awaited_once_with("TOKEN")

    async def test_incomplete_self_store_writes_nothing_and_has_no_confirm(self) -> None:
        source = self._match(results=[["3.100", None, None], COMPLETE.copy()])
        fresh = self._match(results=[["3.100", None, None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=111)
        outcome = HelperScoreStoreOutcome(
            results=("3.100", "2.90", None), changed=True, skipped=(), job_id="a" * 32,
            status="failed", summary="partial", trusted=tuple(zip(DISTRICTS[:2], COMPLETE[:2])),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_self_result(ctx, source, 0)

        fresh.get_pymongo_collection().update_one.assert_not_awaited()
        self.assertEqual(fresh.participants[0].result, ["3.100", None, None])
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_self_store_does_not_write_result_after_side_restarts(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh.times[0] = 101
        ctx = self._slash_ctx(actor=111)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(MatchCommands, "_finish_stored_result_side", new_callable=AsyncMock) as cleanup,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_self_result(ctx, source, 0)

        fresh.get_pymongo_collection().update_one.assert_not_awaited()
        self.bot.fetch_user.assert_not_awaited()
        cleanup.assert_not_awaited()

    async def test_identical_retry_does_not_write_or_notify(self) -> None:
        unchanged = self._match(results=[COMPLETE.copy(), COMPLETE.copy()])
        unchanged_ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=False, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=((DISTRICTS[2], COMPLETE[2]),),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(result_store, "invite_eligible_players", wraps=self_hosting.invite_eligible_players)
                as invitation_check,
        ):
            match_model.find_one = AsyncMock(side_effect=[unchanged, unchanged])
            await self.cog.store_match_result_automatically(unchanged_ctx, unchanged, 0)
        unchanged.get_pymongo_collection().update_one.assert_not_awaited()
        unchanged_ctx.send.assert_not_awaited()
        self.bot.fetch_user.assert_not_awaited()
        invitation_check.assert_awaited_once_with(self.bot, ("111",), pending_only=True)

    def test_confirmation_commands_and_callbacks_are_not_registered(self) -> None:
        self.assertFalse(hasattr(MatchCommands, "match_result_confirm"))
        self.assertFalse(hasattr(MatchCommands, "match_result_confirm_button"))
        self.assertFalse(hasattr(result_store, "ConfirmResultRequest"))
        self.assertFalse(hasattr(result_store, "confirm_match_result"))
        self.assertFalse(hasattr(result_store_ui, "result_confirm_button"))

    async def test_two_sides_serialize_result_persistence_and_finalize_once(self) -> None:
        database_match = self._match(
            results=[[None, "2.90", "1.80"], ["3.100", None, "1.80"]],
        )
        ctx0 = self._slash_ctx(actor=777)
        ctx1 = self._slash_ctx(actor=888)
        both_helpers_started = anyio.Event()
        release_helpers = anyio.Event()
        helper_count = 0
        first_set_started = anyio.Event()
        release_first_set = anyio.Event()
        set_count = 0

        async def helper_outcome(_runtime, _ctx, request):
            nonlocal helper_count
            helper_count += 1
            if helper_count == 2:
                both_helpers_started.set()
            await release_helpers.wait()
            district = DISTRICTS[request.player]
            return HelperScoreStoreOutcome(
                results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
                trusted=((district, COMPLETE[request.player]),),
            )

        async def persist(_query, operation):
            nonlocal set_count
            updates = operation["$set"]
            set_count += 1
            if set_count == 1:
                first_set_started.set()
                await release_first_set.wait()
            for player in (0, 1):
                result_key = f"participants.{player}.result"
                if result_key in updates:
                    database_match.participants[player].result = list(updates[result_key])
            return SimpleNamespace(matched_count=1)

        database_match.get_pymongo_collection().update_one = AsyncMock(side_effect=persist)
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(side_effect=helper_outcome)),
                patch.object(MatchCommands, "_finish_stored_result_side", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(return_value=database_match)
            with anyio.fail_after(5):
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(self.cog.store_match_result_automatically, ctx0, database_match, 0)
                    task_group.start_soon(self.cog.store_match_result_automatically, ctx1, database_match, 1)
                    await both_helpers_started.wait()
                    release_helpers.set()
                    await first_set_started.wait()
                    self.assertEqual(match_model.find_one.await_count, 3)
                    release_first_set.set()

        self.assertEqual(database_match.get_pymongo_collection().update_one.await_count, 2)
        finalize.assert_awaited_once_with("TOKEN")

    async def test_no_trusted_score_preserves_results(self) -> None:
        match = self._match(results=[COMPLETE.copy(), COMPLETE.copy()])
        match.old_msgs[0] = 444
        ctx = self._slash_ctx(actor=777)
        self.bot.fetch_channel = AsyncMock()
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=False, skipped=(), job_id="job", status="succeeded", summary="ok",
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.store_match_result_automatically(ctx, match, 0)
        match.get_pymongo_collection().update_one.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    async def test_blocked_result_dm_retains_slot_and_finishes_side_tasks(self) -> None:
        from elobot.db import BaseStore

        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        stored = BaseStore(clan_tag="#ONE", slot="1", reservation_id="stored-slot", ready=True)
        for match in (source, fresh):
            match.participants[0].base_store = stored.model_copy(deep=True)
            match.participants[0].opponent_bases_stored = True
        ctx = self._slash_ctx(actor=777)
        response = MagicMock(status=403, reason="Forbidden")
        response.headers = {}
        forbidden = ipy.errors.Forbidden(response, "Direct messages are closed")
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE), changed=True, skipped=(), job_id="job", status="succeeded", summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(result_store_ui, "send_stored_result_dm", new=AsyncMock(side_effect=forbidden)),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock) as clear_store,
                patch.object(match_commands, "free_clan", new_callable=AsyncMock) as free_clan,
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock) as remove_task,
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)
        clear_store.assert_not_awaited()
        self.assertEqual(fresh.participants[0].base_store, stored)
        self.assertTrue(fresh.participants[0].opponent_bases_stored)
        free_clan.assert_awaited_once_with("#ONE", expected_thread="321")
        remove_task.assert_awaited_once_with("321", "111")
        finalize.assert_awaited_once_with("TOKEN")

    async def test_http_autostore_merges_trusted_score_and_finalizes_without_confirmation(self) -> None:
        from tests._helper_score_fixture import HelperScoreState

        fixture = HelperScoreState(self)
        await fixture.asyncSetUp()
        try:
            fixture.status_responses = [(
                200,
                fixture.job_document("failed", {
                    "Wizard Valley": fixture.score(2, 85),
                    "Balloon Lagoon": None,
                    "Dragon Cliffs": fixture.score(1, 70, verified=False),
                }),
            )]
            helper_outcome = await fixture.invoke(fixture.make_context(), fixture.make_match())
            self.assertEqual(fixture.create_payloads[0]["type"], "fetch_score")
            self.assertEqual(helper_outcome.trusted, (("Wizard Valley", "2.85"),))
        finally:
            await fixture.asyncTearDown()

        source = self._match(results=[[None, "2.90", "1.80"], COMPLETE.copy()])
        source.final_districts = ["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"]
        fresh = self._match(results=[[None, "2.90", "1.80"], COMPLETE.copy()])
        fresh.final_districts = source.final_districts.copy()
        ctx = self._slash_ctx(actor=777)
        user = MagicMock(mention="<@111>", send=AsyncMock())
        self.bot.fetch_user.return_value = user
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=helper_outcome)),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock) as finalize,
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)
        self.assertEqual(fresh.participants[0].result, ["2.85", "2.90", "1.80"])
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.assertEqual(
            [button.label for button in user.send.await_args.kwargs["components"].components],
            ["Play Again"],
        )
        finalize.assert_awaited_once_with("TOKEN")

    async def test_complete_autostore_finishes_side_before_finalization_and_notification(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE),
            changed=True,
            skipped=(),
            job_id="job",
            status="succeeded",
            summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        events: list[str] = []
        fresh.get_pymongo_collection().update_one.side_effect = (
            lambda _query, _updates: events.append("write") or SimpleNamespace(matched_count=1)
        )

        async def finish(_match, _player) -> None:
            events.append("finish")

        async def finalize(_token) -> bool:
            events.append("finalize")
            return True

        async def notify(_request) -> None:
            events.append("notify")

        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(self.cog, "_finish_stored_result_side", side_effect=finish),
                patch.object(self.cog, "_finalize_match_results_locked", side_effect=finalize),
                patch.object(result_store_ui, "notify_stored_result", side_effect=notify),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await self.cog.store_match_result_automatically(ctx, source, 0)

        self.assertEqual(events, ["write", "finish", "finalize", "notify"])

    async def test_background_autostore_skips_a_result_completed_before_its_lock(self) -> None:
        match = self._match(results=[COMPLETE.copy(), COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await result_store.store_match_result_automatically(
                self.cog,
                ctx,
                result_store.AutomaticResultStoreRequest(
                    match=match,
                    player=0,
                    interactive=False,
                    expected_started_at=100,
                ),
            )

        store.assert_not_awaited()
        ctx.send.assert_not_awaited()

    async def test_background_autostore_logs_its_scheduled_delay(self) -> None:
        # Given
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        fresh = self._match(results=[[None, None, None], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        player_user = MagicMock(mention="<@111>", send=AsyncMock())
        self.bot.fetch_user.return_value = player_user
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE),
            changed=True,
            skipped=(),
            job_id="job",
            status="succeeded",
            summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        request = result_store.AutomaticResultStoreRequest(
            match=source,
            player=0,
            interactive=False,
            expected_started_at=100,
            scheduled_delay_minutes=40,
        )

        # When
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
                patch.object(MatchCommands, "_finish_stored_result_side", new_callable=AsyncMock),
                patch.object(self.cog, "_finalize_match_results_locked", new_callable=AsyncMock),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, fresh])
            await result_store.store_match_result_automatically(self.cog, ctx, request)

        # Then
        content = ctx.send.await_args.args[0]
        self.assertTrue(content.startswith(str(match_commands.SUCCESS_EMOJI)))
        self.assertIn(str(request.scheduled_delay_minutes), content)
        self.assertIn(player_user.mention, content)
        ctx.send.assert_awaited_once()

    async def test_background_autostore_skips_a_restarted_side(self) -> None:
        match = self._match(results=[[None, None, None], COMPLETE.copy()])
        match.times[0] = 101
        ctx = self._slash_ctx(actor=777)
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await result_store.store_match_result_automatically(
                self.cog,
                ctx,
                result_store.AutomaticResultStoreRequest(
                    match=match,
                    player=0,
                    interactive=False,
                    expected_started_at=100,
                ),
            )

        store.assert_not_awaited()
        ctx.send.assert_not_awaited()

    async def test_background_autostore_skips_a_result_completed_while_helper_runs(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        completed = self._match(results=[["3.100", "2.90", "1.70"], COMPLETE.copy()])
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE),
            changed=True,
            skipped=(),
            job_id="job",
            status="succeeded",
            summary="ok",
            trusted=((DISTRICTS[2], COMPLETE[2]),),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, completed])
            await result_store.store_match_result_automatically(
                self.cog,
                ctx,
                result_store.AutomaticResultStoreRequest(
                    match=source,
                    player=0,
                    interactive=False,
                    expected_started_at=100,
                ),
            )

        completed.get_pymongo_collection().update_one.assert_not_awaited()
        ctx.send.assert_not_awaited()

    async def test_background_autostore_skips_a_restart_while_helper_runs(self) -> None:
        source = self._match(results=[[None, None, None], COMPLETE.copy()])
        restarted = self._match(results=[[None, None, None], COMPLETE.copy()])
        restarted.times[0] = 101
        ctx = self._slash_ctx(actor=777)
        outcome = HelperScoreStoreOutcome(
            results=tuple(COMPLETE),
            changed=True,
            skipped=(),
            job_id="job",
            status="succeeded",
            summary="ok",
            trusted=tuple(zip(DISTRICTS, COMPLETE)),
        )
        with (
                patch.object(result_store, "Match") as match_model,
                patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(return_value=outcome)),
        ):
            match_model.find_one = AsyncMock(side_effect=[source, restarted])
            await result_store.store_match_result_automatically(
                self.cog,
                ctx,
                result_store.AutomaticResultStoreRequest(
                    match=source,
                    player=0,
                    interactive=False,
                    expected_started_at=100,
                ),
            )

        restarted.get_pymongo_collection().update_one.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_scheduled_result_store_runs_for_the_same_incomplete_side(self) -> None:
        match = self._match(results=[[None, None, None], COMPLETE.copy()])
        channel = MagicMock(name="match-thread")
        channel.name = "🟢 Match TOKEN"
        extension = MagicMock()
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        self.bot.get_ext.return_value = extension
        request = result_store_task.ScheduledResultStoreRequest(
            token="TOKEN",
            player=0,
            requested_by="777",
            started_at=100,
            submitter_id=str(BOT_USER_ID),
        )

        with (
                patch.object(result_store_task, "Match") as match_model,
                patch.object(result_store_task.result_store, "store_match_result_automatically",
                             new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await result_store_task.run_scheduled_result_store(self.bot, request)

        runtime, ctx, store_request = store.await_args.args
        self.assertIs(runtime, extension)
        self.assertIsInstance(ctx, result_store_ui.ScheduledResultStoreContext)
        self.assertEqual(str(ctx.author.id), "777")
        self.assertIs(ctx.channel, channel)
        self.assertIs(store_request.match, match)
        self.assertFalse(store_request.interactive)
        self.assertFalse(store_request.allow_cancellation)
        self.assertEqual(store_request.max_allowed_attacks, 2)
        self.assertEqual(store_request.submitter_id, str(BOT_USER_ID))
        self.assertEqual(store_request.expected_started_at, 100)
        self.assertEqual(
            store_request.scheduled_delay_minutes,
            int(result_store_task.SCHEDULED_RESULT_STORE_DELAY.total_seconds() // 60),
        )

    async def test_scheduled_result_store_skips_an_already_stored_side(self) -> None:
        match = self._match(results=[COMPLETE.copy(), COMPLETE.copy()])
        request = result_store_task.ScheduledResultStoreRequest(
            token="TOKEN",
            player=0,
            requested_by="777",
            started_at=100,
        )
        with (
                patch.object(result_store_task, "Match") as match_model,
                patch.object(result_store_task.result_store, "store_match_result_automatically",
                             new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await result_store_task.run_scheduled_result_store(self.bot, request)

        store.assert_not_awaited()
        self.bot.fetch_channel.assert_not_called()

    def test_scheduled_result_store_uses_a_forty_minute_date_trigger(self) -> None:
        now = datetime(2026, 8, 24, 12, 0, 0)
        request = result_store_task.ScheduledResultStoreRequest(
            token="TOKEN",
            player=0,
            requested_by="777",
            started_at=100,
        )
        task = MagicMock()
        with (
                patch.object(result_store_task, "datetime") as clock,
                patch.object(result_store_task.ipy, "Task", return_value=task) as task_type,
        ):
            clock.now.return_value = now
            result_store_task.schedule_automatic_result_store(self.bot, request)

        trigger = task_type.call_args.args[1]
        self.assertIsInstance(trigger, ipy.DateTrigger)
        self.assertEqual(trigger.target, now + timedelta(minutes=40))
        task.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

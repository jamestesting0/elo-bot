import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.settings.document import DocumentSettings
from beanie.odm.fields import ExpressionField
from pymongo.errors import OperationFailure

import elo_bot.extensions.giveaway as commands
from elo_bot.db import Giveaway


class TestGiveawayStateConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.row = {
            "_id": "42", "end_timestamp": 3_000_000_000, "ended": False, "entries": ["100"],
            "sponsor_id": "300", "min_matches": 0, "min_elo": 0, "channel_id": 50,
        }
        self.db_failure = False
        self.events: list[str] = []
        self.enterContext(patch.object(Giveaway, "_document_settings", DocumentSettings(
            name="giveaways", use_state_management=True,
        )))

        async def find_update(query, update, **_kwargs):
            if self.db_failure:
                raise OperationFailure("synthetic write failure")
            if "$addToSet" in update:
                if "ended" in query and self.row["ended"]:
                    return None
                if "end_timestamp" in query and self.row["end_timestamp"] <= query["end_timestamp"]["$gt"]:
                    return None
                user_id = update["$addToSet"]["entries"]
                if user_id in self.row["entries"]:
                    return None
                self.row["entries"].append(user_id)
            else:
                self.row.update(update["$set"])
                self.events.append("commit")
            return deepcopy(self.row)

        async def update_one(query, update):
            if self.db_failure:
                raise OperationFailure("synthetic write failure")
            if any(self.row.get(key) != value for key, value in query.items()):
                return SimpleNamespace(matched_count=0)
            self.row.update(update["$set"])
            self.events.append("commit")
            return SimpleNamespace(matched_count=1)

        self.collection = MagicMock(
            find_one_and_update=AsyncMock(side_effect=find_update), update_one=AsyncMock(side_effect=update_one),
        )
        self.enterContext(patch.object(Giveaway, "get_pymongo_collection", return_value=self.collection))
        self.enterContext(patch.object(Giveaway, "get", new=AsyncMock(side_effect=self.load)))
        self.message = MagicMock(id=42)
        self.message.embeds = [ipy.Embed(title="Prize Giveaway", fields=[ipy.EmbedField(
            name="Details", value="Ends: later\nEntries: 1",
        )])]
        self.message.components = [ipy.ActionRow(ipy.Button(
            style=ipy.ButtonStyle.SECONDARY, custom_id="enter_giveaway", label="Enter",
        ))]

        async def edit(**kwargs):
            if "embed" in kwargs:
                self.message.embeds = [deepcopy(kwargs["embed"])]
            if "components" in kwargs:
                self.message.components = deepcopy(kwargs["components"])
            self.events.append("edit")

        self.message.edit = AsyncMock(side_effect=edit)
        self.message.reply = AsyncMock(side_effect=lambda *_args: self.events.append("announce"))
        self.channel = MagicMock(
            id=50, mention="<#50>", fetch_message=AsyncMock(return_value=self.message),
            send=AsyncMock(return_value=self.message),
        )
        self.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        self.bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=self.channel))
        self.cog = commands.GiveawayCommands(self.bot)

    def load(self, *_args) -> Giveaway:
        model = Giveaway.model_validate(deepcopy(self.row))
        model._save_state()
        return model

    async def test_ended_and_expired_giveaways_reject_atomic_entry(self) -> None:
        for ended, timestamp in ((True, 3_000_000_000), (False, 1)):
            with self.subTest(ended=ended):
                self.row.update(ended=ended, end_timestamp=timestamp, entries=["100"])
                result = await Giveaway.add_entry("42", "200")
                self.assertIsNone(result)
                self.assertEqual(self.row["entries"], ["100"])

    async def test_finished_cleanup_preserves_unended_giveaways(self) -> None:
        self.row["end_timestamp"] = 1

        def find(*filters):
            only_ended = {"ended": True} in filters
            selected = [] if only_ended and not self.row["ended"] else [self.load()]
            return MagicMock(to_list=AsyncMock(return_value=selected))

        with (
            patch.object(Giveaway, "end_timestamp", ExpressionField("end_timestamp"), create=True),
            patch.object(Giveaway, "find", side_effect=find),
            patch.object(Giveaway, "delete", new=AsyncMock()) as delete,
        ):
            await self.cog.finished_giveaway_delete.callback.func(self.cog)
            delete.assert_not_awaited()
            self.row["ended"] = True
            await self.cog.finished_giveaway_delete.callback.func(self.cog)
            delete.assert_awaited_once()

    async def test_closed_entry_callback_reports_failure_without_success(self) -> None:
        self.row["ended"] = True
        ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock())
        ctx.message = self.message
        ctx.author.id = 200
        with patch.object(commands, "Player") as players, patch.object(commands, "Season") as seasons:
            players.find_one = AsyncMock(return_value=SimpleNamespace(elo=1500, matches=[]))
            seasons.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
            await self.cog.enter_giveaway.callback.func(self.cog, ctx)

        self.assertEqual(self.row["entries"], ["100"])
        self.assertNotIn("now entered", ctx.send.await_args.args[0])
        self.message.edit.assert_not_awaited()

    async def test_end_persists_winner_before_publishing_success(self) -> None:
        await self.cog.end_giveaway("42")

        self.assertEqual(self.events, ["edit", "commit", "edit", "announce"])
        self.assertEqual(self.message.edit.await_args_list[0].kwargs["embed"].title, "Prize Giveaway `(Ending)`")
        self.assertEqual(self.message.edit.await_args_list[1].kwargs["embed"].title, "Prize Giveaway `(Ended)`")
        self.assertTrue(self.row["ended"])
        self.assertEqual(self.row.get("winner_id"), "100")
        self.assertTrue(self.row.get("draw_id"))
        self.assertEqual(self.message.components, [])

    async def test_stale_automatic_end_cannot_reroll_a_finished_giveaway(self) -> None:
        self.row["ended"] = True
        await self.cog.end_giveaway("42")

        self.message.edit.assert_not_awaited()
        self.message.reply.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_draw_conflict_preserves_a_concurrent_entry(self) -> None:
        snapshot = self.load()
        self.row["entries"].append("200")
        claimed = await snapshot.claim_draw()

        self.assertFalse(claimed)
        self.assertEqual(self.row["entries"], ["100", "200"])
        self.assertFalse(self.row["ended"])

    async def test_stale_reroll_cannot_replace_a_newer_same_winner_draw(self) -> None:
        self.row.update(ended=True, winner_id="100", draw_id="ORIGINAL")
        first, stale = self.load(), self.load()
        self.assertTrue(await first.claim_draw(reroll=True))
        saved_draw = self.row["draw_id"]
        claimed = await stale.claim_draw(reroll=True)

        self.assertFalse(claimed)
        self.assertEqual(self.row["draw_id"], saved_draw)

    async def test_explicit_reroll_keeps_a_distinct_durable_draw(self) -> None:
        await self.cog.end_giveaway("42")
        initial_draw = self.row.get("draw_id")
        await self.cog.end_giveaway("42", reroll=True)

        self.assertNotEqual(self.row.get("draw_id"), initial_draw)
        self.assertEqual(self.row.get("winner_id"), "100")
        self.assertEqual(self.message.reply.await_count, 2)
        self.assertEqual(self.message.embeds[0].fields[0].value.count("Winner:"), 1)

    async def test_concurrent_end_attempts_publish_only_one_winner(self) -> None:
        import anyio

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.cog.end_giveaway, "42")
                tasks.start_soon(self.cog.end_giveaway, "42")

        self.message.reply.assert_awaited_once()
        self.assertEqual(self.row.get("winner_id"), "100")

    async def test_end_database_failure_has_no_winner_success(self) -> None:
        self.db_failure = True
        with self.assertRaises(OperationFailure):
            await self.cog.end_giveaway("42")

        self.assertFalse(self.row["ended"])
        self.message.reply.assert_not_awaited()
        self.assertNotIn("(Ended)", self.message.embeds[0].title)
        self.assertEqual(self.message.components, [])

    async def test_failed_result_publication_keeps_a_neutral_closed_announcement(self) -> None:
        edit_message = self.message.edit.side_effect
        error = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))

        async def fail_result(**kwargs):
            if "(Ended)" in kwargs["embed"].title:
                raise error
            await edit_message(**kwargs)

        self.message.edit.side_effect = fail_result
        with self.assertRaises(ipy.errors.HTTPException):
            await self.cog.end_giveaway("42")

        self.assertTrue(self.row["ended"])
        self.assertEqual(self.row["winner_id"], "100")
        self.assertEqual(self.message.components, [])
        self.assertIn("(Ending)", self.message.embeds[0].title)
        self.message.reply.assert_not_awaited()

    async def test_failed_pending_edit_leaves_the_giveaway_open(self) -> None:
        self.message.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.cog.end_giveaway("42")

        self.assertFalse(self.row["ended"])
        self.collection.update_one.assert_not_awaited()
        self.message.reply.assert_not_awaited()

    async def test_created_giveaway_activation_failure_reports_saved_state(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        edit_message = self.message.edit.side_effect
        error = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))

        async def fail_activation(**kwargs):
            if isinstance(kwargs["components"], ipy.Button):
                raise error
            await edit_message(**kwargs)

        self.message.edit.side_effect = fail_activation
        with patch.object(Giveaway, "insert", new=AsyncMock()) as insert:
            await self.cog.giveaway_create.callback.func(
                self.cog, ctx, "Prize", MagicMock(id=300, mention="<@300>"), self.channel, days=1,
                giveaway_description="Sponsor terms remain visible.",
            )

        insert.assert_awaited_once()
        self.assertIn("was saved", ctx.send.await_args.args[0])
        self.assertEqual(self.message.components, [])
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].description, "Sponsor terms remain visible.")

    async def test_create_insert_failure_never_publishes_active_controls(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.author.mention = "<@100>"
        with patch.object(Giveaway, "insert", new=AsyncMock(side_effect=OperationFailure("insert failed"))):
            await self.cog.giveaway_create.callback.func(
                self.cog, ctx, "Prize", MagicMock(id=300, mention="<@300>"), self.channel, days=1,
            )

        self.assertIsInstance(self.channel.send.await_args.kwargs["components"], list)
        self.assertEqual(self.channel.send.await_args.kwargs["components"], [])
        pending = self.channel.send.await_args.kwargs["embed"]
        self.assertNotIn("Confirming", pending.title)
        self.assertEqual(pending.fields[-1].value, "Opening entries…")
        self.assertEqual(self.message.components, [])
        self.assertNotIn("created in", ctx.send.await_args.args[0])

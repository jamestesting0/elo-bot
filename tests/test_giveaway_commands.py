import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import elo_bot.extensions.giveaway as giveaway_module
from elo_bot.db import Giveaway
from elo_bot.extensions.giveaway import GiveawayCommands


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FUTURE_TIMESTAMP = 1_800_000_000


def giveaway_document(
        *, ended: bool = False, entries: list[str] | None = None,
        end_timestamp: float = FUTURE_TIMESTAMP) -> SimpleNamespace:
    document = SimpleNamespace(
        id="300", end_timestamp=end_timestamp, ended=ended, entries=entries or [],
        sponsor_id="200", min_elo=0, min_matches=0, channel_id=400,
        winner_id=None, draw_id=None, save_changes=AsyncMock(), delete=AsyncMock(),
    )

    async def claim_draw(*, reroll: bool = False) -> bool:
        if document.ended != reroll:
            return False
        document.ended = True
        document.winner_id = document.entries[0] if document.entries else None
        document.draw_id = "DRAW"
        return True

    document.claim_draw = AsyncMock(side_effect=claim_draw)
    return document


def giveaway_query(rows: list[SimpleNamespace]) -> MagicMock:
    query = MagicMock()
    query.to_list = AsyncMock(return_value=rows)
    return query


class QueryField:
    def __le__(self, other: float) -> tuple[str, float]:
        return ("lte", other)


class TestGiveawayCreate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = GiveawayCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author.mention = "<@100>"
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock()
        self.sponsor = MagicMock(spec=ipy.User)
        self.sponsor.id = 200
        self.sponsor.mention = "<@200>"
        self.message = MagicMock(id=300)
        self.message.edit = AsyncMock()
        self.channel = MagicMock(spec=ipy.GuildText)
        self.channel.id = 400
        self.channel.mention = "<#400>"
        self.channel.send = AsyncMock(return_value=self.message)
        self.mock_datetime = self.enterContext(patch("elo_bot.extensions.giveaway.datetime"))
        self.mock_datetime.now.return_value = NOW
        self.mock_datetime.fromtimestamp.side_effect = datetime.fromtimestamp
        self.trigger_datetime = self.enterContext(patch("interactions.models.internal.tasks.triggers.datetime"))
        self.trigger_datetime.now.return_value = NOW.replace(tzinfo=None)
        self.insert = self.enterContext(
            patch("elo_bot.extensions.giveaway.Giveaway.insert", new_callable=AsyncMock)
        )
        self.enterContext(patch("elo_bot.extensions.giveaway.Giveaway.get_pymongo_collection"))
        self.task = self.enterContext(patch("elo_bot.extensions.giveaway.ipy.Task"))

    async def create_giveaway(
            self, *, days: int | None = None, hours: int | None = None, timestamp: str | None = None,
            role_mention: ipy.Role | None = None) -> None:
        time_options: dict[str, int | str] = {}
        if days is not None:
            time_options["days"] = days
        if hours is not None:
            time_options["hours"] = hours
        if timestamp is not None:
            time_options["timestamp"] = timestamp
        await self.cog.giveaway_create.callback(
            self.ctx, giveaway_item="Gold Pass", giveaway_sponsor=self.sponsor,
            giveaway_channel=self.channel, role_mention=role_mention, **time_options,
        )

    def inserted_giveaway(self) -> Giveaway:
        return self.insert.await_args.args[0]

    def assert_creation_not_started(self) -> None:
        self.ctx.defer.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.insert.assert_not_awaited()
        self.task.assert_not_called()

    def test_create_schema_uses_optional_days_hours_and_timestamp(self) -> None:
        options = {option["name"]: option for option in GiveawayCommands.giveaway_create.to_dict()["options"]}
        self.assertNotIn("giveaway_duration", options)
        self.assertEqual(options["days"]["type"], ipy.OptionType.INTEGER)
        self.assertFalse(options["days"]["required"])
        self.assertEqual(options["days"]["min_value"], 0)
        self.assertEqual(options["hours"]["type"], ipy.OptionType.INTEGER)
        self.assertFalse(options["hours"]["required"])
        self.assertEqual(options["hours"]["min_value"], 0)
        self.assertEqual(options["hours"]["max_value"], 23)
        self.assertEqual(options["timestamp"]["type"], ipy.OptionType.STRING)
        self.assertFalse(options["timestamp"]["required"])
        self.assertFalse(options["role_mention"]["required"])

    async def test_days_and_hours_insert_document_and_use_local_naive_trigger(self) -> None:
        for options in ({"days": 1, "hours": 2}, {"hours": 2}, {"days": 1}):
            with self.subTest(options=options):
                self.insert.reset_mock()
                self.channel.send.reset_mock()
                self.message.edit.reset_mock()
                self.task.reset_mock()
                expected_timestamp = (NOW + timedelta(**options)).timestamp()

                await self.create_giveaway(**options)

                giveaway = self.inserted_giveaway()
                self.assertEqual(giveaway.id, "300")
                self.assertEqual(giveaway.end_timestamp, expected_timestamp)
                self.assertEqual(giveaway.sponsor_id, "200")
                self.assertEqual(giveaway.channel_id, 400)
                self.assertEqual(giveaway.entries, [])
                self.assertFalse(giveaway.ended)
                self.assertEqual(self.channel.send.await_args.kwargs["components"], [])
                self.assertEqual(self.message.edit.await_args.kwargs["components"].custom_id, "enter_giveaway")
                self.assertIn(
                    f"<t:{int(expected_timestamp)}:R>", self.channel.send.await_args.kwargs["embed"].fields[0].value,
                )
                trigger = self.task.call_args.args[1]
                self.assertIsInstance(trigger, ipy.DateTrigger)
                self.assertEqual(trigger.target, datetime.fromtimestamp(expected_timestamp))
                self.assertEqual(trigger.next_fire(), datetime.fromtimestamp(expected_timestamp))
                self.task.return_value.start.assert_called_once_with()
                self.assertEqual(self.channel.send.await_args.args, ())

    async def test_timestamp_only_creation_overrides_default_zero_duration(self) -> None:
        for options in (
            {"timestamp": str(FUTURE_TIMESTAMP)},
            {"days": 1, "hours": 2, "timestamp": f"<t:{FUTURE_TIMESTAMP}:F>"},
        ):
            with self.subTest(options=options):
                self.insert.reset_mock()
                self.channel.send.reset_mock()
                self.task.reset_mock()
                await self.create_giveaway(**options)

                self.assertEqual(self.inserted_giveaway().end_timestamp, float(FUTURE_TIMESTAMP))
                self.assertIn(
                    f"<t:{FUTURE_TIMESTAMP}:R>",
                    self.channel.send.await_args.kwargs["embed"].fields[0].value,
                )
                trigger = self.task.call_args.args[1]
                self.assertEqual(trigger.target, datetime.fromtimestamp(FUTURE_TIMESTAMP))
                self.assertIsNone(trigger.target.tzinfo)

    async def test_raw_timestamp_overrides_duration_and_preserves_role_ping(self) -> None:
        role = MagicMock(spec=ipy.Role)
        role.mention = "<@&500>"
        await self.create_giveaway(days=1, hours=2, timestamp=str(FUTURE_TIMESTAMP), role_mention=role)
        self.assertEqual(self.inserted_giveaway().end_timestamp, float(FUTURE_TIMESTAMP))
        self.assertEqual(self.channel.send.await_args.args, (role.mention,))

    async def test_zero_duration_without_timestamp_is_rejected(self) -> None:
        await self.create_giveaway(days=0, hours=0)
        self.ctx.send.assert_awaited_once()
        self.assert_creation_not_started()

    async def test_all_time_inputs_omitted_is_rejected(self) -> None:
        await self.create_giveaway()

        self.ctx.send.assert_awaited_once()
        self.assertIn("duration must be greater than zero", self.ctx.send.await_args.args[0])
        self.assert_creation_not_started()

    async def test_malformed_timestamp_does_not_fall_back_to_duration(self) -> None:
        await self.create_giveaway(days=1, hours=2, timestamp="tomorrow")
        self.ctx.send.assert_awaited_once()
        self.assert_creation_not_started()

    async def test_non_future_timestamp_is_rejected(self) -> None:
        await self.create_giveaway(days=1, hours=2, timestamp=str(int(NOW.timestamp())))
        self.ctx.send.assert_awaited_once()
        self.assert_creation_not_started()

    async def test_out_of_range_timestamp_is_rejected(self) -> None:
        await self.create_giveaway(days=1, hours=2, timestamp="32536800000")
        self.ctx.send.assert_awaited_once()
        self.assert_creation_not_started()


class TestGiveawayRuntime(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = GiveawayCommands(self.bot)

    async def test_enter_uses_atomic_result_count_and_duplicate_state(self) -> None:
        # Given: an eligible player and a stored giveaway
        stored = giveaway_document()
        updated = giveaway_document(entries=["500"])
        player = SimpleNamespace(matches=[], elo=1000)
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 500
        ctx.message.id = 300
        ctx.message.embeds[0].fields[0].value = "Entries: 0"
        ctx.message.edit = AsyncMock()
        ctx.send = AsyncMock()
        seasons = MagicMock()
        seasons.sort.return_value = seasons
        seasons.to_list = AsyncMock(return_value=[])

        # When: the first entry succeeds and the second atomic update rejects a duplicate
        with (patch("elo_bot.extensions.giveaway.Giveaway.get", new_callable=AsyncMock, return_value=stored),
              patch.object(giveaway_module.Player, "user_id", MagicMock(), create=True),
              patch("elo_bot.extensions.giveaway.Player.find_one", new_callable=AsyncMock, return_value=player),
              patch("elo_bot.extensions.giveaway.Season.find_all", return_value=seasons),
              patch("elo_bot.extensions.giveaway.Giveaway.add_entry", new_callable=AsyncMock,
                    side_effect=[updated, None]) as add_entry):
            await self.cog.enter_giveaway.callback(ctx)
            await self.cog.enter_giveaway.callback(ctx)

        # Then: the returned document drives the count and None reports the duplicate
        self.assertEqual(add_entry.await_count, 2)
        self.assertIn("Entries: 1", ctx.message.embeds[0].fields[0].value)
        ctx.message.edit.assert_awaited_once()
        self.assertIn("already entered", ctx.send.await_args_list[1].args[0])

    async def test_end_fetches_document_and_persists_ended_state(self) -> None:
        # Given: an active giveaway with one entrant and its Discord message
        giveaway = giveaway_document(entries=["500"])
        message = MagicMock()
        message.embeds[0].title = "Giveaway"
        message.embeds[0].fields[0].value = "Ends: soon"
        message.components[0].components[0].disabled = False
        message.edit = AsyncMock()
        message.reply = AsyncMock()
        channel = MagicMock()
        channel.guild.fetch_member = AsyncMock(return_value=SimpleNamespace(id=500, display_name="Winner"))
        channel.fetch_message = AsyncMock(return_value=message)
        self.bot.fetch_channel = AsyncMock(return_value=channel)

        # When: the giveaway is ended
        with patch("elo_bot.extensions.giveaway.Giveaway.get", new_callable=AsyncMock, return_value=giveaway):
            await self.cog.end_giveaway("300")

        # Then: Discord is updated and the ended flag is persisted
        self.assertTrue(giveaway.ended)
        giveaway.claim_draw.assert_awaited_once_with(reroll=False)
        giveaway.save_changes.assert_not_awaited()
        self.bot.fetch_channel.assert_awaited_once_with(400, force=True)
        channel.fetch_message.assert_awaited_once_with("300", force=True)
        self.assertIn("<@200>", message.reply.await_args.args[0])

    async def test_end_and_reroll_commands_fetch_documents(self) -> None:
        active = giveaway_document()
        ended = giveaway_document(ended=True)
        end_ctx = MagicMock(spec=ipy.SlashContext)
        end_ctx.send = AsyncMock()
        reroll_ctx = MagicMock(spec=ipy.SlashContext)
        reroll_ctx.send = AsyncMock()

        with (patch("elo_bot.extensions.giveaway.Giveaway.get", new_callable=AsyncMock,
                    side_effect=[active, ended]),
              patch.object(self.cog, "end_giveaway", new_callable=AsyncMock) as end):
            await self.cog.giveaway_end.callback.func(self.cog, end_ctx, "300")
            await self.cog.giveaway_reroll.callback.func(self.cog, reroll_ctx, "300")

        self.assertEqual(end.await_args_list[0].args, ("300",))
        self.assertEqual(end.await_args_list[1].args, ("300",))
        self.assertEqual(end.await_args_list[1].kwargs, {"reroll": True})

    async def test_message_delete_deletes_persisted_document(self) -> None:
        giveaway = giveaway_document()
        event = SimpleNamespace(message=SimpleNamespace(id=300))
        with patch("elo_bot.extensions.giveaway.Giveaway.get", new_callable=AsyncMock, return_value=giveaway):
            await self.cog.on_message_delete.callback(self.cog, event)
        giveaway.delete.assert_awaited_once_with()

    async def test_cleanup_queries_old_rows_and_deletes_each(self) -> None:
        rows = [giveaway_document(), giveaway_document()]
        query = giveaway_query(rows)
        with (patch("elo_bot.extensions.giveaway.datetime") as clock,
              patch.object(giveaway_module.Giveaway, "end_timestamp", QueryField(), create=True),
              patch("elo_bot.extensions.giveaway.Giveaway.find", return_value=query) as find):
            clock.now.return_value = NOW.replace(tzinfo=None)
            await self.cog.finished_giveaway_delete.callback()
        find.assert_called_once()
        for row in rows:
            row.delete.assert_awaited_once_with()

    async def test_scheduler_queries_due_unended_rows(self) -> None:
        rows = [giveaway_document(), giveaway_document()]
        query = giveaway_query(rows)
        with (patch("elo_bot.extensions.giveaway.datetime") as clock,
              patch.object(giveaway_module.Giveaway, "end_timestamp", QueryField(), create=True),
              patch("elo_bot.extensions.giveaway.Giveaway.find", return_value=query) as find,
              patch.object(GiveawayCommands, "end_giveaway", new_callable=AsyncMock) as end):
            clock.now.return_value = NOW.replace(tzinfo=None)
            await self.cog.giveaways_end.callback()
        self.assertEqual(
            find.call_args.args[0],
            ("lte", NOW.replace(tzinfo=None).timestamp()),
        )
        self.assertEqual(find.call_args.args[1], {"ended": False})
        self.assertEqual([call.args for call in end.await_args_list], [("300",), ("300",)])


if __name__ == "__main__":
    unittest.main()

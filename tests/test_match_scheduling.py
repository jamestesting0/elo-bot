import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.db import Match, Participant
from elo_bot.extensions.match import MatchCommands, hosting


class TestMatchScheduling(unittest.IsolatedAsyncioTestCase):
    now = 1_800_000_000

    def setUp(self) -> None:
        self.bot = MagicMock(ext={})
        self.bot.fetch_user = AsyncMock(side_effect=lambda user_id: MagicMock(mention=f"<@{user_id}>"))
        self.bot.fetch_channel = AsyncMock()
        self.cog = MatchCommands(self.bot)
        self.match = Match.model_construct(id=PydanticObjectId(), token="TOKEN", thread="777", matched_time=1,
                                           participants=[Participant(user_id="100"), Participant(user_id="200")])
        self.enterContext(patch.object(hosting, "Match", find_one=AsyncMock(return_value=self.match)))
        self.save = self.enterContext(patch.object(Match, "set", new_callable=AsyncMock))
        self.publications = self.enterContext(patch.object(hosting, "publish_match_message", new_callable=AsyncMock))
        self.helper_tasks = self.enterContext(patch.object(hosting, "append_helper_task", new_callable=AsyncMock))
        self.enterContext(patch.object(hosting, "choose_tip", new=AsyncMock(return_value="tip")))
        self.enterContext(patch.object(hosting, "schedule_channel_name_edit"))

    def context(self, content: str | None = None, *, message_author: int = 200) -> ipy.InteractionContext:
        ctx = MagicMock(spec=ipy.SlashContext if content is None else ipy.ContextMenuContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 777
        ctx.author.id = 100
        ctx.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(id=user_id))
        if content is not None:
            ctx.target = MagicMock(spec=ipy.Message, content=content, author=MagicMock(id=message_author))
        return ctx

    async def test_slash_schedules_refuse_past_or_current_timestamps(self) -> None:
        self.match.times = [1_800_003_600, 1_800_007_200]
        for command, kwargs, timestamp in (
                (self.cog.match_schedule, {}, "1799999999"),
                (self.cog.match_war_schedule, {"player": 1}, "<t:1800000000>")):
            with self.subTest(command=command.resolved_name):
                ctx = self.context()
                self.match.is_war = "player" in kwargs
                with patch.object(hosting, "time", return_value=self.now):
                    await command.callback.func(self.cog, ctx, timestamp=timestamp, **kwargs)
                ctx.send.assert_awaited_once_with(
                    f"{ERROR_EMOJI} Enter a future Discord timestamp or Unix timestamp.", ephemeral=True,
                )
        self.save.assert_not_awaited()
        self.publications.assert_not_awaited()
        self.helper_tasks.assert_not_awaited()
        self.assertEqual(self.match.times, [1_800_003_600, 1_800_007_200])

    async def test_message_schedules_live_at_first_valid_future_tag(self) -> None:
        ctx = self.context("1800000002 <t:1799999999:F> <t:1800000003:X> <t:99999999999> "
                           "<t:1800000200:S> or <t:1800000100>", message_author=999)
        with patch.object(hosting, "time", return_value=self.now):
            await self.cog.match_schedule_message.callback.func(self.cog, ctx)
        self.save.assert_awaited_once_with({"is_war": False, "times.0": 1_800_000_200, "times.1": 1_800_000_200})
        self.assertEqual(self.match.times, [1_800_000_200, 1_800_000_200])
        self.assertIn("<t:1800000200:F>", self.publications.await_args.kwargs["embed"].fields[1].value)
        ctx.send.assert_awaited_once_with(f"{SUCCESS_EMOJI} Match scheduled for <t:1800000200:F>.", ephemeral=True)

    async def test_message_war_schedule_updates_only_the_authors_side(self) -> None:
        self.match.times = [1_800_003_600, None]
        ctx = self.context("Can play <t:1800000200>.")
        with patch.object(hosting, "time", return_value=self.now):
            await self.cog.match_war_schedule_message.callback.func(self.cog, ctx)
        self.save.assert_awaited_once_with({"is_war": True, "hosts.1": None, "times.1": 1_800_000_200})
        self.assertEqual(self.match.times, [1_800_003_600, 1_800_000_200])
        self.assertEqual(self.publications.await_args.kwargs["content"], "<@200>")
        ctx.send.assert_awaited_once_with(
            f"{SUCCESS_EMOJI} Match scheduled for <@200> at <t:1800000200:F>.", ephemeral=True,
        )

    async def test_war_message_rejects_outside_author_and_missing_future_tag(self) -> None:
        self.match.times = [1_800_003_600, 1_800_007_200]
        for author, content, error in (
                (999, "<t:1800000200>", "The message author must be a player in this match."),
                (200, "", "This message has no future Discord timestamp."),
                (200, "1800000200 or <t:1799999999>", "This message has no future Discord timestamp.")):
            with self.subTest(author=author, content=content):
                ctx = self.context(content, message_author=author)
                with patch.object(hosting, "time", return_value=self.now):
                    await self.cog.match_war_schedule_message.callback.func(self.cog, ctx)
                ctx.send.assert_awaited_once_with(f"{ERROR_EMOJI} {error}", ephemeral=True)
        self.save.assert_not_awaited()
        self.publications.assert_not_awaited()
        self.helper_tasks.assert_not_awaited()
        self.assertEqual(self.match.times, [1_800_003_600, 1_800_007_200])

    async def test_message_actions_require_scheduling_role(self) -> None:
        ctx = self.context("<t:1800000200>")
        ctx.author.has_role.return_value = False
        for command in (self.cog.match_schedule_message, self.cog.match_war_schedule_message):
            with self.subTest(command=command.resolved_name):
                with self.assertRaises(ipy.errors.CommandCheckFailure):
                    await command(ctx)
        ctx.defer.assert_not_awaited()
        self.save.assert_not_awaited()

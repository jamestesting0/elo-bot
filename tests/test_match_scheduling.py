import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.db import Match, Participant
from elo_bot.db.publication_models import MatchMessagePublication
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
        self.save.assert_awaited_once_with({"is_war": False, "times.0": 1_800_000_200, "times.1": 1_800_000_200,
                                           "scheduled_times": [1_800_000_200, 1_800_000_200]})
        self.assertEqual(self.match.times, [1_800_000_200, 1_800_000_200])
        thread_post = self.publications.await_args_list[0].kwargs
        clan, actions = [row.components for row in thread_post["components"]]
        self.assertEqual(clan[0].custom_id, "host_clan_select|TOKEN|0")
        self.assertEqual([button.label for button in actions], ["Host", "Start Match", "Abort"])
        self.assertIn("<t:1800000200:F>", self.publications.await_args.kwargs["embed"].fields[1].value)
        ctx.send.assert_awaited_once_with(f"{SUCCESS_EMOJI} Match scheduled for <t:1800000200:F>.", ephemeral=True)

    async def test_message_war_schedule_updates_only_the_authors_side(self) -> None:
        self.match.times = [1_800_003_600, None]
        ctx = self.context("Can play <t:1800000200>.")
        with patch.object(hosting, "time", return_value=self.now):
            await self.cog.match_war_schedule_message.callback.func(self.cog, ctx)
        self.save.assert_awaited_once_with({"is_war": True, "hosts.1": None, "times.1": 1_800_000_200,
                                           "scheduled_times": [None, 1_800_000_200]})
        self.assertEqual(self.match.times, [1_800_003_600, 1_800_000_200])
        self.assertEqual(self.publications.await_args.kwargs["content"], "<@200>")
        clan, actions = [row.components for row in self.publications.await_args.kwargs["components"]]
        self.assertEqual(clan[0].custom_id, "host_clan_select|TOKEN|1")
        self.assertEqual([button.label for button in actions], ["Host", "Start Match", "Abort"])
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

    def scheduled_control(self, action: str, player: int) -> ipy.ComponentContext:
        match = self.match
        match.times[player] = match.scheduled_times[player] = self.now + 100
        match.old_msgs[player] = 55
        embed = ipy.Embed(title="**Match Scheduled**", fields=[
            ipy.EmbedField(name="Time", value="Soon"), ipy.EmbedField(name="Host", value="Not assigned"),
            ipy.EmbedField(name="Host Clan", value="Not assigned"),
        ])
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.channel.id = 777
        ctx.author.id = 999
        ctx.custom_id = f"{action}|TOKEN|{player}"
        ctx.message = MagicMock(spec=ipy.Message, id=55, embeds=[embed])
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    async def test_selecting_a_scheduled_clan_saves_the_choice_without_claiming_the_resource(self) -> None:
        self.match.is_war = False
        ctx = self.scheduled_control("host_clan_select", 0)
        ctx.values = ["#CLAN"]
        published = ctx.message.embeds[0].to_dict()
        published["fields"][1]["value"] = "Previously assigned host"
        self.match.message_publications[0] = MatchMessagePublication(
            message_id=55, is_war=False, participants=("100", "200"), times=tuple(self.match.times),
            hosts=("999", None), host_clans=(None, None), embeds=[published], components=[],
        )
        self.bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(
            tag="#CLAN", name="Selected Clan", share_link="https://example.com/clan"))
        with (
            patch.object(hosting, "update_match_control", new=AsyncMock(return_value=True)) as update,
            patch.object(hosting, "claim_resource_clan", new_callable=AsyncMock) as claim,
        ):
            await self.cog.host_clan_select.callback.func(self.cog, ctx)

        self.assertEqual(update.await_args.args[2], {"scheduled_host_clans": ["#CLAN", None]})
        claim.assert_not_awaited()
        self.assertEqual(self.match.host_clans, [None, None])
        self.assertIs(self.publications.await_args.kwargs["existing_message"], ctx.message)
        fields = self.publications.await_args.kwargs["embed"].fields
        self.assertEqual(fields[1].value, "Previously assigned host")
        self.assertIn("Selected Clan", fields[2].value)

    async def test_start_match_rejects_unauthorized_unconfigured_and_old_buttons(self) -> None:
        for roles, clan, message_id, expected in (
            (False, "#CLAN", 55, "permission"),
            (True, None, 55, "Set the host clan"),
            (True, "#CLAN", 56, "latest match message"),
        ):
            with self.subTest(expected=expected):
                ctx = self.scheduled_control("scheduled_match_start", 0)
                ctx.author.has_role.return_value = roles
                ctx.message.id = message_id
                self.match.scheduled_host_clans[0] = clan
                with (
                    patch.object(self.cog, "_start_war", new_callable=AsyncMock) as start,
                    patch("elo_bot.features.matches.controls.edit_channel_message", new_callable=AsyncMock),
                ):
                    await self.cog.scheduled_match_start.callback.func(self.cog, ctx)
                start.assert_not_awaited()
                self.assertIn(expected, ctx.send.await_args.args[0])

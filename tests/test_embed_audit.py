import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from interactions.models.discord.message import process_message_payload

import cogs.match_commands as match_commands
from cogs.live_hosting import live_start
from elobot.db import Participant


class EmbedAuditFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.fetch_user = AsyncMock()
        self.bot.fetch_channel = AsyncMock()
        self.bot.coc_client = MagicMock()
        self.bot.coc_client.get_clan = AsyncMock()
        self.cog = match_commands.MatchCommands(self.bot)

    @staticmethod
    def make_match(*, is_war: bool, times: list[int | None], hosts: list[str | None],
                   host_clans: list[str | None], old_msgs: list[int | None], msg: str | None = None) -> MagicMock:
        match = MagicMock()
        match.id = PydanticObjectId("000000000000000000000001")
        match.token = "TOKEN"
        match.thread = "42"
        match.is_war = is_war
        match.times = times
        match.hosts = hosts
        match.host_clans = host_clans
        match.old_msgs = old_msgs
        match.msg = msg
        match.is_autowin = False
        match.participants = [Participant(user_id="100"), Participant(user_id="200")]
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            return_value=SimpleNamespace(matched_count=1),
        )
        match.set = AsyncMock()
        return match

    @staticmethod
    def clan(tag: str = "#NEW") -> MagicMock:
        clan = MagicMock(tag=tag, share_link="https://example.test/clan")
        clan.name = "New Clan"
        return clan

    @staticmethod
    def user(user_id: int, mention: str, *, staff: bool = False) -> MagicMock:
        user = MagicMock(id=user_id, mention=mention, display_name=f"User {user_id}")
        user.roles = [SimpleNamespace(id=match_commands.STAFF_ROLE)] if staff else []
        return user

    async def test_set_host_preserves_started_title_in_thread_and_new_public_embed(self) -> None:
        match = self.make_match(
            is_war=False, times=[1000, 1000], hosts=["901", "901"],
            host_clans=["#OLD", "#OLD"], old_msgs=[123, None],
        )
        old_message = MagicMock(
            embeds=[ipy.Embed(title="**Match Started**", description="Updating…")],
            edit=AsyncMock(),
        )
        public_channel = MagicMock(send=AsyncMock(return_value=MagicMock(id=777)))
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author = self.user(901, "<@901>")
        ctx.channel.id = 42
        ctx.channel.fetch_message = AsyncMock(return_value=old_message)
        host = self.user(999, "<@999>", staff=True)
        p1 = self.user(100, "<@100>")
        p2 = self.user(200, "<@200>")
        self.bot.fetch_user.side_effect = [p1, p2]
        self.bot.fetch_channel.return_value = public_channel
        clan = self.clan("#OLD")
        self.bot.coc_client.get_clan.return_value = clan

        with (
            patch.object(match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=match))),
            patch.object(match_commands, "choose_tip", new=AsyncMock(return_value="tip")),
            patch.object(match_commands, "schedule_channel_name_edit"),
            patch.object(match_commands, "publish_match_message", new=AsyncMock()) as published,
        ):
            await self.cog.match_set_host.callback.func(self.cog, ctx, host)

        thread_payload = published.await_args_list[0].kwargs["embed"].to_dict()
        public_payload = published.await_args_list[1].kwargs["embed"].to_dict()
        self.assertIs(published.await_args_list[1].kwargs["public_channel"], public_channel)
        self.assertIsNone(published.await_args_list[1].kwargs.get("existing_message"))
        self.assertIs(published.await_args_list[0].kwargs["existing_message"], old_message)
        self.assertEqual(thread_payload["title"], "**Match Started**")
        self.assertEqual(public_payload["title"], "**Match Started**")
        self.assertIn("**[User 999](<https://discordapp.com/users/999>)**", public_payload["description"])
        self.assertEqual(public_payload["fields"][2]["value"],
                         "[New Clan (#OLD)](https://example.test/clan)")

    async def test_set_clan_preserves_started_title_and_updates_public_clan(self) -> None:
        match = self.make_match(
            is_war=False, times=[1000, 1000], hosts=["999", "999"],
            host_clans=["#OLD", "#OLD"], old_msgs=[123, None], msg="888",
        )
        old_message = MagicMock(
            embeds=[ipy.Embed(title="**Match Started**", description="Details")],
            edit=AsyncMock(),
        )
        public_message = MagicMock(
            embeds=[ipy.Embed(
                title="**Match Started**", fields=[
                    ipy.EmbedField(name="**Players**", value="players"),
                    ipy.EmbedField(name="**Time**", value="time"),
                    ipy.EmbedField(name="**Host Clan**", value="old clan"),
                ],
            )],
            edit=AsyncMock(),
        )
        public_channel = MagicMock(fetch_message=AsyncMock(return_value=public_message))
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 42
        ctx.channel.fetch_message = AsyncMock(return_value=old_message)
        self.bot.fetch_channel.return_value = public_channel
        self.bot.coc_client.get_clan.return_value = self.clan("#NEW")

        with (
            patch.object(match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=match))),
            patch.object(match_commands, "try_reserve_resource_clan", new=AsyncMock(return_value=MagicMock())),
            patch.object(match_commands, "claim_resource_clan", new=AsyncMock(return_value=True)),
            patch.object(match_commands, "publish_match_message", new=AsyncMock()) as published,
        ):
            await self.cog.match_set_clan.callback.func(self.cog, ctx, clan="#NEW")

        thread_payload = published.await_args_list[0].kwargs["embed"].to_dict()
        public_payload = process_message_payload(embeds=published.await_args_list[1].kwargs["embed"])
        self.assertIs(published.await_args_list[1].kwargs["public_channel"], public_channel)
        self.assertIs(published.await_args_list[1].kwargs["existing_message"], public_message)
        self.assertIs(published.await_args_list[0].kwargs["existing_message"], old_message)
        self.assertEqual(thread_payload["title"], "**Match Started**")
        self.assertEqual(public_payload["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(public_payload["embeds"][0]["fields"][2]["value"],
                         "[New Clan (#NEW)](https://example.test/clan)")

    async def test_war_schedule_retains_existing_host_clan_in_thread_embed(self) -> None:
        match = self.make_match(
            is_war=True, times=[None, None], hosts=["999", None],
            host_clans=["#WAR", None], old_msgs=[None, None],
        )
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 42
        player = self.user(100, "<@100>")
        self.bot.fetch_user.return_value = player
        self.bot.coc_client.get_clan.return_value = self.clan("#WAR")
        published = AsyncMock()

        with (
            patch.object(match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=match))),
            patch.object(match_commands, "append_helper_task", new=AsyncMock()),
            patch.object(match_commands, "publish_match_message", published),
        ):
            await self.cog.match_war_schedule.callback.func(self.cog, ctx, player=0, timestamp="1700000000")

        embed = published.await_args.kwargs["embed"]
        payload = embed.to_dict()
        self.assertEqual(payload["title"], "**Match Scheduled**")
        self.assertEqual(payload["fields"][2]["value"],
                         "[New Clan (#WAR)](https://example.test/clan)")

    async def test_live_schedule_rebuilds_existing_public_embed_with_title_host_and_clan(self) -> None:
        match = self.make_match(
            is_war=False, times=[1000, 1000], hosts=["999", "999"],
            host_clans=["#LIVE", None], old_msgs=[None, None], msg="777",
        )
        public = MagicMock(
            id=80, _channel_id=90, components=[],
            embeds=[ipy.Embed(
                title="**Match Started**", description="stale", fields=[
                    ipy.EmbedField(name="**Players**", value="stale players"),
                    ipy.EmbedField(name="**Time**", value="stale time"),
                    ipy.EmbedField(name="**Host Clan**", value="stale clan"),
                ],
            )],
            edit=AsyncMock(),
        )
        channel = MagicMock(fetch_message=AsyncMock(return_value=public))
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(
            id=int(user_id), display_name=f"User {user_id}",
        ))
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 42
        host = self.user(999, "<@999>")
        self.bot.fetch_user.return_value = host
        self.bot.coc_client.get_clan.return_value = self.clan("#LIVE")
        self.bot.fetch_channel.return_value = channel

        with (
            patch.object(match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=match))),
            patch.object(match_commands, "append_helper_task", new=AsyncMock()),
            patch.object(match_commands, "publish_match_message", new=AsyncMock()) as published,
            patch.object(match_commands, "schedule_channel_name_edit"),
        ):
            await self.cog.match_schedule.callback.func(self.cog, ctx, timestamp="2000")

        self.assertIs(published.await_args.kwargs["existing_message"], public)
        embed = published.await_args.kwargs["embed"].to_dict()
        self.assertEqual(embed["title"], "**Match Rescheduled**")
        self.assertIn("**[User 999](<https://discordapp.com/users/999>)**", embed["description"])
        self.assertEqual(embed["fields"][2]["value"],
                         "[New Clan (#LIVE)](https://example.test/clan)")

    async def test_live_start_refreshes_existing_public_host_clan_field(self) -> None:
        from tests import _match_publication_fixture as publication_tests

        fixture = publication_tests.MatchPublicationState(self)
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.match.msg = "80"
        fixture.persisted["msg"] = "80"
        public = MagicMock(
            id=80, _channel_id=90, components=[],
            embeds=[ipy.Embed(
                title="**Match Started**", fields=[
                    ipy.EmbedField(name="**Players**", value="players"),
                    ipy.EmbedField(name="**Time**", value="old time"),
                    ipy.EmbedField(name="**Host Clan**", value="old clan"),
                ],
            )],
            edit=AsyncMock(),
        )
        fixture.bot.fetch_channel = AsyncMock(
            return_value=MagicMock(id=90, fetch_message=AsyncMock(return_value=public)),
        )
        request = fixture.live_request()
        with (
            patch.object(live_start.match_commands, "Match", new=MagicMock(
                find_one=AsyncMock(return_value=fixture.match),
            )),
            patch.object(live_start, "mark_store_reserved", new=AsyncMock(return_value=True)),
            patch.object(live_start.match_commands, "send_bases", new=AsyncMock()),
            patch.object(live_start.match_commands, "remove_helper_task", new=AsyncMock()),
            patch.object(live_start, "schedule_channel_name_edit"),
        ):
            await live_start.complete_live_start(fixture.bot, fixture.ctx, request)

        self.assertEqual(public.edit.await_count, 1)
        payload = process_message_payload(**public.edit.await_args.kwargs)
        public_embed = payload.get("embed", payload.get("embeds", [{}])[0])
        if hasattr(public_embed, "to_dict"):
            public_embed = public_embed.to_dict()
        self.assertEqual(public_embed["fields"][2]["value"],
                         f"[A Clan (#CLAN)]({request.start.clan.share_link})")

    async def test_existing_message_edit_failure_marks_incomplete_without_removing_controls(self) -> None:
        from tests import _match_publication_fixture as publication_tests

        fixture = publication_tests.MatchPublicationState(self)
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.match.old_msgs[0] = 43
        fixture.persisted["old_msgs.0"] = 43
        control = ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Store", custom_id="store|TOKEN|0"))
        error = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        existing = SimpleNamespace(
            id=43,
            _channel_id=42,
            components=[control],
            embeds=[ipy.Embed(title="**Match Started**", description="Old")],
            edit=AsyncMock(side_effect=[error, None]),
        )
        expected = match_commands.MatchMessageState.capture(fixture.match, 0)
        embed = ipy.Embed(title="**Match Started**", description="New")

        with self.assertRaises(ipy.errors.HTTPException):
            await match_commands.publish_match_message(
                fixture.ctx, fixture.match, expected, embed=embed,
                components=[control], existing_message=existing,
            )

        self.assertEqual(fixture.persisted["old_msgs.0"], 43)
        self.assertEqual(fixture.collection.update_one.await_count, 2)
        self.assertEqual(existing.edit.await_count, 2)
        self.assertNotIn("components", existing.edit.await_args_list[0].kwargs)
        self.assertNotIn("components", existing.edit.await_args_list[1].kwargs)
        self.assertEqual(existing.components, [control])


if __name__ == "__main__":
    unittest.main()

import asyncio
import re
import unittest
from collections import defaultdict
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from PIL import Image

import elo_bot.extensions.match.base_store as match_base_store_module
import elo_bot.extensions.match.dashboards as match_dashboards_module
import elo_bot.features.matches.resources as resources_module
from elo_bot.config.game import HOST_CLAN_CHOICES
from elo_bot.config.ids import GUILD_IDS, MAIN_GUILD
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.core.season_calendar import season_calendar
from elo_bot.db import BaseStore, ServerState
from elo_bot.discord import runtime
from elo_bot.extensions.embed import EmbedCommands
from elo_bot.features.embeds.elo_kings import PeakRating
from elo_bot.features.embeds.readme import README_SECTIONS


class TestEmbedCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        resources_module.RESOURCE_CLAN_LOCKS.clear()

    async def test_embed_branding_uses_the_running_bot_version(self):
        cog = EmbedCommands(MagicMock())
        for version, logo, color in (("main", "elo-bot.png", 0xD8A34D), ("beta", "elo-beta.png", 0x6EB793)):
            with self.subTest(version=version), patch.object(runtime, "VERSION", version, create=True):
                ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
                ctx.channel.send = AsyncMock()
                await cog.embed.callback.func(cog, ctx, "Elo Final")
                payload = ctx.channel.send.await_args.kwargs["embed"].to_dict()
                self.assertEqual(payload["color"], color)
                self.assertEqual(payload["footer"]["icon_url"],
                                 f"https://raw.githubusercontent.com/jamestesting0/elo-bot/main/assets/images/{logo}")

    async def test_embed_footer_uses_the_signed_in_bots_avatar(self):
        cog = EmbedCommands(MagicMock())
        for version, user_id, avatar in (("main", "1234", "a" * 32), ("beta", "5678", "b" * 32)):
            bot = ipy.Client()
            bot._user = ipy.ClientUser.from_dict({
                "id": user_id, "username": version, "discriminator": "0", "avatar": avatar, "verified": True,
            }, bot)
            with (self.subTest(version=version), patch.object(runtime, "bot", bot, create=True),
                  patch.object(runtime, "VERSION", version, create=True)):
                ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
                ctx.channel.send = AsyncMock()
                await cog.embed.callback.func(cog, ctx, "Elo Final")
                payload = ctx.channel.send.await_args.kwargs["embed"].to_dict()
                self.assertEqual(payload["footer"]["icon_url"],
                                 f"https://cdn.discordapp.com/avatars/{user_id}/{avatar}.png?size=64")

    async def test_elo_final_details_opens_private_guide_without_season_dates(self):
        cog = EmbedCommands(MagicMock())
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.channel.send = AsyncMock()
        await cog.embed.callback.func(cog, ctx, "Elo Final")
        posted = ctx.channel.send.await_args.kwargs
        details_button, notify_button = posted["components"][0].components
        self.assertEqual(details_button.custom_id, "elo_final_details")
        self.assertEqual(notify_button.label, "Get Notified")
        self.assertEqual(notify_button.emoji.name, "🔔")
        details = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), defer=AsyncMock())
        details.author.id = 123
        await cog.elo_final_details.callback.func(cog, details)
        reply = details.send.await_args.kwargs
        self.assertTrue(reply["ephemeral"])
        with open("assets/embeds/readme/elo_final.md") as source:
            self.assertEqual(reply["embed"].description, source.read())
        menu = reply["components"][0].components[0]
        self.assertEqual([option.value for option in menu.options if option.default], ["elo_final"])
        self.assertEqual(menu.custom_id, "readme_section|123")

    async def test_final_notification_button_toggles_the_clicking_members_role(self):
        role = 1531685536769507379
        member_id = 123
        client = ipy.Client()
        client.http.add_guild_member_role = AsyncMock()
        client.http.remove_guild_member_role = AsyncMock()
        member = ipy.Member.from_dict({"id": member_id, "guild_id": MAIN_GUILD, "roles": [456]}, client)
        cog = EmbedCommands(MagicMock())
        ctx = MagicMock(spec=ipy.ComponentContext, member=member, defer=AsyncMock(), send=AsyncMock())

        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                await cog.elo_final_notify.callback.func(cog, ctx)
                self.assertEqual(member.has_role(role), enabled)
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

        self.assertTrue(member.has_role(456))
        for write in (client.http.add_guild_member_role, client.http.remove_guild_member_role):
            write.assert_awaited_once()
            self.assertEqual(write.await_args.args, (MAIN_GUILD, member_id, role))
        ctx.edit_origin.assert_not_awaited()

        client.http.add_guild_member_role.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        await cog.elo_final_notify.callback.func(cog, ctx)
        self.assertFalse(member.has_role(role))
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_information_shows_season_dates_and_opens_private_guide(self):
        cog = EmbedCommands(MagicMock())
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.channel.send = AsyncMock()
        now = datetime(2026, 11, 19, tzinfo=UTC)
        schedule = season_calendar(now, datetime(2026, 10, 4, tzinfo=UTC), None)
        sen = "920112329344036924"
        star = "284509170412027905"
        players = MagicMock(user_id="user_id")
        players.find.return_value.project.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[PeakRating(user_id=sen, highest_elo=1702), PeakRating(user_id=star, highest_elo=1569)],
        )
        with Image.new("RGB", (96, 64), "#08dd99") as portrait, BytesIO() as buffer:
            portrait.save(buffer, format="PNG")
            avatar = MagicMock(fetch=AsyncMock(return_value=buffer.getvalue()))
        async def fetch_user(user_id):
            if user_id == sen:
                raise TimeoutError
            self.assertEqual(user_id, star)
            return MagicMock(display_avatar=avatar)

        cog.bot.fetch_user = AsyncMock(side_effect=fetch_user)
        with (patch("elo_bot.features.embeds.static.ongoing_season_calendar", new=AsyncMock(return_value=schedule)),
              patch("elo_bot.features.embeds.elo_kings.Player", new=players)):
            await cog.embed.callback.func(cog, ctx, "Elo Information")
        posted = ctx.channel.send.await_args.kwargs
        file = posted["file"]
        self.assertEqual(posted["embed"].images[0].url, f"attachment://{file.file_name}")
        self.assertIsNone(file.description)
        with Image.open(file.file) as image, Image.open("assets/finals/elo_kings_leaderboard.png") as artwork:
            self.assertEqual(image.size, (1672, 941))
            first_slot = (475, 728, 507, 760)
            self.assertEqual(image.crop(first_slot).tobytes(), artwork.convert("RGB").crop(first_slot).tobytes())
            self.assertEqual(image.getpixel((491, 788)), (8, 221, 153))
            self.assertEqual(image.getpixel((475, 772)), artwork.convert("RGB").getpixel((475, 772)))
        description = posted["embed"].description
        expected = [int(moment.timestamp()) for moment in (
            schedule.final_selection, schedule.queue_closes, schedule.season_ends)]
        self.assertEqual([int(stamp) for stamp in re.findall(r"<t:(\d+):D>", description)], expected)
        self.assertEqual([int(stamp) for stamp in re.findall(r"<t:(\d+):R>", description)], expected)
        components = posted["components"]
        self.assertEqual(len(components), 3)
        readme_button = components[2].components[0]
        self.assertEqual(readme_button.custom_id, "readme_button")
        buttons = [button for row in components for button in row.components]
        self.assertEqual([button.custom_id for button in buttons if button.style != ipy.ButtonStyle.URL],
                         ["participate_button", "support_button", "rewards_button", "build_button", "readme_button"])
        self.assertEqual(sum(button.style == ipy.ButtonStyle.URL for button in buttons), 1)
        ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock())
        ctx.author.id = 123

        await cog.readme_button.callback.func(cog, ctx)

        reply = ctx.send.await_args.kwargs
        self.assertTrue(reply["ephemeral"])
        self.assertEqual(reply["embed"].description, Path("assets/embeds/readme/getting_started.md").read_text())
        menu = reply["components"][0].components[0]
        self.assertEqual([option.value for option in menu.options], list(README_SECTIONS))
        self.assertEqual([option.value for option in menu.options if option.default], ["getting_started"])

    async def test_readme_navigation_edits_same_message_with_guide_content(self):
        cog = EmbedCommands(MagicMock())
        ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), defer=AsyncMock(), edit_origin=AsyncMock())
        ctx.author.id = 123
        ctx.custom_id = "readme_section|123"
        for section in README_SECTIONS:
            with self.subTest(section=section):
                ctx.values = [section]
                ctx.defer.reset_mock()
                await cog.readme_section.callback.func(cog, ctx)
                ctx.defer.assert_awaited_once_with(edit_origin=True)

                reply = ctx.edit_origin.await_args.kwargs
                description = reply["embed"].description
                self.assertEqual(description, Path(f"assets/embeds/readme/{section}.md").read_text())
                menu = reply["components"][0].components[0]
                self.assertEqual([option.value for option in menu.options if option.default], [section])
        ctx.send.assert_not_awaited()

    async def test_readme_navigation_rejects_another_user_and_unknown_section(self):
        cog = EmbedCommands(MagicMock())
        for user_id, section in ((456, "seasons"), (123, "../rules")):
            with self.subTest(user_id=user_id, section=section):
                ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(),
                                defer=AsyncMock(), edit_origin=AsyncMock())
                ctx.author.id = user_id
                ctx.custom_id = "readme_section|123"
                ctx.values = [section]
                await cog.readme_section.callback.func(cog, ctx)
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
                ctx.edit_origin.assert_not_awaited()
                ctx.defer.assert_not_awaited()

    async def test_clan_availability_embed_shows_slot_thread_mentions(self):
        bot = MagicMock()
        bot.file_locks = defaultdict(asyncio.Lock)
        cog = EmbedCommands(bot)

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 123
        ctx.channel.send = AsyncMock(return_value=MagicMock(id=456, edit=AsyncMock()))
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=123, display_name="Player"))

        clan = MagicMock(
            tag="#CLAN",
            availability_str="Available",
            available_slots=["Slot 2"],
        )
        clan.name = "Test [Clan]"
        free_clan = MagicMock(
            tag="#FREE",
            availability_str="Available",
            available_slots=["Slot 1", "Slot 2"],
        )
        free_clan.name = "Free Clan"
        match = MagicMock(thread="987")
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]

        with (
                patch("elo_bot.extensions.embed.Clan") as mock_clan,
                patch("elo_bot.extensions.embed.Match") as mock_match,
                patch.object(
                    ServerState,
                    "update_fields",
                    new_callable=AsyncMock,
                ) as mock_update_fields,
        ):
            mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan, free_clan])
            mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

            await cog.embed.callback.func(cog, ctx, embed_type="Clan Availability")

        mock_update_fields.assert_awaited_once_with(
            clan_availability_embed={"channel": 123, "message": 456}
        )
        message = ctx.channel.send.return_value
        payload = message.edit.await_args.kwargs["embed"]
        self.assertFalse(payload.get("fields"))
        self.assertEqual(
            payload["description"],
            "**[Test \\[Clan\\]](https://link.clashofclans.com/en?action=OpenClanProfile&tag=%23CLAN)**\n"
            "Available\nSlot 1: <#987> **[Player](<https://discordapp.com/users/123>)**\n\n"
            "**[Free Clan](https://link.clashofclans.com/en?action=OpenClanProfile&tag=%23FREE)**\nAvailable",
        )

    async def test_clan_availability_publishes_all_occupied_clans_and_slots(self):
        cog = EmbedCommands(MagicMock())
        message = MagicMock(id=456, edit=AsyncMock())
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        ctx.channel.id = 123
        ctx.channel.send = AsyncMock(return_value=message)
        ctx.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(
            id=user_id, display_name=f"Player {user_id}".ljust(32, "x"),
        ))
        clans, matches = [], []
        for index, choice in enumerate(HOST_CLAN_CHOICES):
            tag, thread = str(choice.value), str(10**18 + index)
            first, second = str(11 * 10**17 + index), str(12 * 10**17 + index)
            clan = MagicMock(tag=tag, availability_str=f"<#{thread}> <@{first}> vs <@{second}> <t:1795440000:R>")
            clan.name = str(choice.name)
            clans.append(clan)
            matches.append(MagicMock(thread=thread, participants=[
                MagicMock(user_id=user_id, base_store=BaseStore(clan_tag=tag, slot=slot, stored_at=1795440000))
                for slot, user_id in (("Slot 1", first), ("Slot 2", second))
            ]))

        with (
            patch("elo_bot.extensions.embed.Clan") as clan_model,
            patch("elo_bot.extensions.embed.Match") as match_model,
            patch.object(ServerState, "update_fields", new=AsyncMock()),
        ):
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=clans)
            match_model.find.return_value.to_list = AsyncMock(return_value=matches)
            await cog.embed.callback.func(cog, ctx, embed_type="Clan Availability")

        payload = message.edit.await_args.kwargs["embed"]
        blocks = [payload["description"], *(field["value"] for field in payload["fields"])]
        entries = [entry.splitlines() for block in blocks for entry in block.split("\n\n")]
        self.assertEqual(len(entries), len(clans))
        for (title, status, slot_one, slot_two), clan, match in zip(entries, clans, matches, strict=True):
            self.assertIn(f"%23{clan.tag[1:]}", title)
            self.assertIn(f"<#{match.thread}>", status)
            self.assertTrue(slot_one.startswith(f"Slot 1: <#{match.thread}>"))
            self.assertTrue(slot_two.startswith(f"Slot 2: <#{match.thread}>"))

    async def test_manual_free_rejects_locked_clan_with_unlocked_alternatives(self) -> None:
        # Given a manager targeting locked clan A while clan B is still available.
        bot = MagicMock()
        cog = EmbedCommands(bot)
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.has_role.return_value = True
        ctx.values = ["#A"]
        ctx.send = AsyncMock()
        clan_a = MagicMock(tag="#A", name="Alpha", availability_str="<#101> occupied")
        clan_b = MagicMock(tag="#B", availability_str="Available")
        clan_b.name = "Bravo"
        reservation = await match_base_store_module.try_reserve_resource_clan("#A")

        try:
            with patch("elo_bot.extensions.embed.Clan") as clan_model, \
                    patch.object(match_dashboards_module.Clan, "find_all") as find_all:
                clan_model.find_one = AsyncMock(return_value=clan_a)
                find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When the manager tries to free clan A.
                await cog.free_clan_select.callback.func(cog, ctx)
        finally:
            reservation.release()

        # Then the conflicting write is rejected and only unlocked clan B is listed.
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Bravo", response)
        self.assertNotIn("- Alpha", response)


    async def test_member_update_without_cached_roles_is_ignored(self):
        cog = EmbedCommands(MagicMock())
        event = MagicMock(before=ipy.MISSING)
        event.guild.id = GUILD_IDS[0]
        with patch("elo_bot.extensions.embed.Player.find_one", new=AsyncMock()) as lookup:
            await cog.on_member_update.callback(cog, event)
        lookup.assert_not_awaited()
        event.after.fetch_dm.assert_not_called()

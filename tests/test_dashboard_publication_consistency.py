import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from interactions.models.discord.message import process_message_payload
from pymongo.errors import OperationFailure

import cogs.embed_commands as commands
import cogs.match_commands as match_commands
from elobot.db import ServerState


class TestDashboardPublicationConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.cog = commands.EmbedCommands(MagicMock(ext={}))
        self.ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        self.ctx.channel.id = 100
        self.message = MagicMock(id=200, embeds=[], components=[])
        self.events = []

        async def publish(**kwargs):
            self.message.embeds = [ipy.Embed.from_dict(deepcopy(kwargs["embed"]))
                                   if isinstance(kwargs["embed"], dict) else deepcopy(kwargs["embed"])]
            self.message.components = deepcopy(kwargs.get("components", []))
            self.events.append("send")
            return self.message

        async def edit(**kwargs):
            self.message.embeds = [ipy.Embed.from_dict(deepcopy(kwargs["embed"]))
                                   if isinstance(kwargs["embed"], dict) else deepcopy(kwargs["embed"])]
            if "components" in kwargs:
                self.message.components = deepcopy(kwargs["components"])
            self.events.append("edit")

        self.ctx.channel.send = AsyncMock(side_effect=publish)
        self.message.edit = AsyncMock(side_effect=edit)
        clans = self.enterContext(patch.object(commands, "Clan"))
        clans.find_all.return_value.to_list = AsyncMock(return_value=[])
        matches = self.enterContext(patch.object(commands, "Match"))
        matches.find.return_value.to_list = AsyncMock(return_value=[])

    async def test_failed_registration_never_publishes_active_dashboard(self) -> None:
        for kind in ("Hosting Queue", "Clan Availability"):
            with self.subTest(kind=kind):
                with patch.object(ServerState, "update_fields", new=AsyncMock(
                    side_effect=OperationFailure("pointer write failed"),
                )):
                    await self.cog.embed.callback.func(self.cog, self.ctx, embed_type=kind)

                sent = self.ctx.channel.send.await_args.kwargs
                self.assertEqual(sent["embed"].title, f"**{kind}**")
                self.assertEqual(sent["embed"].description, "Loading…")
                self.assertEqual(sent["components"], [])
                self.assertEqual(self.message.embeds[0].title, f"**{kind}** `(Unavailable)`")
                self.assertEqual(self.message.components, [])
                self.assertNotIn("The embed has been sent", self.ctx.send.await_args.args[0])

    async def test_dashboard_activates_after_pointer_is_committed(self) -> None:
        async def persist(**_kwargs):
            self.events.append("commit")

        with patch.object(ServerState, "update_fields", new=AsyncMock(side_effect=persist)) as update:
            await self.cog.embed.callback.func(self.cog, self.ctx, embed_type="Clan Availability")

        self.assertEqual(self.events, ["send", "commit", "edit"])
        update.assert_awaited_once_with(clan_availability_embed={"channel": 100, "message": 200})
        self.assertEqual(self.message.embeds[0].title, "**Clan Availability**")
        self.assertTrue(self.message.components)
        self.assertIn("The embed has been sent", self.ctx.send.await_args.args[0])

    async def test_failed_activation_reports_registered_but_unavailable(self) -> None:
        edit_message = self.message.edit.side_effect

        async def fail_activation(**kwargs):
            if kwargs.get("components"):
                raise ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
            await edit_message(**kwargs)

        self.message.edit.side_effect = fail_activation
        with patch.object(ServerState, "update_fields", new=AsyncMock()) as update:
            await self.cog.embed.callback.func(self.cog, self.ctx, embed_type="Clan Availability")

        update.assert_awaited_once()
        self.assertEqual(self.message.components, [])
        self.assertIn("Unavailable", self.message.embeds[0].title)
        self.assertIn("registered", self.ctx.send.await_args.args[0])
        self.assertNotIn("The embed has been sent", self.ctx.send.await_args.args[0])

    async def test_failed_failure_edit_still_reports_failure_to_caller(self) -> None:
        self.message.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with patch.object(ServerState, "update_fields", new=AsyncMock()):
            await self.cog.embed.callback.func(self.cog, self.ctx, embed_type="Clan Availability")

        self.assertIn("could not", self.ctx.send.await_args.args[0])
        self.assertIn("announcement", self.ctx.send.await_args.args[0])
        self.assertEqual(self.ctx.send.await_count, 1)
        self.assertEqual(self.message.embeds[0].title, "**Clan Availability**")
        self.assertEqual(self.message.embeds[0].description, "Loading…")
        self.assertEqual(self.message.components, [])

    async def test_periodic_jobs_preserve_pending_and_unavailable_dashboards(self) -> None:
        channel = MagicMock(fetch_message=AsyncMock(return_value=self.message))
        bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=channel))
        cog = match_commands.MatchCommands(bot)
        state = SimpleNamespace(
            hosting_queue_embed=SimpleNamespace(channel=100, message=200),
            clan_availability_embed=SimpleNamespace(channel=100, message=200),
        )
        with (
            patch.object(ServerState, "current", new=AsyncMock(return_value=state)),
            patch.object(match_commands, "sync_base_store_slots", new=AsyncMock()) as sync,
            patch.object(match_commands, "HelperTask") as tasks,
            patch.object(match_commands, "Match") as matches,
            patch.object(match_commands, "Clan") as clans,
        ):
            tasks.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
            matches.find.return_value.to_list = AsyncMock(return_value=[])
            clans.find_all.return_value.to_list = AsyncMock(return_value=[SimpleNamespace(
                name="Clan", tag="#C", availability_str="Available", available_slots=["Slot 1", "Slot 2"],
            )])
            for marker in ("Confirming", "Unavailable", "Loading"):
                for task in (cog.update_hosting_queue_embed, cog.update_clan_availability_embed):
                    if marker in ("Confirming", "Loading") and task is cog.update_hosting_queue_embed:
                        continue
                    with self.subTest(marker=marker, callback=task.callback):
                        self.message.embeds = [ipy.Embed(
                            title="Dashboard" if marker == "Loading" else f"Dashboard`({marker})`",
                            description="Loading…" if marker == "Loading" else "Keep this status.",
                            fields=[ipy.EmbedField(name="**Clan (#C)**", value="Previous snapshot")],
                        )]
                        expected = deepcopy(self.message.embeds[0].to_dict())
                        self.message.components = []
                        self.message.edit.reset_mock()
                        await task.callback.func(cog)

                        self.assertEqual(self.message.embeds[0].to_dict(), expected)
                        self.assertEqual(self.message.components, [])
                        self.message.edit.assert_not_awaited()
            sync.assert_not_awaited()

    async def test_hosting_refresh_clears_legacy_registration_copy(self) -> None:
        self.message.embeds = [ipy.Embed(
            title="**Hosting Queue**`(Confirming)`",
            description="This dashboard is being registered. Its controls are not available yet.",
        )]
        channel = MagicMock(fetch_message=AsyncMock(return_value=self.message))
        cog = match_commands.MatchCommands(MagicMock(ext={}, fetch_channel=AsyncMock(return_value=channel)))
        state = SimpleNamespace(hosting_queue_embed=SimpleNamespace(channel=100, message=200))
        with (
            patch.object(ServerState, "current", new=AsyncMock(return_value=state)),
            patch.object(match_commands, "HelperTask") as tasks,
        ):
            tasks.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
            await cog.update_hosting_queue_embed.callback.func(cog)
        self.assertEqual(self.message.embeds[0].title, "**Hosting Queue**")
        self.assertEqual(self.message.embeds[0].description, "")
        self.message.edit.assert_awaited_once()
        payload = process_message_payload(embeds=self.message.edit.await_args.kwargs["embed"])
        self.assertEqual(payload["embeds"][0]["description"], "")


    async def test_clan_refresh_clears_stale_loading_only_after_controls_are_active(self) -> None:
        self.message.embeds = [ipy.Embed(title="**Clan Availability**", description="Loading…")]
        controls = [ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Free", custom_id="free_clan"))]
        self.message.components = controls
        channel = MagicMock(fetch_message=AsyncMock(return_value=self.message))
        cog = match_commands.MatchCommands(MagicMock(ext={}, fetch_channel=AsyncMock(return_value=channel)))
        state = SimpleNamespace(clan_availability_embed=SimpleNamespace(channel=100, message=200))
        with (
            patch.object(ServerState, "current", new=AsyncMock(return_value=state)),
            patch.object(match_commands, "sync_base_store_slots", new=AsyncMock()),
            patch.object(match_commands, "Match") as matches,
            patch.object(match_commands, "Clan") as clans,
        ):
            matches.find.return_value.to_list = AsyncMock(return_value=[])
            clans.find_all.return_value.to_list = AsyncMock(return_value=[])
            await cog.update_clan_availability_embed.callback.func(cog)
        self.message.edit.assert_awaited_once()
        self.assertFalse(self.message.embeds[0].description)
        self.assertEqual(self.message.components, controls)
        payload = process_message_payload(embeds=self.message.edit.await_args.kwargs["embed"])
        self.assertEqual(payload["embeds"][0]["description"], "")

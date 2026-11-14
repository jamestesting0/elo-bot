import io
import re
import unittest
from contextlib import redirect_stderr
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

import beta
import main
import cogs.final_commands as final_commands
import cogs.leaderboard_commands as leaderboards
from elobot.db import EloFinal, Player, PlayerSeasonData, RecordedMatch


class TestBackgroundStateConsistency(unittest.IsolatedAsyncioTestCase):
    async def test_final_nickname_update_does_not_restore_a_removed_player(self) -> None:
        for concurrent_removal in (True, False):
            with self.subTest(concurrent_removal=concurrent_removal):
                settings = DocumentSettings(name="elo_finals", use_state_management=True)
                with patch.object(EloFinal, "_document_settings", settings):
                    seed = EloFinal.model_construct(
                        id=PydanticObjectId(), season_number=1, player_nicknames={"100": "Original", "200": "Keep"},
                        schema_version=2, season_id=PydanticObjectId(), active=True, entrant_ids=["100", "200"],
                    )
                    seed._save_state()
                    row = seed.model_dump(by_alias=True)
                    cog = final_commands.FinalCommands(MagicMock(ext={}))
                    ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
                    removal_ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
                    target = MagicMock(id=100, mention="<@100>")

                    async def find(*_args, **_kwargs):
                        model = EloFinal.model_validate(deepcopy(row))
                        model._save_state()
                        return model

                    async def conditional_update(query, change):
                        if "$unset" in change:
                            for path in change["$unset"]:
                                field, key = path.split(".")
                                row[field].pop(key, None)
                            for field, value in change["$pull"].items():
                                row[field] = [item for item in row[field] if item != value]
                            for field, value in change["$inc"].items():
                                row[field] += value
                            return SimpleNamespace(matched_count=1)
                        if concurrent_removal:
                            await cog.final_player_remove.callback.func(cog, removal_ctx, target)
                        exists = "100" in row["player_nicknames"]
                        if query.get("player_nicknames.100") == {"$exists": True} and not exists:
                            return SimpleNamespace(matched_count=0)
                        row["player_nicknames"]["100"] = change["$set"]["player_nicknames.100"]
                        return SimpleNamespace(matched_count=1)

                    collection = MagicMock(
                        update_one=AsyncMock(side_effect=conditional_update),
                    )
                    with (
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection),
                        patch.object(cog, "_edition", new=AsyncMock(side_effect=find)),
                    ):
                        await cog.final_player_nick.callback.func(cog, ctx, target, "Changed")

                    if concurrent_removal:
                        self.assertNotIn("100", row["player_nicknames"])
                        self.assertNotIn("100", row["entrant_ids"])
                        self.assertNotIn("Updated", ctx.send.await_args.args[0])
                        self.assertIn("Removed", removal_ctx.send.await_args.args[0])
                    else:
                        self.assertEqual(row["player_nicknames"]["100"], "Changed")
                        self.assertIn("Updated", ctx.send.await_args.args[0])
                    self.assertEqual(row["player_nicknames"]["200"], "Keep")

    async def test_final_player_removal_reaches_persisted_dictionary(self) -> None:
        settings = DocumentSettings(name="elo_finals", use_state_management=True)
        with patch.object(EloFinal, "_document_settings", settings):
            model = EloFinal.model_construct(
                id=PydanticObjectId(), season_number=1, player_nicknames={"100": "Remove", "200": "Keep"},
                schema_version=2, season_id=PydanticObjectId(), active=True, entrant_ids=["100", "200"],
            )
            model._save_state()
            persisted = model.model_dump(by_alias=True)
            updates = []

            async def update(_query, change, **_kwargs):
                updates.append(deepcopy(change))
                for path, value in change.get("$set", {}).items():
                    if path.startswith("player_nicknames."):
                        persisted["player_nicknames"][path.split(".")[1]] = value
                for path in change.get("$unset", {}):
                    field, key = path.split(".")
                    persisted[field].pop(key, None)
                for field, value in change.get("$pull", {}).items():
                    persisted[field] = [item for item in persisted[field] if item != value]
                for field, value in change.get("$inc", {}).items():
                    persisted[field] += value
                return SimpleNamespace(matched_count=1)

            collection = MagicMock(update_one=AsyncMock(side_effect=update))
            cog = final_commands.FinalCommands(MagicMock(ext={}))
            ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
            ctx.author.has_role.return_value = True
            with (
                patch.object(EloFinal, "get_pymongo_collection", return_value=collection),
                patch.object(cog, "_edition", new=AsyncMock(return_value=model)),
            ):
                await cog.final_player_remove.callback.func(cog, ctx, MagicMock(id=100, mention="<@100>"))

            self.assertEqual(persisted["player_nicknames"], {"200": "Keep"})
            self.assertEqual(updates, [{"$unset": {"player_nicknames.100": "", "seeds.100": "", "archived_elo.100": ""},
                                       "$pull": {"entrant_ids": "100", "byes": "100"},
                                       "$inc": {"revision": 1, "setup_revision": 1}}])
            self.assertEqual(persisted["entrant_ids"], ["200"])
            self.assertEqual(persisted["setup_revision"], 1)
            ctx.send.assert_awaited_once()

    async def test_bug_report_acknowledgement_waits_for_owner_delivery(self) -> None:
        for module in (main, beta):
            with self.subTest(module=module.__name__):
                ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), kwargs={}, custom_id="test")
                response = MagicMock(send=AsyncMock(), defer=AsyncMock())
                error = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
                bot = MagicMock(wait_for_component=AsyncMock(return_value=SimpleNamespace(ctx=response)))
                bot.owner.send = AsyncMock(side_effect=error)
                with patch.object(module, "bot", bot), redirect_stderr(io.StringIO()):
                    with self.assertRaises(ipy.errors.Forbidden):
                        await module.report_error(ctx, RuntimeError("synthetic failure"))

                bot.owner.send.assert_awaited_once()
                response.send.assert_not_awaited()

    async def test_failed_bug_response_keeps_retry_control(self) -> None:
        for module in (main, beta):
            with self.subTest(module=module.__name__):
                button = ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Respond", custom_id="respond")
                ctx = MagicMock(spec=ipy.ModalContext, defer=AsyncMock(), send=AsyncMock())
                ctx.message.embeds = [ipy.Embed(fields=[ipy.EmbedField(name="Details", value="Test")])]
                ctx.message.components = [ipy.ActionRow(button)]
                ctx.message.edit = AsyncMock()
                error = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
                user = MagicMock(send=AsyncMock(side_effect=error))
                bot = MagicMock(fetch_user=AsyncMock(return_value=user), owners=[])
                with patch.object(module, "bot", bot):
                    await module.bug_respond_modal.callback(ctx, **{"100": "A response"})

                user.send.assert_awaited_once()
                self.assertFalse(button.disabled)
                ctx.message.edit.assert_not_awaited()

    async def test_live_elo_refresh_keeps_historical_snapshot(self) -> None:
        cog = leaderboards.LeaderboardCommands(MagicMock(ext={}))
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=100, display_name="Player"))
        ctx.message.edit = AsyncMock()
        ctx.message.embeds = [ipy.Embed(
            title="**Elo Leaderboard**", fields=[ipy.EmbedField(name="Scope", value="Season 1")],
        )]
        snapshot = SimpleNamespace(season_number=1, start=100, end=200, players=[PlayerSeasonData(player_id="100", elo=1100)])
        current = Player.model_construct(id="100", user_id="100", tag="#P", elo=1500, highest_elo=1500)
        with patch.object(leaderboards, "Season") as seasons, patch.object(leaderboards, "Player") as players:
            seasons.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[snapshot])
            players.find_all.return_value.to_list = AsyncMock(return_value=[current])
            players.side_effect = lambda **fields: Player.model_construct(**fields)
            await cog.lb_refresh.callback.func(cog, ctx)

        embed = ctx.message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual(embed.fields[0].value, "Season 1")
        self.assertEqual(re.findall(r"`(\d+)`", embed.description), ["1100"])

    async def test_live_addiction_refresh_keeps_all_seasons_scope(self) -> None:
        cog = leaderboards.LeaderboardCommands(MagicMock(ext={}))
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=100, display_name="Player"))
        ctx.message.edit = AsyncMock()
        ctx.message.embeds = [ipy.Embed(
            title="**Addiction Leaderboard**", fields=[ipy.EmbedField(name="Scope", value="All Seasons")],
        )]
        attack = SimpleNamespace(districts={}, won=True, stars=0, destruction=0)
        player = Player.model_construct(
            id="100", user_id="100", tag="#P", elo=1500, highest_elo=1500,
            matches=[
                RecordedMatch.model_construct(id="old", time=150, players={"100": attack}),
                RecordedMatch.model_construct(id="new", time=350, players={"100": attack}),
            ],
        )
        cog._player_cache = [player]
        with patch.object(leaderboards, "Season") as seasons, patch.object(leaderboards, "Player") as players:
            seasons.find_all.return_value.sort.return_value.to_list = AsyncMock(
                return_value=[SimpleNamespace(season_number=1, start=0, end=200)]
            )
            players.find_all.return_value.to_list = AsyncMock(return_value=[player])
            await cog.lb_refresh.callback.func(cog, ctx)

        players.find_all.assert_not_called()

        embed = ctx.message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual(embed.fields[0].value, "All Seasons")
        self.assertEqual(re.findall(r"`(\d+)`", embed.description), ["2"])

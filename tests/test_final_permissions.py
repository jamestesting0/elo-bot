import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.app import errors
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, LVL1_ROLES, MAIN_GUILD, STAFF_ROLE
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import EloFinal, EloFinalMatch, Match, Participant
from elo_bot.db.final_models import FinalMatchResult, FinalResultSnapshot
from elo_bot.extensions.final import FinalCommands
from elo_bot.extensions.match import MatchCommands


class TestFinalPermissions(unittest.IsolatedAsyncioTestCase):
    def context(self, *, manager=False):
        ctx = MagicMock(guild_id=MAIN_GUILD, send=AsyncMock(), send_modal=AsyncMock(), kwargs={})
        ctx.author.id = 9
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES if manager else role == STAFF_ROLE
        return ctx

    async def test_every_manager_command_rejects_staff_through_the_global_handler(self):
        cog = FinalCommands(MagicMock(ext={}))
        public = {"final bracket view", "final predict", "final predictions view", "final profile", "final matches",
                  "final dashboard"}
        commands = [command for _, command in inspect.getmembers(cog)
                    if isinstance(command, ipy.SlashCommand) and command.sub_cmd_name
                    and command.resolved_name not in public]
        for command in commands:
            with self.subTest(command=command.resolved_name):
                ctx = self.context()
                self.assertTrue(command.checks)
                self.assertFalse(any([await check(ctx) for check in command.checks]))
                with self.assertRaises(ipy.errors.CommandCheckFailure) as denied:
                    await command(ctx)
                await errors.on_command_error.callback(SimpleNamespace(ctx=ctx, error=denied.exception))
                self.assertEqual(ctx.send.await_args.args[0],
                    f"{ERROR_EMOJI} You do not have permission to run this command!")
                ctx.send_modal.assert_not_awaited()
                manager = self.context(manager=True)
                self.assertTrue(all([await check(manager) for check in command.checks]))

    async def test_pairing_control_uses_the_standard_component_permission_failure(self):
        cog = FinalCommands(MagicMock(ext={}))
        ctx = self.context()
        with self.assertRaises(ipy.errors.CommandCheckFailure) as denied:
            await cog.final_pairing_action(ctx)
        await errors.on_component_error.callback(SimpleNamespace(ctx=ctx, error=denied.exception))
        self.assertEqual(ctx.send.await_args.args[0],
            f"{ERROR_EMOJI} You do not have permission to interact with this component!")
        ctx.send_modal.assert_not_awaited()

    async def test_result_entry_rejects_staff_corrections_before_any_modal(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), guild_id=MAIN_GUILD, season_number=7,
            player_nicknames={"1": "One", "2": "Two"}, nodes={
                "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], result_id="prior", state="DECIDED")})
        source = Match.model_construct(format="elo_final_seven", final_id=str(final.id), final_node_id="QF1",
            participants=[Participant(user_id=user, format="elo_final_seven", districts=list(FINAL_DISTRICTS), bases=[None] * 7, result=[None] * 7) for user in ("1", "2")])
        cog = MatchCommands(MagicMock(ext={}, fetch_user=AsyncMock(return_value=MagicMock())))
        previous = FinalMatchResult.model_construct(snapshot=FinalResultSnapshot(contestants=["1", "2"],
            winner="1", scores={"1": ["3.100"] * 7, "2": ["2.80"] * 7}))
        for callback in ("result_submit_button", "match_resubmit", "result_store_button"):
            with self.subTest(callback=callback):
                ctx = self.context()
                ctx.custom_id = "match_result_store|token|0" if callback == "result_store_button" else "submit_button|token"
                with patch("elo_bot.extensions.match.results.Match", MagicMock(find_one=AsyncMock(return_value=source))), patch(
                        "elo_bot.extensions.match.result_store.Match", MagicMock(find_one=AsyncMock(return_value=source))), patch(
                        "elo_bot.features.finals.result_ui.require_ongoing_match", new=AsyncMock(return_value=final)), patch.object(FinalMatchResult, "get", new=AsyncMock(return_value=previous)), patch(
                        "elo_bot.extensions.match.result_store.check_match_control", new=AsyncMock(return_value=True)), patch(
                        "elo_bot.extensions.match.result_store.open_run_refusal", new=AsyncMock(return_value=None)):
                    await getattr(cog, callback).callback.func(cog, ctx)
                ctx.send_modal.assert_not_awaited()
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
                self.assertIn("`/match resubmit`", ctx.send.await_args.args[0])
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_initial_staff_and_manager_correction_forms_have_the_current_identity(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), guild_id=MAIN_GUILD, season_number=7,
            player_nicknames={"1": "One", "2": "Two"}, nodes={
                "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"])})
        source = Match.model_construct(format="elo_final_seven", final_id=str(final.id), final_node_id="QF1",
            participants=[Participant(user_id=user, format="elo_final_seven", districts=list(FINAL_DISTRICTS), bases=[None] * 7, result=[None] * 7) for user in ("1", "2")])
        cog = MatchCommands(MagicMock(ext={}, fetch_user=AsyncMock(return_value=MagicMock())))
        previous = FinalMatchResult.model_construct(snapshot=FinalResultSnapshot(
            contestants=["1", "2"], winner="1", scores={"1": ["3.100"] * 7, "2": ["2.80"] * 7}))
        for correction in (False, True):
            with self.subTest(correction=correction):
                final.nodes["QF1"].result_id = "prior" if correction else None
                ctx = self.context(manager=correction)
                with patch("elo_bot.extensions.match.results.Match", MagicMock(find_one=AsyncMock(return_value=source))), patch(
                        "elo_bot.extensions.match.result_store.Match", MagicMock(find_one=AsyncMock(return_value=source))), patch(
                        "elo_bot.features.finals.result_ui.require_ongoing_match", new=AsyncMock(return_value=final)), patch.object(
                        FinalMatchResult, "get", new=AsyncMock(return_value=previous)):
                    await cog.match_resubmit.callback.func(cog, ctx)
                modal = ctx.send_modal.await_args.args[0]
                self.assertTrue(modal.custom_id.startswith(f"finalres|{final.id}|QF1|9|"))
                self.assertEqual(len(modal.components), 3 if correction else 2)
                if correction:
                    self.assertEqual(modal.components[0].value.splitlines(), ["3.100"] * 7)
                ctx.send.assert_not_awaited()

    async def test_member_check_rejects_blacklist_and_cross_server_controls_without_lookup(self):
        cog = FinalCommands(MagicMock(ext={}))
        for blacklisted in (False, True):
            ctx = self.context()
            ctx.guild_id = MAIN_GUILD if blacklisted else 1
            ctx.author.has_role.side_effect = lambda role: blacklisted and role == ELO_BLACKLIST_ROLE
            cog._edition = AsyncMock()
            await cog.final_dashboard.callback.func(cog, ctx)
            cog._edition.assert_not_awaited()
            self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
            self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

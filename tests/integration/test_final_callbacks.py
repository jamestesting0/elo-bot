from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from cogs.final_commands import FinalCommands
from cogs.leaderboard_commands import LeaderboardCommands, construct_leaderboard_string, _MATCH_LEADERBOARD_TYPES
from cogs.token_commands import TokenCommands
from common.constants import MAIN_GUILD, LVL1_ROLES, LVL2_ROLES, LB_ENTRIES_PER_PAGE, ERROR_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from common.finals.lifecycle import confirm_bracket
from common.finals.predictions import change_prediction_state
from common.tokens.bets import open_market
from database_models.final_models import FinalPrediction
from database_models.token_models import FinalBet, TokenWallet
from database_models import Player
from database_models.final_models import FinalMatchResult


class TestFinalCallbacks(FinalScenarioCase):
    def context(self, user=9, *, manager=False):
        ctx = MagicMock(guild_id=MAIN_GUILD, id=100, send=AsyncMock(), defer=AsyncMock(), edit_origin=AsyncMock(),
                        send_modal=AsyncMock(), values=[])
        ctx.author.id = user
        ctx.author.has_role.side_effect = lambda role: manager and role in LVL1_ROLES
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        return ctx

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot = MagicMock(ext={})
        self.cog = FinalCommands(self.bot)
        self.final = await confirm_bracket(await self.roster(8), "manager")

    def library_context(self, kind, custom_id=None, values=None):
        client = MagicMock()
        client.app.id = 42
        member = MagicMock(id=9)
        member.has_role.return_value = False
        client.cache.get_member.return_value = member
        client.cache.get_user.return_value = member
        client.cache.place_user_data.return_value = member
        client.cache.get_guild.return_value.fetch_member = AsyncMock(return_value=None)
        message = MagicMock(id=9001, flags=ipy.MessageFlags.IS_COMPONENTS_V2 | ipy.MessageFlags.EPHEMERAL)
        client.cache.place_message_data.return_value = message
        client.cache.get_message.return_value = message
        client.http.post_initial_response = AsyncMock(return_value=None)
        client.http.edit_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.get_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.post_followup = AsyncMock(return_value={"id": "9002"})
        payload = {"id": "1750000000000000000", "application_id": "42", "type": kind, "token": "test-only",
            "version": 1, "locale": "en-US", "guild_id": str(MAIN_GUILD), "channel_id": "77",
            "user": {"id": "9", "username": "Bettor", "discriminator": "0", "avatar": None},
            "data": {"id": "123", "name": "final", "type": 1}}
        if custom_id:
            payload["data"] = {"custom_id": custom_id, "component_type": 3, "values": values or []}
            payload["message"] = {"id": "9001"}
        context_type = ipy.ComponentContext if custom_id else ipy.SlashContext
        return context_type.from_dict(client, payload), client

    async def test_prediction_uses_pinned_library_ephemeral_edit_original_payload(self):
        await change_prediction_state(self.final, True)
        ctx, client = self.library_context(int(ipy.InteractionType.APPLICATION_COMMAND))
        await self.cog.final_predict.callback.func(self.cog, ctx)
        initial = client.http.post_initial_response.await_args.args[0]
        self.assertEqual(initial["type"], 5)
        self.assertEqual(initial["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        payload = client.http.edit_interaction_message.await_args.args[0]
        self.assertTrue(payload["flags"] & ipy.MessageFlags.IS_COMPONENTS_V2)
        self.assertFalse(payload.get("content"))
        self.assertFalse(payload.get("embeds"))
        self.assertEqual(payload["components"][0]["type"], 17)
        client.http.post_followup.assert_not_awaited()
        prediction = await FinalPrediction.find_one({"user_id": "9"})
        component, transport = self.library_context(int(ipy.InteractionType.MESSAGE_COMPONENT),
            f"finalpred|7|9|{prediction.session_revision}|pick|QF1", [self.final.nodes["QF1"].contestants[0]])
        await self.cog.final_prediction_action.callback.func(self.cog, component)
        self.assertEqual(transport.http.post_initial_response.await_args.args[0]["type"], 6)
        edited = transport.http.edit_interaction_message.await_args.args[0]
        self.assertTrue(edited["flags"] & ipy.MessageFlags.IS_COMPONENTS_V2)
        self.assertEqual((await FinalPrediction.get(prediction.id)).draft["QF1"], "1")

    async def test_betting_review_confirm_stale_and_owner_controls_match_persisted_state(self):
        market = await open_market(self.final, "QF1")
        ctx = self.context()
        ctx.guild.fetch_member.return_value = MagicMock(id=1, display_name="Player 1")
        ctx.custom_id = f"betamount|{market.id}|9|1|0"
        await self.cog.bet_amount.callback.func(self.cog, ctx, "1000")
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.description, f"**[Player 1](<https://discordapp.com/users/1>)** · 1,000 {TOKEN_EMOJI}")
        self.assertEqual([(field.name, field.value) for field in embed.fields],
                         [("**Estimated Profit**", f"0 {TOKEN_EMOJI}")])
        button = ctx.send.await_args.kwargs["components"][0]
        self.assertLessEqual(len(button.custom_id), 100)
        self.assertIsNone(await FinalBet.find_one({"user_id": "9"}))
        ctx.custom_id = button.custom_id
        await self.cog.bet_confirm.callback.func(self.cog, ctx)
        self.assertEqual((await TokenWallet.get("9")).balance, 4000)
        self.assertEqual((await FinalBet.find_one({"user_id": "9"})).amount, 1000)
        self.assertIn(f"{SUCCESS_EMOJI} Bet updated: 1,000 {TOKEN_EMOJI} on <@1>.",
                      ctx.edit_origin.await_args.kwargs["content"])
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"], [])
        self.assertEqual(ctx.edit_origin.await_args.kwargs["embeds"], [])
        self.assertEqual(ctx.defer.await_args.kwargs, {"edit_origin": True})
        ctx.id = 101
        await self.cog.bet_confirm.callback.func(self.cog, ctx)
        self.assertIn("bet changed", ctx.edit_origin.await_args.kwargs["content"])
        self.assertTrue(ctx.edit_origin.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"][0].label, "Open Bet")
        self.assertEqual((await TokenWallet.get("9")).balance, 4000)
        foreign = self.context(user=8)
        foreign.custom_id = button.custom_id
        await self.cog.bet_confirm.callback.func(self.cog, foreign)
        self.assertIn("another user", foreign.send.await_args.args[0])
        self.assertTrue(foreign.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        foreign.edit_origin.assert_not_awaited()
        cancel = self.context()
        cancel.id = 102
        cancel.custom_id = f"betconfirm|{market.id}|9|1|1|0"
        await self.cog.bet_confirm.callback.func(self.cog, cancel)
        self.assertIn(f"{SUCCESS_EMOJI} Bet cancelled. 1,000 {TOKEN_EMOJI} returned.",
                      cancel.edit_origin.await_args.kwargs["content"])
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertFalse((await FinalBet.find_one({"user_id": "9"})).active)

    async def test_bet_receipt_replaces_review_with_pinned_library_payload(self):
        market = await open_market(self.final, "QF1")
        component, transport = self.library_context(int(ipy.InteractionType.MESSAGE_COMPONENT),
            f"betconfirm|{market.id}|9|1|0|1000")
        component.message.flags = ipy.MessageFlags.EPHEMERAL
        await self.cog.bet_confirm.callback.func(self.cog, component)
        self.assertEqual(transport.http.post_initial_response.await_args.args[0]["type"], 6)
        payload = transport.http.edit_interaction_message.await_args.args[0]
        self.assertIn("Season 7 Final · QF1", payload["content"])
        self.assertIn(f"Available Tokens: 4,000 {TOKEN_EMOJI}", payload["content"])
        self.assertIn(f"{SUCCESS_EMOJI} Bet updated: 1,000 {TOKEN_EMOJI}", payload["content"])
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["embeds"], [])
        transport.http.post_followup.assert_not_awaited()
        self.assertEqual((await TokenWallet.get("9")).balance, 4000)
        self.assertEqual((await FinalBet.find_one({"user_id": "9"})).amount, 1000)

    async def test_profile_tokens_and_leaderboard_use_the_same_committed_balance(self):
        ctx = self.context()
        await self.cog.final_profile.callback.func(self.cog, ctx)
        fields = ctx.send.await_args.kwargs["embed"].fields
        self.assertIn(f"**Available:** 5,000 {TOKEN_EMOJI}",
                      next(field.value for field in fields if field.name == "__**Tokens**__"))
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        await TokenWallet.get_pymongo_collection().update_one({"_id": "9"}, {"$set": {"balance": -100}})
        ctx.send.reset_mock()
        await self.cog.final_profile.callback.func(self.cog, ctx)
        tokens = next(field.value for field in ctx.send.await_args.kwargs["embed"].fields
                      if field.name == "__**Tokens**__")
        self.assertIn(f"**Available:** 0 {TOKEN_EMOJI}", tokens)
        self.assertIn(f"**Debt:** 100 {TOKEN_EMOJI}", tokens)
        wallet = TokenCommands(self.bot)
        await wallet.tokens_balance.callback.func(wallet, ctx)
        self.assertEqual([(field.name, field.value) for field in ctx.send.await_args.kwargs["embed"].fields],
                         [("**Available**", f"0 {TOKEN_EMOJI}"),
                          ("**Committed**", f"0 {TOKEN_EMOJI}"), ("**Debt**", f"100 {TOKEN_EMOJI}")])
        leaderboard = LeaderboardCommands(self.bot)
        rows, total, paged = await leaderboard._board_rows("Tokens", "All Seasons", 0, 0, 0, [])
        self.assertEqual(total, 9)
        text = await construct_leaderboard_string(ctx.guild, rows, "Tokens", 0, 0, paged=paged)
        self.assertIn(f"Debt: 100 {TOKEN_EMOJI}", text)
        self.assertNotIn("Tokens", _MATCH_LEADERBOARD_TYPES)
        self.assertNotIn("Predictions", _MATCH_LEADERBOARD_TYPES)

    async def test_token_and_prediction_leaderboard_navigation_refresh_keeps_scope_and_owner(self):
        for user in range(10, LB_ENTRIES_PER_PAGE + 3):
            await Player(id=str(user), user_id=str(user), tag=f"#P{user}", elo=2000, highest_elo=2100).insert()
        board = LeaderboardCommands(self.bot)
        ctx = self.context()
        ctx.edit = AsyncMock()
        await board.leaderboard.callback.func(board, ctx, lb_type="Tokens", hidden=True)
        payload = ctx.send.await_args.kwargs
        message = MagicMock(embeds=[payload["embed"]], components=payload["components"], edit=AsyncMock())
        message.interaction.user.id = 9
        ctx.message = message
        self.assertEqual(message.embeds[0].fields[0].value, "Current Season")
        self.assertEqual([option.value for option in message.components[0].components[0].options if option.default], ["Tokens"])
        ctx.custom_id = f"lb_right|{LB_ENTRIES_PER_PAGE}"
        await board.lb_pagination.callback.func(board, ctx)
        second_page = message.embeds[0].description
        self.assertEqual(message.components[-1].components[0].disabled, False)
        self.assertTrue(message.components[-1].components[-1].disabled)
        await board.lb_refresh.callback.func(board, ctx)
        self.assertIn(second_page, message.embeds[0].description)
        self.assertEqual(message.embeds[0].fields[0].value, "Current Season")
        await board.lb_page_modal.callback.func(board, ctx, page="1")
        self.assertTrue(message.components[-1].components[0].disabled)
        await board.lb_page_button.callback.func(board, ctx)
        self.assertEqual(ctx.send_modal.await_args.args[0].custom_id, "lb_page_modal")
        await FinalPrediction(id=f"{self.final.id}:9", final_id=str(self.final.id), season_number=7, user_id="9",
                              setup_revision=self.final.setup_revision, submitted_at=1, points=2, eligible=3).insert()
        ctx.values = ["Predictions"]
        await board.lb_type_select.callback.func(board, ctx)
        message.components = ctx.edit.await_args.kwargs["components"]
        self.assertEqual(message.embeds[0].fields[0].value, "Season 7 Final")
        self.assertEqual([option.value for option in message.components[1].components[0].options if option.default], ["7"])
        ctx.values = ["7"]
        await board.lb_season_select.callback.func(board, ctx)
        await board.lb_refresh.callback.func(board, ctx)
        self.assertIn("2/3", message.embeds[0].description)
        self.assertIn("66.67%", message.embeds[0].description)
        self.assertEqual(message.embeds[0].fields[0].value, "Season 7 Final")
        ctx.author.id = 8
        before = ctx.edit.await_count
        await board.lb_season_select.callback.func(board, ctx)
        self.assertEqual(ctx.edit.await_count, before)
        self.assertIn("cannot interfere", ctx.send.await_args.args[0])

    async def test_official_result_callback_reports_saved_state_when_discord_publication_fails(self):
        ctx = self.context(manager=True)
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES or role in LVL2_ROLES
        node = self.final.nodes["QF1"]
        ctx.custom_id = f"finalres|7|QF1|9|0|{node.contestant_revision}|0|-|0"
        self.bot.fetch_channel = AsyncMock(side_effect=ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden")))
        before = [player.model_dump() for player in await Player.find_all().to_list()]
        confirmations = []

        async def confirm(**kwargs):
            self.assertEqual(await FinalMatchResult.count(), 0)
            self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
            self.assertEqual(ctx.send.await_args.args[0], "Confirm this result?")
            self.assertIn("700%", ctx.send.await_args.kwargs["embed"].fields[0].value)
            confirmation = self.context(manager=True)
            confirmation.custom_id = "finalconfirm|100"
            confirmations.append(confirmation)
            return MagicMock(ctx=confirmation)

        self.bot.wait_for_component = AsyncMock(side_effect=confirm)
        await self.cog.final_result_modal.callback.func(self.cog, ctx, player1="\n".join(["3.100"] * 7),
                                                      player2="\n".join(["2.80"] * 7))
        confirmation = self.bot.wait_for_component.await_args.kwargs
        self.assertFalse(confirmation["check"](MagicMock(ctx=self.context(user=8))))
        response = confirmations[0].edit_origin.await_args.kwargs
        self.assertTrue(response["content"].startswith(f"{SUCCESS_EMOJI} Result saved."))
        self.assertIn("publication remains pending", response["content"])
        self.assertEqual(response["components"], [])
        self.assertEqual(ctx.send.await_count, 1)
        self.assertEqual(await FinalMatchResult.count(), 1)
        self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])

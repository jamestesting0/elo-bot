import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.extensions.final import FinalCommands
from elo_bot.extensions.match import MatchCommands
from elo_bot.extensions.tokens import TokenCommands
from elo_bot.config.ids import FINAL_CHANNEL, LVL1_ROLES, LVL2_ROLES
from elo_bot.config.game import DISTRICT_CODES
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI, TOKEN_EMOJI
from elo_bot.features.finals.lifecycle import confirm_bracket, resolve_final
from elo_bot.features.finals.matches import bind_thread, prepare_match, schedule_final_match
from elo_bot.features.finals.profile_ui import profile_view
from elo_bot.features.finals.pairing_ui import pairing_view
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.features.finals.leaderboards import FinalBoardRow, final_board_text
from elo_bot.features.tokens.bets import BetAction, apply_bet, open_market, void_market
from elo_bot.features.tokens.presentation import bet_review, market_controls, market_embed, publish_market
from elo_bot.db import EloFinal, Match, Player
from elo_bot.db.final_models import FinalMatchResult, FinalPlayerProfile
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet


class TestFinalUIConsistency(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(await self.roster(8), "manager")
        source = await prepare_match(self.final, "QF1")
        source = await schedule_final_match(source, 2000000000)
        await source.set({"thread": "777"})
        await bind_thread(self.final, source, "777")
        self.final = await resolve_final()
        self.bot = MagicMock(ext={}, user=SimpleNamespace(id=42))
        self.cog = FinalCommands(self.bot)
        self.match_cog = MatchCommands(self.bot)
        self.ctx = self.context()
        node = self.final.nodes["QF1"]
        self.ctx.custom_id = f"finalres|7|QF1|9|0|{node.contestant_revision}|{node.attempt}|-|0"
        self.confirmation = self.context()
        self.confirmation.custom_id = "finalconfirm|100"
        self.message = SimpleNamespace(id=123)
        self.ctx.send.return_value = self.message
        message = MagicMock(id=888, pin=AsyncMock(), edit=AsyncMock())
        channel = MagicMock(send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
        channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot.fetch_channel = AsyncMock(return_value=channel)

    def context(self, user=9):
        ctx = MagicMock(id=100, guild_id=self.final.guild_id, channel=MagicMock(id=self.final.channel_id),
            send=AsyncMock(), defer=AsyncMock(), edit=AsyncMock(), edit_origin=AsyncMock(), send_modal=AsyncMock())
        ctx.author.id = user
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        ctx.author.mention = f"<@{user}>"
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES or role in LVL2_ROLES
        return ctx

    async def submit(self):
        await self.cog.final_result_modal.callback.func(self.cog, self.ctx,
            player1="\n".join(["3.100"] * 7), player2="\n".join(["2.80"] * 7))

    async def test_preview_is_ephemeral_and_confirmation_commits_once_before_success(self):
        before = [player.model_dump() for player in await Player.find_all().to_list()]

        async def confirm(**kwargs):
            self.assertEqual(await FinalMatchResult.count(), 0)
            self.assertEqual((await resolve_final()).nodes["QF1"].result_version, 0)
            preview = self.ctx.send.await_args
            self.assertTrue(preview.kwargs["ephemeral"])
            self.assertEqual([button.label for button in preview.kwargs["components"].components], ["Cancel", "Confirm"])
            self.assertEqual(len(preview.kwargs["embed"].description.split("**")), 19)
            self.assertNotIn("Revision", [field.name.strip("*_") for field in preview.kwargs["embed"].fields])
            self.assertTrue(kwargs["check"](SimpleNamespace(ctx=self.confirmation)))
            self.assertFalse(kwargs["check"](SimpleNamespace(ctx=self.context(8))))
            return SimpleNamespace(ctx=self.confirmation)

        self.bot.wait_for_component = AsyncMock(side_effect=confirm)

        async def success(**kwargs):
            self.assertEqual(await FinalMatchResult.count(), 1)
            self.assertEqual((await resolve_final()).nodes["QF1"].result_version, 1)
            self.assertEqual(kwargs["content"], f"{SUCCESS_EMOJI} Result saved.")
            self.assertEqual(kwargs["components"], [])
            self.assertNotIn("Official result", [field.name.strip("*_") for field in kwargs["embed"].fields])

        self.confirmation.edit_origin.side_effect = success
        await self.submit()
        self.confirmation.defer.assert_awaited_once_with(edit_origin=True)
        self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])

    async def test_cancel_and_timeout_leave_results_and_profiles_unchanged(self):
        profiles = [row.model_dump() for row in await FinalPlayerProfile.find_all().to_list()]
        self.confirmation.custom_id = "finalcancel|100"
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=self.confirmation))
        await self.submit()
        self.assertEqual(self.confirmation.edit_origin.await_args.kwargs["content"],
                         f"{SUCCESS_EMOJI} Submission cancelled.")
        self.assertEqual(self.confirmation.edit_origin.await_args.kwargs["components"], [])
        self.bot.wait_for_component.side_effect = asyncio.TimeoutError
        await self.submit()
        self.assertIn("expired", self.ctx.edit.await_args.kwargs["content"])
        self.assertTrue(self.ctx.edit.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.assertEqual(self.ctx.edit.await_args.kwargs["components"], [])
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertEqual(profiles, [row.model_dump() for row in await FinalPlayerProfile.find_all().to_list()])
        self.bot.fetch_channel.assert_not_awaited()

    async def test_stale_pairing_and_revoked_role_reject_confirmation(self):
        async def changed(**kwargs):
            await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
                {"$inc": {"nodes.QF1.contestant_revision": 1}})
            return SimpleNamespace(ctx=self.confirmation)

        self.bot.wait_for_component = AsyncMock(side_effect=changed)
        await self.submit()
        self.assertIn("match changed", self.confirmation.edit_origin.await_args.kwargs["content"])
        self.assertEqual(await FinalMatchResult.count(), 0)
        await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
            {"$inc": {"nodes.QF1.contestant_revision": -1}})
        self.confirmation.author.has_role.return_value = False
        self.confirmation.author.has_role.side_effect = None
        self.bot.wait_for_component.side_effect = None
        self.bot.wait_for_component.return_value = SimpleNamespace(ctx=self.confirmation)
        await self.submit()
        self.assertIn("manager", self.confirmation.edit_origin.await_args.kwargs["content"])
        self.assertEqual(await FinalMatchResult.count(), 0)

    async def test_invalid_scores_do_not_open_confirmation_and_database_failure_has_no_success(self):
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=self.confirmation))
        await self.cog.final_result_modal.callback.func(self.cog, self.ctx, player1="3.100", player2="2.80")
        self.bot.wait_for_component.assert_not_awaited()
        await self.client.admin.command({"configureFailPoint": "failCommand", "mode": {"times": 1},
            "data": {"failCommands": ["insert"], "errorCode": 2}})
        try:
            await self.submit()
        finally:
            await self.client.admin.command({"configureFailPoint": "failCommand", "mode": "off"})
        self.assertIn("Could not confirm the save", self.confirmation.edit_origin.await_args.kwargs["content"])
        self.assertTrue(self.confirmation.edit_origin.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.bot.fetch_channel.assert_not_awaited()

    async def test_profile_and_board_use_established_labels_without_refresh_or_revision(self):
        user = MagicMock(id=1, avatar=None)
        embed, controls = await profile_view(self.final, user, "9", guild=self.ctx.guild)
        self.assertTrue(embed.title.startswith("**") and embed.title.endswith("**"))
        self.assertTrue(all("**" in field.name for field in embed.fields))
        text = "\n".join(field.value for field in embed.fields)
        self.assertIn("**Avg. Stars:** 0.00", text)
        self.assertIn("**Avg. Destruction:** 0.00%", text)
        self.assertIn(f"**Available:** 5,000 {TOKEN_EMOJI}", text)
        self.assertEqual([button.label for button in controls[1].components], ["Matches"])
        row = FinalBoardRow("9", 2, 1, "Season 7 Final", eligible=3)
        self.assertEqual(final_board_text([row], "Predictions"),
                         "🥇 **[❌Invalid Player](<https://discordapp.com/users/9>)** `2/3` → `66.67%`")
        self.assertEqual(final_board_text([FinalBoardRow("9", 5000, 1, "Lifetime")], "Tokens"),
                         f"🥇 **[❌Invalid Player](<https://discordapp.com/users/9>)** `5,000` {TOKEN_EMOJI}")
        for page in range(4):
            pairing, rows = pairing_view(self.final, "9", page)
            self.assertEqual(pairing.title, f"**Season 7 Final · Pairing {page + 1}/4**")
            self.assertEqual(rows[-1].components[0].disabled, page == 0)
            self.assertEqual(rows[-1].components[1].disabled, page == 3)

    async def test_history_arrows_page_jump_boundaries_and_owner_preserve_results(self):
        node = self.final.nodes["QF1"]
        for version in range(3):
            await submit_result(FinalResultSubmission(final_id=self.final.id, node_id=node.id,
                actor_id="manager", operation_key=f"pages:{version}", expected_version=version,
                contestant_revision=node.contestant_revision, attempt=node.attempt,
                scores={node.contestants[0]: ["3.100"] * 7, node.contestants[1]: ["2.80"] * 7},
                reason=f"Correction {version}" if version else None))
            await self.cog._recover(await resolve_final())
        before = [row.model_dump() for row in await FinalMatchResult.find_all().to_list()]
        await self.cog._history_view(self.ctx, 7, "1")
        player_page = self.ctx.send.await_args.kwargs
        self.assertEqual([button.label for button in player_page["components"]], ["◀️️", "1/1", "▶️"])
        self.assertEqual([button.disabled for button in player_page["components"]], [True, False, True])
        self.assertEqual([field.name for field in player_page["embed"].fields], ["**Total**"])
        self.assertNotIn("Correction", player_page["embed"].description)
        self.assertEqual([row["reason"] for row in before], [None, "Correction 1", "Correction 2"])
        await self.cog._history_view(self.ctx, 7, "-")
        payload = self.ctx.send.await_args.kwargs
        self.assertEqual([button.label for button in payload["components"]], ["◀️️", "1/3", "▶️"])
        self.assertEqual([button.style for button in payload["components"]],
                         [ipy.ButtonStyle.PRIMARY, ipy.ButtonStyle.SECONDARY, ipy.ButtonStyle.PRIMARY])
        self.assertEqual([button.disabled for button in payload["components"]], [True, False, False])
        self.assertNotIn("**History**", [field.name for field in payload["embed"].fields])
        self.assertNotIn("**Reason**", [field.name for field in payload["embed"].fields])
        self.ctx.custom_id = payload["components"][2].custom_id
        await self.cog.final_history_page.callback.func(self.cog, self.ctx)
        middle = self.ctx.edit_origin.await_args.kwargs
        self.assertEqual([button.label for button in middle["components"]], ["◀️️", "2/3", "▶️"])
        self.assertEqual([button.disabled for button in middle["components"]], [False, False, False])
        self.assertIn("**History**", [field.name for field in middle["embed"].fields])
        self.assertNotIn("**Reason**", [field.name for field in middle["embed"].fields])
        self.ctx.custom_id = middle["components"][1].custom_id
        await self.cog.final_history_jump.callback.func(self.cog, self.ctx)
        modal = self.ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.title, "Jump to page")
        modal_ctx = self.context()
        modal_ctx.__class__ = ipy.ModalContext
        modal_ctx.message = self.message
        modal_ctx.custom_id = modal.custom_id
        await self.cog.final_history_jump_modal.callback.func(self.cog, modal_ctx, "3")
        modal_ctx.edit.assert_awaited_once()
        self.assertIs(modal_ctx.edit.await_args.args[0], self.message)
        last = modal_ctx.edit.await_args.kwargs
        self.assertEqual([button.label for button in last["components"]], ["◀️️", "3/3", "▶️"])
        self.assertEqual([button.disabled for button in last["components"]], [False, False, True])
        self.ctx.custom_id = last["components"][0].custom_id
        await self.cog.final_history_page.callback.func(self.cog, self.ctx)
        self.assertEqual(self.ctx.edit_origin.await_args.kwargs["components"][1].label, "2/3")
        modal_ctx.edit.reset_mock()
        await self.cog.final_history_jump_modal.callback.func(self.cog, modal_ctx, "4")
        modal_ctx.edit.assert_not_awaited()
        self.assertIn("between 1 and 3", modal_ctx.send.await_args.args[0])
        self.ctx.author.id = 8
        self.ctx.edit_origin.reset_mock()
        await self.cog.final_history_page.callback.func(self.cog, self.ctx)
        self.ctx.edit_origin.assert_not_awaited()
        self.assertIn("another user", self.ctx.send.await_args.args[0])
        await self.cog.final_history_jump.callback.func(self.cog, self.ctx)
        self.assertEqual(self.ctx.send_modal.await_count, 1)
        self.assertEqual(before, [row.model_dump() for row in await FinalMatchResult.find_all().to_list()])

    async def test_public_betting_embed_omits_saved_staff_refund_notes(self):
        market = await open_market(self.final, "QF1")
        await void_market(market.id, "manager", "Internal QA refund note")
        await self.cog._recover(await resolve_final())
        saved = await FinalBettingMarket.get(market.id)
        embed = market_embed(self.final, saved)
        self.assertIn("REFUNDED", embed.description)
        self.assertEqual([field.name for field in embed.fields], ["**Total Pool**"])
        self.assertNotIn("Internal QA", embed.description + " ".join(field.value for field in embed.fields))
        self.assertEqual(saved.void_reason, "Internal QA refund note")
        self.assertEqual(embed.fields[0].value, f"0 {TOKEN_EMOJI}")
        self.assertTrue(market_controls(saved)[0].disabled)

    async def test_token_history_uses_standard_arrows_and_jump_without_wallet_changes(self):
        market = await open_market(self.final, "QF1")
        for version in range(20):
            await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="1", amount=100 + version,
                expected_version=version, action_key=f"token-pages:{version}"))
        before = (await TokenWallet.get("9")).model_dump()
        ledger = [row.model_dump() for row in await TokenLedgerEntry.find_all().to_list()]
        cog = TokenCommands(self.bot)
        await cog.tokens_history.callback.func(cog, self.ctx)
        payload = self.ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertIn(f"Edit: -1 {TOKEN_EMOJI} · QF1", payload["embed"].description)
        self.assertNotIn(" tokens", payload["embed"].description)
        self.assertEqual([button.label for button in payload["components"]], ["◀️️", "1/3", "▶️"])
        self.assertEqual([button.style for button in payload["components"]],
                         [ipy.ButtonStyle.PRIMARY, ipy.ButtonStyle.SECONDARY, ipy.ButtonStyle.PRIMARY])
        self.assertEqual([button.disabled for button in payload["components"]], [True, False, False])
        self.ctx.custom_id = payload["components"][2].custom_id
        await cog.token_page.callback.func(cog, self.ctx)
        middle = self.ctx.edit_origin.await_args.kwargs
        self.assertEqual([button.label for button in middle["components"]], ["◀️️", "2/3", "▶️"])
        self.assertEqual([button.disabled for button in middle["components"]], [False, False, False])
        self.ctx.custom_id = middle["components"][1].custom_id
        await cog.token_jump.callback.func(cog, self.ctx)
        modal_ctx = self.context()
        modal_ctx.__class__ = ipy.ModalContext
        modal_ctx.message = self.message
        modal_ctx.custom_id = self.ctx.send_modal.await_args.args[0].custom_id
        await cog.token_jump_modal.callback.func(cog, modal_ctx, "3")
        last = modal_ctx.edit.await_args.kwargs
        self.assertEqual([button.label for button in last["components"]], ["◀️️", "3/3", "▶️"])
        self.assertEqual([button.disabled for button in last["components"]], [False, False, True])
        modal_ctx.edit.reset_mock()
        await cog.token_jump_modal.callback.func(cog, modal_ctx, "0")
        modal_ctx.edit.assert_not_awaited()
        self.assertIn("positive integer", modal_ctx.send.await_args.args[0])
        self.ctx.author.id = 8
        self.ctx.edit_origin.reset_mock()
        await cog.token_page.callback.func(cog, self.ctx)
        self.ctx.edit_origin.assert_not_awaited()
        self.assertIn("another user", self.ctx.send.await_args.args[0])
        self.assertEqual(before, (await TokenWallet.get("9")).model_dump())
        self.assertEqual(ledger, [row.model_dump() for row in await TokenLedgerEntry.find_all().to_list()])

    async def test_bet_review_shows_profit_and_required_debt_without_changing_the_bet(self):
        market = await open_market(self.final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="7", contestant=market.contestants[1], amount=200,
            expected_version=0, action_key="review:opponent"))
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant="1", amount=100,
            expected_version=0, action_key="review:existing"))
        market = await FinalBettingMarket.get(market.id)
        before = (await FinalBet.find_one({"user_id": "9"})).model_dump()
        wallet = (await TokenWallet.get("9")).model_dump()
        embed, controls = await bet_review(market, "9", "1", 300, 1, guild=self.ctx.guild)
        self.assertEqual(embed.description, f"**[❌Invalid Player](<https://discordapp.com/users/1>)** · 300 {TOKEN_EMOJI}")
        self.assertEqual([(field.name, field.value) for field in embed.fields],
                         [("**Estimated Profit**", f"200 {TOKEN_EMOJI}")])
        self.assertEqual([button.label for button in controls], ["Confirm Bet", "Back"])
        self.assertEqual(before, (await FinalBet.find_one({"user_id": "9"})).model_dump())
        self.assertEqual(wallet, (await TokenWallet.get("9")).model_dump())
        self.ctx.custom_id = f"betopen|{market.id}"
        await self.cog.bet_open.callback.func(self.cog, self.ctx)
        chooser = self.ctx.send.await_args.kwargs["embed"]
        self.assertEqual([field.name for field in chooser.fields], ["**Available Tokens**"])
        self.assertIn(f"100 {TOKEN_EMOJI}", chooser.description)
        await TokenWallet.get_pymongo_collection().update_one({"_id": "9"}, {"$set": {"balance": -100}})
        debt_review, _ = await bet_review(market, "9", "1", 50, 1, guild=self.ctx.guild)
        self.assertEqual([(field.name, field.value) for field in debt_review.fields],
                         [("**Estimated Profit**", f"200 {TOKEN_EMOJI}"), ("**Debt**", f"100 {TOKEN_EMOJI}")])
        cancellation, cancel_controls = await bet_review(market, "9", "1", 0, 1, guild=self.ctx.guild)
        self.assertEqual(cancellation.description, f"Cancel bet · Refund: 100 {TOKEN_EMOJI}")
        self.assertEqual([(field.name, field.value) for field in cancellation.fields],
                         [("**Debt**", f"100 {TOKEN_EMOJI}")])
        self.assertEqual(cancel_controls[0].label, "Cancel Bet")
        self.assertEqual(before, (await FinalBet.find_one({"user_id": "9"})).model_dump())
        self.assertEqual((await TokenWallet.get("9")).balance, -100)

    async def base_setup(self):
        source = await prepare_match(self.final, "QF1")
        await source.set({"thread": "777"})
        ctx = self.context(int(source.participants[0].user_id))
        message = SimpleNamespace(id=888, author=SimpleNamespace(id=42), pinned=True,
            embeds=[ipy.Embed(fields=[ipy.EmbedField(name=str(n), value="value") for n in range(4)])],
            edit=AsyncMock(), components=[ipy.ActionRow(ipy.Button(label="Submit Result",
                style=ipy.ButtonStyle.GRAY, custom_id=f"submit_button|{source.token}"))])
        thread = MagicMock(fetch_message=AsyncMock(return_value=message), fetch_pinned_messages=AsyncMock(return_value=[message]))
        self.bot.fetch_channel = AsyncMock(return_value=thread)
        await source.set({"thread_message_id": 888})
        return source, ctx, message

    async def test_recent_matches_show_opponent_and_latest_seven_district_totals(self):
        node = self.final.nodes["QF1"]
        own, opponent = node.contestants
        before = [player.model_dump() for player in await Player.find_all().to_list()]
        for version in (0, 1):
            await submit_result(FinalResultSubmission(final_id=self.final.id, node_id=node.id,
                actor_id="manager", operation_key=f"recent:{version}", expected_version=version,
                contestant_revision=node.contestant_revision, attempt=node.attempt,
                scores={own: ["3.100" if version == 0 else "2.80"] * 7,
                        opponent: ["2.80" if version == 0 else "3.100"] * 7},
                reason="Correct the test scores" if version else None))
            await self.cog._recover(await resolve_final())
        embed, _ = await profile_view(await resolve_final(), MagicMock(id=int(own), avatar=None), "9",
                                     guild=self.ctx.guild)
        recent = next(field for field in embed.fields if "Recent Matches" in field.name)
        self.assertEqual(recent.name, "__**Recent Matches**__")
        self.assertEqual(recent.value.count("vs."), 1)
        self.assertIn(f"vs. **[❌Invalid Player](<https://discordapp.com/users/{opponent}>)**", recent.value)
        self.assertIn("Loss", recent.value)
        self.assertIn("560% - 21", recent.value)
        self.assertIn("700%", recent.value)
        self.assertNotIn("revision", recent.value.lower())
        self.assertEqual(await FinalMatchResult.count(), 2)
        self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])

    async def test_command_saves_and_replaces_all_seven_bases_before_publication(self):
        source, ctx, message = await self.base_setup()
        before = [player.model_dump() for player in await Player.find_all().to_list()]
        for index, district in enumerate(source.final_districts):
            link = f"https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%{DISTRICT_CODES[district]}%3ATEST"
            await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, link, match="QF1")
            saved = await Match.get(source.id)
            self.assertEqual(saved.participants[0].bases[index].link, link)
            self.assertIn("Base submitted", ctx.send.await_args.args[0])
        replacement = saved.participants[0].bases[6].link.replace("TEST", "REPLACED")
        await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, replacement, match="QF1")
        self.assertEqual((await Match.get(source.id)).participants[0].bases[6].link, replacement)
        self.assertEqual((await Match.get(source.id)).final_base_page_revision, 8)
        self.assertTrue(all(base is None for base in (await Match.get(source.id)).participants[1].bases))
        self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])
        self.assertEqual(message.edit.await_count, 8)

    async def test_base_command_rejects_foreign_finished_and_concurrent_changes(self):
        source, ctx, message = await self.base_setup()
        link = f"https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%{DISTRICT_CODES[source.final_districts[6]]}%3ATEST"
        ctx.author.id = 9
        await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, link, match="QF1")
        self.assertIn("Only the players", ctx.send.await_args.args[0])
        ctx.author.id = int(source.participants[0].user_id)
        await source.set({"final_result_id": "test-result"})
        await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, link, match="QF1")
        self.assertIn("finished", ctx.send.await_args.args[0])
        await source.set({"final_result_id": None})

        collection = Match.get_pymongo_collection()
        update_one = collection.update_one

        async def concurrent_update(query, update, **kwargs):
            if "final_base_page_revision" in query:
                await update_one({"_id": source.id}, {"$inc": {"final_base_page_revision": 1}})
            return await update_one(query, update, **kwargs)

        with patch.object(collection, "update_one", side_effect=concurrent_update):
            await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, link, match="QF1")
        self.assertIn("not saved", ctx.send.await_args.args[0])
        self.assertTrue(all(base is None for base in (await Match.get(source.id)).participants[0].bases))
        message.edit.assert_not_awaited()

    async def test_base_delivery_failure_retains_saved_base_and_old_modal_is_retired(self):
        source, ctx, message = await self.base_setup()
        link = f"https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%{DISTRICT_CODES[source.final_districts[6]]}%3ATEST"
        message.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        await self.match_cog.match_base_submit.callback.func(self.match_cog, ctx, link, match="QF1")
        self.assertEqual((await Match.get(source.id)).participants[0].bases[6].link, link)
        self.assertIn("Base saved.", ctx.send.await_args.args[0])
        ctx.custom_id = f"submit_bases|{source.token}"
        await self.match_cog.submit_bases_button.callback.func(self.match_cog, ctx)
        ctx.send_modal.assert_not_awaited()
        self.assertIn("/match base submit match:QF1", ctx.send.await_args.args[0])

    async def test_announcement_targets_final_channel_and_reuses_stored_channel_on_retry(self):
        await self.final.set({"channel_id": 123})
        market = await open_market(self.final, "QF1")
        message = SimpleNamespace(id=888, edit=AsyncMock())
        channel = MagicMock(send=AsyncMock(return_value=message), fetch_message=AsyncMock(return_value=message))
        channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        await publish_market(self.bot, self.final, market)
        self.bot.fetch_channel.assert_awaited_once_with(FINAL_CHANNEL)
        self.assertFalse(channel.send.await_args.kwargs["components"][0].disabled)
        self.assertEqual(channel.send.await_args.kwargs["components"][0].label, "Place / Edit Bet")
        self.assertEqual(channel.send.await_args.kwargs["components"][0].to_dict()["emoji"]["id"],
                         1555865080711876660)
        self.assertIn(f"**[❌Invalid Player](<https://discordapp.com/users/1>)**: 0 {TOKEN_EMOJI}",
                      channel.send.await_args.kwargs["embed"].description)
        stored = await FinalBettingMarket.get(market.id)
        self.assertEqual(stored.announcement_channel_id, FINAL_CHANNEL)
        await self.final.set({"channel_id": 456})
        await publish_market(self.bot, self.final, stored)
        self.assertEqual(self.bot.fetch_channel.await_args.args[0], FINAL_CHANNEL)
        channel.send.assert_awaited_once()

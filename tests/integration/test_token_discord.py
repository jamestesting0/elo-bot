import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import TOKEN_EMOJI, configure_application_emojis
from elo_bot.config.ids import MAIN_GUILD
from elo_bot.db import Match, Participant
from elo_bot.db.token_models import ParticipantStake, TokenLedgerEntry, TokenWallet
from elo_bot.extensions.match import MatchCommands
from elo_bot.extensions.tokens import TokenCommands
from elo_bot.features.tokens.stake_publication import recover_stakes
from elo_bot.features.tokens.stakes import stake_window_closed
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet
from elo_bot.features.tokens.queue_items import insert_queued_match
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket, request_cancellation
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.matches.cleanup import delete_unreserved_match
from tests.integration._final_fixture import FinalScenarioCase


class TestTokenDiscord(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.http = MagicMock(post_initial_response=AsyncMock(), post_followup=AsyncMock(return_value={"id": "9001"}),
                              edit_interaction_message=AsyncMock(return_value={"id": "9001"}),
                              get_interaction_message=AsyncMock(return_value={"id": "9001"}))
        self.discord = MagicMock(http=self.http, user=MagicMock(id=42))
        self.discord.app.id = 42
        message = MagicMock(id=9001, flags=ipy.MessageFlags.NONE)
        self.discord.cache.place_message_data.return_value = message
        self.discord.cache.get_message.return_value = message
        self.tokens = TokenCommands(ipy.Client())
        self.matches = MatchCommands(ipy.Client())
        self.matches.bot = self.discord
        self.sequence = 1700000000000000000

    def context(self, kind, data, user="1"):
        self.sequence += 1
        user_object = MagicMock(id=int(user), has_role=MagicMock(return_value=False))
        self.discord.cache.get_member.return_value = user_object
        self.discord.cache.get_user.return_value = user_object
        return kind.from_dict(self.discord, {
            "id": str(self.sequence), "application_id": "42", "type": 2 if kind is ipy.SlashContext else 3,
            "token": "offline", "locale": "en-US", "guild_id": str(MAIN_GUILD), "channel_id": "100",
            "user": {"id": user}, "message": {"id": "9001"}, "data": data,
        })

    def edit(self):
        call = self.http.edit_interaction_message.await_args
        return call.args[0] if call.args else call.kwargs["payload"]

    async def click(self, control, *, value=None, user="1"):
        data = {"custom_id": control["custom_id"], "component_type": 3 if value else 2}
        if value:
            data["values"] = [value]
        ctx = self.context(ipy.ComponentContext, data, user)
        await self.tokens.token_item_control.callback.func(self.tokens, ctx)

    def stake_announcement(self, match):
        embed = ipy.Embed(title="Match", fields=[ipy.EmbedField(name="**Notes**", value="- Existing note.")])
        message = MagicMock(id=456, author=MagicMock(id=42), embeds=[embed], edit=AsyncMock(),
                            components=[ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Submit",
                                                               custom_id=f"submit_button|{match.token}"))])
        self.discord.fetch_channel = AsyncMock(return_value=MagicMock(fetch_message=AsyncMock(return_value=message)))
        return embed, message

    async def stake(self, user, amount):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "match"}, user)
        await self.matches.match_stake.callback.func(self.matches, ctx, amount)
        return self.edit()

    async def test_public_shop_and_inventory_keep_item_controls_owner_only(self):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_shop.callback.func(self.tokens, ctx)
        self.assertNotIn("data", self.http.post_initial_response.await_args.args[0])
        first = self.edit()
        self.assertEqual([option["label"] for option in first["components"][0]["components"][0]["options"]],
                         ["Queue Priority Pro", "Queue Priority Ultra", "District Choice"])
        await self.click(first["components"][1]["components"][2])
        last = self.edit()
        self.assertEqual(last["components"][1]["components"][1]["label"], "2/2")
        await self.click(last["components"][0]["components"][0], value="rank_protection")
        confirm = self.edit()["components"][0]["components"][0]
        await self.click(confirm, user="2")
        self.assertIsNone(await TokenWallet.get("2"))
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        await self.click(confirm)
        await self.click(confirm)
        self.assertEqual((await TokenWallet.get("1")).balance, 1400)
        await self.click(self.edit()["components"][0]["components"][0], value="rank_protection")
        await self.click(self.edit()["components"][0]["components"][0])
        self.assertEqual((await TokenWallet.get("1")).balance, 1400)
        failure = self.http.post_followup.await_args.args[0]
        self.assertEqual(failure["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertIn("enough available tokens", failure["content"])
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx)
        self.assertNotIn("data", self.http.post_initial_response.await_args.args[0])
        await self.click(self.edit()["components"][0]["components"][2])
        inventory = self.edit()
        self.assertEqual(inventory["components"][0]["components"][0]["options"][0]["label"], "Rank Protection")
        await self.click(inventory["components"][0]["components"][0], value="rank_protection")
        equipped = self.edit()
        self.assertIn("Rank Protection ready", equipped["content"])
        cancelled = Match(id=PydanticObjectId(), token="cancel-equipment", matched_time=1,
                          participants=[Participant(user_id="1"), Participant(user_id="2")])
        await insert_queued_match(cancelled, [["Barbarian Camp"], ["Golem Quarry"], ["Goblin Mines"]], (None, None))
        self.assertTrue(await delete_unreserved_match(cancelled, pre_thread=True))
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx)
        await self.click(self.edit()["components"][0]["components"][2])
        restored = self.edit()
        self.assertEqual(restored["components"][0]["components"][0]["options"][0]["label"], "Rank Protection")
        wallet = await TokenWallet.get("1")
        print("SHOP_HTTP", json.dumps({"first": first, "last": last, "insufficient_funds": failure,
                                       "inventory": inventory, "equipped": equipped, "restored": restored, "wallet": {
                                           "balance": wallet.balance, "items": wallet.inventory,
                                       }}, default=str))

    async def test_hidden_shop_and_inventory_keep_challenge_codes_private(self):
        wallet = await ensure_wallet("1")
        await wallet.set({TokenWallet.inventory: {"challenge_override": 1}})
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_shop.callback.func(self.tokens, ctx, hidden=True)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)

        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx)
        await self.click(self.edit()["components"][0]["components"][2])
        await self.click(self.edit()["components"][0]["components"][0], value="challenge_override")
        receipt = self.http.post_followup.await_args.args[0]
        code = receipt["content"].split("`")[1]
        self.assertEqual(receipt["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertIn("Challenge Override ready", self.edit()["content"])
        self.assertNotIn(code, json.dumps(self.edit(), default=str))

        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx, hidden=True)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        self.discord.cache.get_message.return_value.flags = ipy.MessageFlags.EPHEMERAL
        await self.click(self.edit()["components"][0]["components"][2])
        self.assertIn(code, json.dumps(self.edit(), default=str))

    async def test_stake_command_edits_only_its_notes(self):
        match = await Match(token="stake-discord", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        embed, message = self.stake_announcement(match)
        await self.stake("1", "500")
        self.assertEqual(embed.fields[0].value, f"- Existing note.\n- <@1> put 500 {TOKEN_EMOJI} at stake.")
        embed.fields[0].value += "\n- A later match note."
        self.addCleanup(configure_application_emojis, "test-fallback")
        configure_application_emojis("beta")
        await self.stake("1", "550")
        self.assertEqual(embed.fields[0].value, f"- Existing note.\n- <@1> put 550 {TOKEN_EMOJI} at stake.\n"
                         "- A later match note.")
        offered = embed.to_dict()
        await self.stake("1", "0")
        self.assertEqual(embed.fields[0].value, "- Existing note.\n- A later match note.")
        removed = embed.to_dict()
        await self.stake("1", "600")
        message.edit.side_effect = ipy.errors.HTTPException(SimpleNamespace(status=503, reason="Unavailable"))
        with self.assertLogs(level="ERROR"):
            await self.stake("2", "600")
        pending = self.edit()
        self.assertIn("Notes update is pending", pending["content"])
        self.assertTrue((await ParticipantStake.get(str(match.id))).publication_pending)
        message.edit.side_effect = None
        await recover_stakes(self.discord)
        self.assertFalse((await ParticipantStake.get(str(match.id))).publication_pending)
        self.assertEqual(embed.fields[0].value,
                         f"- Existing note.\n- A later match note.\n- <@1> put 600 {TOKEN_EMOJI} at stake.\n"
                         f"- <@2> matches the stake with 600 {TOKEN_EMOJI}.")
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [4400, 4400])
        print("STAKE_HTTP", json.dumps({"offered": offered, "removed": removed, "pending": pending,
                                        "recovered": embed.to_dict(), "stake": (
                                            await ParticipantStake.get(str(match.id))
                                        ).model_dump(mode="json")}, default=str))

    async def test_final_stake_callback_accepts_over_cap_and_refunds_on_cancellation(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        match = await self.scheduled_pairing(final, "QF1")
        await match.set({"thread": "100", "thread_message_id": 456})
        await bind_thread(final, match, "100")
        players = [participant.user_id for participant in match.participants]
        embed, _ = self.stake_announcement(match)
        await self.stake(players[0], "1500")
        fixed = await self.stake(players[1], "1500")
        self.assertIn("1,500", fixed["content"])
        self.assertEqual(embed.fields[0].value.count("1,500"), 2)
        held = [(await TokenWallet.get(user)).balance for user in players]
        self.assertEqual(held, [3500, 3500])
        await accept_final_start(match, "manager")
        late = await self.stake(players[0], "0")
        self.assertIn("Stakes close when the match starts.", late["content"])
        await request_cancellation(final, "Test cancellation")
        stake = await ParticipantStake.get(str(match.id))
        self.assertEqual(stake.state, "settled")
        refunded = [(await TokenWallet.get(user)).balance for user in players]
        self.assertEqual(refunded, [5000, 5000])
        print("FINAL_STAKE_HTTP", json.dumps({"notes": embed.to_dict(), "fixed": fixed, "late": late,
                                             "escrow": stake.model_dump(mode="json"),
                                             "held_balances": held, "refunded_balances": refunded}, default=str))

    async def test_callback_rejects_matching_an_open_offer_after_start(self):
        match = await Match(token="late-stake", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        embed, _ = self.stake_announcement(match)
        await self.stake("1", "500")
        await stake_window_closed(match)
        late = await self.stake("2", "500")
        self.assertIn("Stakes close when the match starts.", late["content"])
        await recover_stakes(self.discord)
        holds = await TokenLedgerEntry.find({"kind": "match_hold"}).count()
        self.assertEqual(holds, 0)
        self.assertEqual(embed.fields[0].value, "- Existing note.")
        print("LATE_STAKE_HTTP", json.dumps({"response": late, "holds": holds, "notes": embed.to_dict()}, default=str))

    async def test_short_all_in_automatically_edits_the_offer_note_and_locks_both_wallets(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        match = await self.scheduled_pairing(final, "QF1")
        await match.set({"thread": "100", "thread_message_id": 456})
        await bind_thread(final, match, "100")
        rich, short = [participant.user_id for participant in match.participants]
        await adjust_balance(rich, 5000, "manager", "add-offer")
        await adjust_balance(short, -1000, "manager", "short-call")
        embed, _ = self.stake_announcement(match)
        await self.stake(rich, "all-in")
        self.assertIn(f"<@{rich}> went all in (10,000 {TOKEN_EMOJI}).", embed.fields[0].value)
        receipt = await self.stake(short, "all-in")
        self.assertIn(f"Stakes fixed at 4,000 {TOKEN_EMOJI} each.", receipt["content"])
        self.assertEqual(embed.fields[0].value, "- Existing note.\n"
                         f"- <@{rich}>’s stake was reduced to 4,000 {TOKEN_EMOJI}.\n"
                         f"- <@{short}> went all in (4,000 {TOKEN_EMOJI}).")
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (rich, short)], [6000, 0])

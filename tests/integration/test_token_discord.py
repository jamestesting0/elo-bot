import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import OperationFailure

from elo_bot.config.emojis import TOKEN_EMOJI, configure_application_emojis
from elo_bot.config.ids import LVL1_ROLES, MAIN_GUILD
from elo_bot.db import Match, Participant
from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import TokenItem
from elo_bot.db.token_models import ParticipantStake, TokenItemUse, TokenLedgerEntry, TokenWallet
from elo_bot.extensions.match import MatchCommands
from elo_bot.extensions.tokens import TokenCommands
from elo_bot.features.tokens.stake_publication import recover_stakes
from elo_bot.features.tokens.stakes import stake_window_closed
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet
from elo_bot.features.tokens.exp import recover_exp, redeem_exp
from elo_bot.features.tokens.items import purchase
from elo_bot.features.tokens.items import activate, finish_use
from elo_bot.features.tokens.item_management import ItemAdjustment, adjust_items, deactivate_item
from elo_bot.features.tokens.queue_items import reserve_queue_items
from elo_bot.features.queue.policy import QUEUE_OPERATION_LOCK
from elo_bot.integrations.exp import ExpGrantError
from elo_bot.features.tokens.queue_items import insert_queued_match
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket, request_cancellation
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.matches.cleanup import delete_unreserved_match
from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.db import Player


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
        user_object = MagicMock(id=int(user), display_name=f"Player {user}", has_role=MagicMock(return_value=False),
                                display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png"))
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
        self.stake_posts = {}

        async def send(content, *, components, allowed_mentions, **kwargs):
            post = MagicMock(id=700 + len(self.stake_posts), author=MagicMock(id=42), content=content,
                             components=[ipy.ActionRow(*components)], allowed_mentions=allowed_mentions)

            async def edit(**changes):
                post.content = changes["content"]
                post.components = changes["components"]

            post.edit = AsyncMock(side_effect=edit)
            self.stake_posts[post.id] = post
            return post

        async def history(**_kwargs):
            for post in reversed(self.stake_posts.values()):
                yield post

        self.stake_channel = MagicMock(
            send=AsyncMock(side_effect=send), history=history,
            fetch_message=AsyncMock(side_effect=lambda message_id: message if message_id == 456 else self.stake_posts.get(message_id)),
        )
        self.discord.fetch_channel = AsyncMock(return_value=self.stake_channel)
        return embed, message

    async def stake(self, user, amount):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "match"}, user)
        await self.matches.match_stake.callback.func(self.matches, ctx, amount)
        return self.edit()

    async def stake_reply(self, post, button, *, user="2"):
        self.discord.cache.get_message.return_value = post
        ctx = self.context(ipy.ComponentContext, {"custom_id": button.custom_id, "component_type": 2}, user)
        await self.matches.match_stake_reply.callback.func(self.matches, ctx)
        return self.edit()

    async def test_balance_user_option_shows_the_selected_wallet_without_creating_the_callers_wallet(self) -> None:
        await adjust_balance("2", 242, "9", "view-other-balance")
        self.discord.cache.place_user_data.return_value = ipy.User.from_dict(
            {"id": "2", "username": "james", "discriminator": "0", "avatar": None}, self.discord)
        self.discord.cache.get_guild.return_value.fetch_member = AsyncMock(return_value=None)
        ctx = self.context(ipy.SlashContext, {
            "id": "42", "name": "tokens", "resolved": {"users": {"2": {"id": "2", "username": "james"}}},
            "options": [{"name": "balance", "type": ipy.OptionType.SUB_COMMAND, "options": [
                {"name": "user", "type": ipy.OptionType.USER, "value": "2"},
            ]}],
        })

        await self.tokens.tokens_balance(ctx)

        self.assertNotIn("data", self.http.post_initial_response.await_args.args[0])
        embed = self.edit()["embeds"][0]
        self.assertIn(f"5,242 {TOKEN_EMOJI}", embed["description"])
        self.assertEqual(embed["title"], "**Tokens**")
        self.assertEqual(embed["author"]["name"], "james")
        self.assertEqual(embed["author"]["url"], "https://discord.com/users/2")
        self.assertNotIn("thumbnail", embed)
        self.assertEqual([field["name"] for field in embed["fields"]], ["__**Bets**__"])
        self.assertIsNone(await TokenWallet.get("1"))

    async def test_bulk_buy_command_charges_once_reports_total_and_supports_default_amount_and_hidden(self):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_buy.callback.func(self.tokens, ctx, "queue_priority_pro", 3)
        self.assertNotIn("data", self.http.post_initial_response.await_args.args[0])
        self.assertIn("`3` × Queue Priority Pro", self.edit()["content"])
        self.assertIn(f"2,400 {TOKEN_EMOJI}", self.edit()["content"])
        retry = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        retry.id = ctx.id
        await self.tokens.tokens_buy.callback.func(self.tokens, retry, "queue_priority_pro", 3)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (2600, 3))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 1)
        hidden = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_buy.callback.func(self.tokens, hidden, "queue_priority_pro", hidden=True)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertIn("`1` × Queue Priority Pro", self.edit()["content"])
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (1800, 4))
        history = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_history.callback.func(self.tokens, history)
        self.assertIn("Queue Priority Pro × 3", self.edit()["embeds"][0]["description"])

    async def test_bulk_use_activates_owned_items_once_and_supports_default_amount_and_hidden(self):
        await adjust_items(ItemAdjustment("1", TokenItem.PRIORITY_PRO, 5, "9", "give-for-use"))
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_use.callback.func(self.tokens, ctx, "queue_priority_pro", 3)
        self.assertNotIn("data", self.http.post_initial_response.await_args.args[0])
        self.assertIn("Activated `3` × Queue Priority Pro", self.edit()["content"])
        retry = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        retry.id = ctx.id
        await self.tokens.tokens_use.callback.func(self.tokens, retry, "queue_priority_pro", 3)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 3)
        hidden = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_use.callback.func(self.tokens, hidden, "queue_priority_pro", hidden=True)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (5000, 1))
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 4)
        insufficient = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_use.callback.func(self.tokens, insufficient, "queue_priority_pro", 2)
        self.assertIn("enough of this item", self.edit()["content"])
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 1)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 4)

    async def test_bulk_district_use_requires_a_choice_and_keeps_it_on_every_queued_copy(self):
        await adjust_items(ItemAdjustment("1", TokenItem.DISTRICT, 2, "9", "give-district"))
        missing = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_use.callback.func(self.tokens, missing, "district_choice", 2)
        self.assertIn("Choose a district", self.edit()["content"])
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 0)
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_use.callback.func(self.tokens, ctx, "district_choice", 2, district="Barbarian Camp")
        uses = await TokenItemUse.find({"wallet_id": "1"}).sort("activation_order").to_list()
        self.assertEqual([(use.district, use.state) for use in uses],
                         [("Barbarian Camp", "armed"), ("Barbarian Camp", "queued")])
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.DISTRICT], 0)

    async def test_bulk_challenge_use_immediately_issues_every_code_privately(self):
        for user, hidden, amount in (("1", False, 100), ("2", True, 3)):
            with self.subTest(hidden=hidden):
                await adjust_items(ItemAdjustment(user, TokenItem.CHALLENGE, amount, "9", f"give-codes:{user}"))
                ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, user)
                self.http.post_followup.reset_mock()
                await self.tokens.tokens_use.callback.func(self.tokens, ctx, "challenge_override", amount, hidden=hidden)
                uses = await TokenItemUse.find({"wallet_id": user}).sort("activation_order").to_list()
                self.assertEqual([use.state for use in uses], ["armed"] * amount)
                codes = {use.target for use in uses}
                self.assertEqual(len(codes), amount)
                self.assertNotIn(None, codes)
                content = self.edit()["content"]
                self.assertLessEqual(len(content), 2000)
                for code in codes:
                    self.assertIn(code, content)
                self.assertIn("Save", content)
                self.assertIn(f"expire <t:{uses[0].expires_at}:R>", content)
                self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"],
                                 ipy.MessageFlags.EPHEMERAL)
                self.http.post_followup.assert_not_awaited()
                self.assertEqual((await TokenWallet.get(user)).inventory[TokenItem.CHALLENGE], 0)

    async def test_bulk_use_delivery_failure_preserves_the_batch_and_retry_does_not_activate_more(self):
        await adjust_items(ItemAdjustment("1", TokenItem.PROTECTION, 3, "9", "give-protection"))
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        with patch.object(self.http, "edit_interaction_message", side_effect=RuntimeError("Discord unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
                await self.tokens.tokens_use.callback.func(self.tokens, ctx, "rank_protection", 3)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PROTECTION], 0)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 3)
        retry = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        retry.id = ctx.id
        await self.tokens.tokens_use.callback.func(self.tokens, retry, "rank_protection", 3)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1"}).count(), 3)
        self.assertIn("Activated `3` × Rank Protection", self.edit()["content"])

    async def test_bulk_exp_use_queues_every_copy_and_stops_after_an_unconfirmed_grant(self):
        grants = []

        async def grant(user, reserve):
            await reserve()
            grants.append(user)
            if user == "2":
                raise ExpGrantError(uncertain=True)
            return str(len(grants))

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            for user in ("1", "2"):
                await adjust_items(ItemAdjustment(user, TokenItem.EXP, 3, "9", f"give-exp:{user}"))
                ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, user)
                await self.tokens.tokens_use.callback.func(self.tokens, ctx, "exp_potion", 3)
                self.assertIn("Queued `3` × EXP Potion", self.edit()["content"])
                self.assertNotIn("Granted", self.edit()["content"])
                for _ in range(3):
                    await recover_exp()
                self.assertEqual((await TokenWallet.get(user)).inventory[TokenItem.EXP], 0)
        self.assertEqual(grants, ["1", "1", "1", "2"])
        self.assertEqual(await TokenItemUse.find({"wallet_id": "1", "state": "used"}).count(), 3)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "2", "state": "reserved"}).count(), 1)
        self.assertEqual(await TokenItemUse.find({"wallet_id": "2", "state": "queued"}).count(), 2)

    async def test_unaffordable_bulk_buy_reports_failure_and_never_buys_a_partial_quantity(self):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_buy.callback.func(self.tokens, ctx, "queue_priority_pro", 7)
        self.assertIn("enough available tokens", self.edit()["content"])
        self.assertNotIn("Purchased", self.edit()["content"])
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory), (5000, {}))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 0)

    async def test_bulk_buy_persistence_failure_cannot_publish_success_or_leave_a_charge(self):
        wallet = await ensure_wallet("1")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        ledger = TokenLedgerEntry.get_pymongo_collection()
        with patch.object(ledger, "insert_one", side_effect=OperationFailure("receipt unavailable")):
            with self.assertRaises(OperationFailure):
                await self.tokens.tokens_buy.callback.func(self.tokens, ctx, "queue_priority_pro", 3)
        self.http.edit_interaction_message.assert_not_awaited()
        wallet = await TokenWallet.get(wallet.id)
        self.assertEqual((wallet.balance, wallet.inventory), (5000, {}))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 0)

    async def test_bulk_buy_delivery_failure_preserves_purchase_and_retry_does_not_charge_again(self):
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        with patch.object(self.http, "edit_interaction_message", side_effect=RuntimeError("Discord unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
                await self.tokens.tokens_buy.callback.func(self.tokens, ctx, "queue_priority_pro", 2)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (3400, 2))
        retry = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        retry.id = ctx.id
        await self.tokens.tokens_buy.callback.func(self.tokens, retry, "queue_priority_pro", 2)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (3400, 2))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 1)
        self.assertIn("`2` × Queue Priority Pro", self.edit()["content"])

    async def exp_control(self, user="1", quantity=1):
        wallet = await adjust_balance(user, 20000, "manager", f"exp-funds:{user}")
        for index in range(quantity):
            await purchase(wallet, TokenItem.EXP, f"exp-buy:{index}")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, user)
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx)
        return wallet, self.edit()["components"][0]["components"][0]

    async def test_exp_shop_purchase_and_redemption_replay_grants_once_after_reservation(self):
        await adjust_balance("1", 20000, "manager", "exp-funds")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_shop.callback.func(self.tokens, ctx)
        await self.click(self.edit()["components"][-1]["components"][2])
        shop = self.edit()
        field = next(field for field in shop["embeds"][0]["fields"] if "EXP Potion" in field["name"])
        self.assertIn("10,000", field["value"])
        await self.click(shop["components"][0]["components"][0], value="exp_potion")
        buy = self.edit()["components"][0]["components"][0]
        await self.click(buy)
        await self.click(buy)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.EXP]), (15000, 1))
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx)
        control = self.edit()["components"][0]["components"][0]
        grants = []

        async def grant(user, reserve):
            self.assertIn("Granting", self.edit()["content"])
            self.assertEqual(self.edit()["components"], [])
            await reserve()
            current = await TokenWallet.get("1")
            use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp"})
            self.assertEqual((current.inventory[TokenItem.EXP], use.state, use.active), (0, "reserved", True))
            grants.append(user)
            return "123456"

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            await self.click(control, value="exp_potion")
            await self.click(control, value="exp_potion")
        use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp"})
        self.assertEqual((use.state, use.active, use.target), ("used", False, "123456"))
        self.assertEqual(grants, ["1"])
        self.assertIn("Granted 3,000 server EXP", self.edit()["content"])

    async def test_exp_failed_preflight_or_explicit_rejection_returns_potion_without_success(self):
        for user, submitted in (("1", False), ("2", True)):
            with self.subTest(submitted=submitted):
                _, control = await self.exp_control(user)

                async def fail(_, reserve):
                    if submitted:
                        await reserve()
                    raise ExpGrantError(uncertain=False)

                with patch("elo_bot.features.tokens.exp.grant_exp", new=fail):
                    await self.click(control, value="exp_potion", user=user)
                wallet = await TokenWallet.get(user)
                use = await TokenItemUse.find_one({"wallet_id": user, "slot": "exp"})
                self.assertEqual((wallet.inventory[TokenItem.EXP], use.state, use.active), (1, "returned", False))
                self.assertIn("potion was returned", self.edit()["content"])
                self.assertNotIn("Granting", self.edit()["content"])

    async def test_exp_unknown_outcome_blocks_grants_but_accepts_waiting_activations(self):
        wallet, control = await self.exp_control(quantity=2)
        grants = []

        async def unknown(user, reserve):
            await reserve()
            grants.append(user)
            raise ExpGrantError(uncertain=True)

        with patch("elo_bot.features.tokens.exp.grant_exp", new=unknown):
            await self.click(control, value="exp_potion")
            await self.click(control, value="exp_potion")
            next_control = self.edit()["components"][0]["components"][0]
            await self.click(next_control, value="exp_potion")
            self.assertEqual(await TokenItemUse.find({"wallet_id": "1", "slot": "exp", "state": "queued"}).count(), 1)
        use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp", "state": "reserved"})
        self.assertEqual(((await TokenWallet.get("1")).inventory[TokenItem.EXP], use.state, use.active),
                         (0, "reserved", True))
        self.assertEqual(grants, ["1"])
        self.assertIn("EXP Potion queued", self.edit()["content"])
        field = next(field for field in self.edit()["embeds"][0]["fields"] if "EXP Potion" in field["name"])
        self.assertIn("Pending", field["value"])
        self.assertIn("Queued", field["value"])
        options = [option for row in self.edit()["components"] for component in row["components"]
                   for option in component.get("options", [])]
        self.assertNotIn("exp_potion", [option["value"] for option in options])

    async def test_concurrent_exp_clicks_cannot_submit_twice(self):
        wallet, _ = await self.exp_control()
        prepared = asyncio.Event()
        preflights = []
        grants = []

        async def grant(user, reserve):
            preflights.append(user)
            if len(preflights) == 2:
                prepared.set()
            await prepared.wait()
            await reserve()
            grants.append(user)
            return "123456"

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            async with asyncio.timeout(5):
                outcomes = await asyncio.gather(redeem_exp(wallet, "concurrent"),
                                                redeem_exp(wallet, "concurrent"), return_exceptions=True)
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 1)
        self.assertEqual(grants, ["1"])
        self.assertEqual((await TokenItemUse.get("1:concurrent")).state, "used")

    async def test_exp_completion_write_failure_keeps_reservation_and_reports_actual_grant(self):
        _, control = await self.exp_control()

        async def grant(_, reserve):
            await reserve()
            return "123456"

        with (patch("elo_bot.features.tokens.exp.grant_exp", new=grant),
              patch("elo_bot.features.tokens.exp.finish_use", side_effect=OperationFailure("write failed"))):
            await self.click(control, value="exp_potion")
        use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp"})
        self.assertEqual(((await TokenWallet.get("1")).inventory[TokenItem.EXP], use.state, use.active),
                         (0, "reserved", True))
        self.assertIn("3,000 EXP was granted, but your potion is pending", self.edit()["content"])

    async def test_exp_reservation_failure_never_submits_or_claims_a_grant(self):
        _, control = await self.exp_control()
        submitted = []

        async def grant(user, reserve):
            await reserve()
            submitted.append(user)
            return "123456"

        with (patch("elo_bot.features.tokens.exp.grant_exp", new=grant),
              patch("elo_bot.features.tokens.exp.transaction", side_effect=OperationFailure("reserve failed"))):
            await self.click(control, value="exp_potion")
        use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp"})
        self.assertEqual((use.state, use.active), ("armed", True))
        self.assertEqual(submitted, [])
        self.assertIn("EXP was not submitted", self.edit()["content"])

    async def test_exp_delivery_failure_preserves_consumption_and_replay_does_not_grant_again(self):
        _, control = await self.exp_control()
        grants = []

        async def grant(user, reserve):
            await reserve()
            grants.append(user)
            return "123456"

        async def edit(*args, **kwargs):
            payload = args[0] if args else kwargs["payload"]
            if "Granted 3,000" in payload.get("content", ""):
                raise RuntimeError("Discord delivery failed")
            return {"id": "9001"}

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            self.http.edit_interaction_message.side_effect = edit
            with self.assertRaisesRegex(RuntimeError, "Discord delivery failed"):
                await self.click(control, value="exp_potion")
            use = await TokenItemUse.find_one({"wallet_id": "1", "slot": "exp"})
            self.assertEqual((use.state, use.active, use.target), ("used", False, "123456"))
            self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.EXP], 0)
            self.http.edit_interaction_message.side_effect = None
            await self.click(control, value="exp_potion")
        self.assertEqual(grants, ["1"])
        self.assertIn("Granted 3,000 server EXP", self.edit()["content"])

    async def active_control(self, control, use_id, *, user="1"):
        ctx = self.context(ipy.ComponentContext, {"custom_id": control["custom_id"], "component_type": 3,
                                                 "values": [use_id]}, user)
        await self.tokens.token_active_control.callback.func(self.tokens, ctx)

    async def test_active_items_disable_only_the_owners_exact_activation_once(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PRIORITY_PRO, 2, "9", "give-items"))
        use = await activate(wallet, TokenItem.PRIORITY_PRO, "first-item")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_items.callback.func(self.tokens, ctx)
        panel = self.edit()
        self.assertEqual(panel["embeds"][0]["title"], "**Activated Items**")
        self.assertEqual(panel["embeds"][0]["author"]["url"], "https://discord.com/users/1")
        control = panel["components"][0]["components"][0]
        await self.active_control(control, use.id, user="2")
        self.assertTrue((await TokenItemUse.get(use.id)).active)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        other_wallet = await ensure_wallet("2")
        with self.assertRaises(FinalError):
            await deactivate_item(other_wallet, use.id)
        with patch.object(self.http, "edit_interaction_message", side_effect=RuntimeError("Discord unavailable")):
            with self.assertRaisesRegex(RuntimeError, "Discord unavailable"):
                await self.active_control(control, use.id)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 2)
        self.assertFalse((await TokenItemUse.get(use.id)).active)
        await self.active_control(control, use.id)
        await self.active_control(control, use.id)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 2)
        self.assertFalse((await Player.get("1")).token_items_active)
        self.assertIn("Returned to inventory", self.edit()["content"])
        newer = await activate(wallet, TokenItem.PRIORITY_PRO, "new-item")
        await self.active_control(control, use.id)
        self.assertTrue((await TokenItemUse.get(newer.id)).active)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 1)

    async def test_active_menu_hides_reserved_and_exp_items_and_rejects_stale_disable(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PRIORITY_PRO, 1, "9", "give-boost"))
        boost = await activate(wallet, TokenItem.PRIORITY_PRO, "boost")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_items.callback.func(self.tokens, ctx)
        old_control = self.edit()["components"][0]["components"][0]
        player = await Player.get("1")
        async with QUEUE_OPERATION_LOCK:
            await reserve_queue_items(player)
        await adjust_items(ItemAdjustment("1", TokenItem.EXP, 1, "9", "give-exp"))
        exp = await activate(wallet, TokenItem.EXP, "pending-exp")
        await self.active_control(old_control, boost.id)
        self.assertIn("cannot be disabled", self.edit()["content"])
        self.assertEqual((await TokenItemUse.get(boost.id)).state, "reserved")
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 0)
        self.assertEqual(len(self.edit()["components"]), 1)
        self.assertEqual(self.edit()["components"][0]["components"][0]["label"], "Refresh")
        statuses = [field["value"] for field in self.edit()["embeds"][0]["fields"]]
        self.assertCountEqual(statuses, ["Reserved", "Pending"])
        with self.assertRaises(FinalError):
            await deactivate_item(wallet, exp.id)
        self.assertTrue((await TokenItemUse.get(exp.id)).active)

    async def test_active_item_pages_can_disable_a_specific_waiting_copy_without_changing_the_ready_one(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PRIORITY_PRO, 30, "9", "give-batch"))
        uses = [await activate(wallet, TokenItem.PRIORITY_PRO, f"batch:{index}") for index in range(30)]
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_items.callback.func(self.tokens, ctx)
        first = self.edit()
        self.assertEqual(len(first["components"][0]["components"][0]["options"]), 25)
        next_button = first["components"][-1]["components"][-1]
        page = self.context(ipy.ComponentContext, {"custom_id": next_button["custom_id"], "component_type": 2})
        await self.tokens.token_active_control.callback.func(self.tokens, page)
        menu = self.edit()["components"][0]["components"][0]
        self.assertEqual([option["value"] for option in menu["options"]], [use.id for use in uses[25:]])
        await self.active_control(menu, uses[-1].id)
        self.assertEqual((await TokenItemUse.get(uses[-1].id)).state, "returned")
        self.assertEqual((await TokenItemUse.get(uses[0].id)).state, "armed")
        self.assertEqual((await TokenItemUse.get(uses[-2].id)).state, "queued")
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 1)
        self.assertEqual(len(self.edit()["components"][0]["components"][0]["options"]), 4)

    async def test_disable_failure_rolls_back_item_return_and_never_announces_success(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PROTECTION, 1, "9", "give-protection"))
        use = await activate(wallet, TokenItem.PROTECTION, "ready-protection")
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_items.callback.func(self.tokens, ctx)
        control = self.edit()["components"][0]["components"][0]

        async def interrupted(use, session, **kwargs):
            await finish_use(use, session, **kwargs)
            raise OperationFailure("return interrupted")

        with patch("elo_bot.features.tokens.item_management.finish_use", new=interrupted):
            with self.assertRaises(OperationFailure):
                await self.active_control(control, use.id)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PROTECTION], 0)
        self.assertTrue((await TokenItemUse.get(use.id)).active)
        self.assertNotIn("Returned to inventory", self.edit().get("content", ""))

    async def test_disable_serializes_with_queue_entry_so_a_returned_boost_cannot_still_apply(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PRIORITY_PRO, 1, "9", "give-boost"))
        await adjust_items(ItemAdjustment("1", TokenItem.DISTRICT, 1, "9", "give-district"))
        boost = await activate(wallet, TokenItem.PRIORITY_PRO, "boost")
        await activate(wallet, TokenItem.DISTRICT, "district", district="Barbarian Camp")
        entered, release, queue_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def held_return(use, session, **kwargs):
            entered.set()
            await release.wait()
            return await finish_use(use, session, **kwargs)

        async def join():
            player = await Player.get("1")
            queue_started.set()
            async with QUEUE_OPERATION_LOCK:
                return await reserve_queue_items(player)

        with patch("elo_bot.features.tokens.item_management.finish_use", new=held_return):
            async with asyncio.timeout(10):
                disabling = asyncio.create_task(deactivate_item(wallet, boost.id))
                joining = None
                try:
                    await entered.wait()
                    joining = asyncio.create_task(join())
                    await queue_started.wait()
                    self.assertFalse(joining.done())
                finally:
                    release.set()
                    await disabling
                    if joining is not None:
                        attempt, boost_amount = await joining
        self.assertIsNotNone(attempt)
        self.assertEqual(boost_amount, 0)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 1)

    async def test_manager_item_grants_and_removals_are_audited_replay_safe_and_do_not_spend_tokens(self):
        wallet = await ensure_wallet("2")
        target = MagicMock(id=2)
        give = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, "9")
        give.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        await self.tokens.tokens_give_item.callback.func(self.tokens, give, target, "exp_potion", 3)
        retry = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, "9")
        retry.id = give.id
        retry.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        await self.tokens.tokens_give_item.callback.func(self.tokens, retry, target, "exp_potion", 3)
        remove = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, "9")
        remove.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        await self.tokens.tokens_remove_item.callback.func(self.tokens, remove, target, "exp_potion", 1)
        current = await TokenWallet.get(wallet.id)
        self.assertEqual((current.balance, current.inventory[TokenItem.EXP]), (5000, 2))
        entries = await TokenLedgerEntry.find({"wallet_id": wallet.id, "kind": "item_adjust"}).sort("created_at").to_list()
        self.assertCountEqual([(entry.actor_id, entry.item_quantity, entry.amount) for entry in entries],
                              [("9", 3, 0), ("9", -1, 0)])
        with self.assertRaises(FinalError):
            await adjust_items(ItemAdjustment("2", TokenItem.EXP, 4, "9", f"item-adjust:{give.id}"))
        self.assertEqual((await TokenWallet.get(wallet.id)).inventory[TokenItem.EXP], 2)
        self.assertIn("Removed 1", self.edit()["content"])
        history = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"}, "2")
        await self.tokens.tokens_history.callback.func(self.tokens, history)
        description = self.edit()["embeds"][0]["description"]
        self.assertIn("EXP Potion +3", description)
        self.assertIn("EXP Potion -1", description)
        self.assertNotIn(f"+0 {TOKEN_EMOJI}", description)

    async def test_manager_removal_cannot_take_active_items_or_overdraw_inventory(self):
        wallet = await adjust_items(ItemAdjustment("1", TokenItem.PROTECTION, 2, "9", "give"))
        active = await activate(wallet, TokenItem.PROTECTION, "protect")
        outcomes = await asyncio.gather(
            adjust_items(ItemAdjustment("1", TokenItem.PROTECTION, -1, "9", "remove-one")),
            adjust_items(ItemAdjustment("1", TokenItem.PROTECTION, -1, "9", "remove-two")), return_exceptions=True)
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 1)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PROTECTION], 0)
        self.assertTrue((await TokenItemUse.get(active.id)).active)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "item_adjust"}).count(), 2)

    async def test_manager_adjustment_write_failure_rolls_back_inventory_and_audit(self):
        wallet = await ensure_wallet("1")
        ledger = TokenLedgerEntry.get_pymongo_collection()
        with patch.object(ledger, "insert_one", side_effect=OperationFailure("audit unavailable")):
            with self.assertRaises(OperationFailure):
                await adjust_items(ItemAdjustment("1", TokenItem.EXP, 3, "9", "failed-grant"))
        self.assertEqual((await TokenWallet.get(wallet.id)).inventory.get(TokenItem.EXP, 0), 0)
        self.assertIsNone(await TokenLedgerEntry.get("failed-grant"))

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
        await self.click(self.edit()["components"][0]["components"][0], value="challenge_override")
        receipt = self.http.post_followup.await_args.args[0]
        code = receipt["content"].split("`")[1]
        use = await TokenItemUse.find_one({"target": code})
        self.assertEqual(receipt["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertIn("Used Challenge Override", self.edit()["content"])
        self.assertIn("Save", receipt["content"])
        self.assertIn(f"expires <t:{use.expires_at}:R>", receipt["content"])
        self.assertNotIn(code, json.dumps(self.edit(), default=str))

        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_inventory.callback.func(self.tokens, ctx, hidden=True)
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertNotIn(code, json.dumps(self.edit(), default=str))
        self.assertEqual(self.edit()["components"], [])
        ctx = self.context(ipy.SlashContext, {"id": "42", "name": "tokens"})
        await self.tokens.tokens_items.callback.func(self.tokens, ctx, hidden=True)
        self.assertNotIn(code, json.dumps(self.edit(), default=str))
        self.assertFalse(self.edit()["embeds"][0].get("fields"))

    async def test_hidden_inventory_for_another_player_only_allows_the_viewer_to_page(self) -> None:
        wallet = await ensure_wallet("2")
        await wallet.set({TokenWallet.inventory: dict.fromkeys((
            TokenItem.PRIORITY_PRO, TokenItem.PRIORITY_ULTRA, TokenItem.DISTRICT,
            TokenItem.CHALLENGE, TokenItem.PROTECTION,
        ), 1)})
        use = await activate(wallet, TokenItem.CHALLENGE, "inventory-other-code")
        await use.set({TokenItemUse.active: True})
        target = ipy.User.from_dict({"id": "2", "username": "james", "discriminator": "0", "avatar": None}, self.discord)
        self.discord.cache.place_user_data.return_value = target
        self.discord.cache.get_guild.return_value.fetch_member = AsyncMock(return_value=target)
        self.discord.cache.get_message.return_value.flags = ipy.MessageFlags.EPHEMERAL
        ctx = self.context(ipy.SlashContext, {
            "id": "42", "name": "tokens", "resolved": {"users": {"2": {"id": "2", "username": "james"}}},
            "options": [{"name": "inventory", "type": ipy.OptionType.SUB_COMMAND, "options": [
                {"name": "user", "type": ipy.OptionType.USER, "value": "2"},
                {"name": "hidden", "type": ipy.OptionType.BOOLEAN, "value": True},
            ]}],
        })

        await self.tokens.tokens_inventory(ctx)

        first = self.edit()
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"], ipy.MessageFlags.EPHEMERAL)
        self.assertEqual(first["embeds"][0]["title"], "**Tokens Inventory**")
        self.assertEqual(first["embeds"][0]["author"]["url"], "https://discord.com/users/2")
        self.assertNotIn("thumbnail", first["embeds"][0])
        self.assertEqual(len(first["components"]), 1)
        right = first["components"][0]["components"][2]
        self.http.edit_interaction_message.reset_mock()
        await self.click(right, user="2")
        self.http.edit_interaction_message.assert_not_awaited()
        await self.click(right)

        last = self.edit()
        self.assertEqual(last["embeds"][0]["title"], first["embeds"][0]["title"])
        self.assertEqual(last["embeds"][0]["author"], first["embeds"][0]["author"])
        self.assertEqual(len(last["components"]), 1)
        self.assertEqual(last["components"][0]["components"][1]["label"], "2/2")
        self.assertIn("Challenge Override", last["embeds"][0]["fields"][0]["name"])
        self.assertIn("Ready", last["embeds"][0]["fields"][0]["value"])
        self.assertNotIn(use.target, json.dumps(last, default=str))
        self.assertIsNone(await TokenWallet.get("1"))

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
        self.assertIn("thread update is pending", pending["content"])
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

    async def test_stake_button_matches_only_the_current_offer_for_the_opponent(self) -> None:
        match = await Match(token="stake-button", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        embed, _ = self.stake_announcement(match)
        await self.stake("1", "500")
        initial = self.stake_posts[700]
        old_button, pass_button = initial.components[0].components
        self.assertEqual(initial.content, f"<@2>, <@1> put **500** {TOKEN_EMOJI} at stake. "
                                          "Match the offer or play without a stake.")
        self.assertEqual((old_button.style, pass_button.style), (ipy.ButtonStyle.SUCCESS, ipy.ButtonStyle.SECONDARY))
        self.assertEqual(initial.allowed_mentions.to_dict(), {"users": [2], "replied_user": False})
        self.assertIn("Only the opponent", (await self.stake_reply(initial, old_button, user="1"))["content"])
        self.assertIn("Only the opponent", (await self.stake_reply(initial, old_button, user="3"))["content"])
        await self.stake("1", "600")
        self.assertEqual(initial.components, [])
        self.assertIn("no longer open", (await self.stake_reply(initial, old_button))["content"])
        current = self.stake_posts[701]
        accept = current.components[0].components[0]
        self.assertEqual((await ParticipantStake.get(str(match.id))).offer_message_id, current.id)
        self.assertIn("no longer open", (await self.stake_reply(initial, accept))["content"])
        self.assertIsNone(await TokenWallet.get("2"))

        accepted = await self.stake_reply(current, accept)

        self.assertIn(f"Stakes fixed at 600 {TOKEN_EMOJI} each", accepted["content"])
        self.assertEqual(current.components, [])
        self.assertIn(f"<@2> matches the stake with 600 {TOKEN_EMOJI}", embed.fields[0].value)
        self.assertIn("no longer open", (await self.stake_reply(current, accept))["content"])
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [4400, 4400])
        self.assertEqual(await TokenLedgerEntry.find({"kind": "match_hold"}).count(), 2)

    async def test_pass_closes_the_offer_without_moving_tokens(self) -> None:
        match = await Match(token="stake-pass", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        embed, _ = self.stake_announcement(match)
        await self.stake("1", "500")
        post = self.stake_posts[700]
        accept, decline = post.components[0].components

        response = await self.stake_reply(post, decline)

        self.assertIn("You passed on the stake", response["content"])
        self.assertEqual((await ParticipantStake.get(str(match.id))).state, "closed")
        self.assertEqual(post.components, [])
        self.assertEqual(embed.fields[0].value, "- Existing note.")
        self.assertIn("no longer open", (await self.stake_reply(post, accept))["content"])
        self.assertEqual((await TokenWallet.get("1")).balance, 5000)
        self.assertIsNone(await TokenWallet.get("2"))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "match_hold"}).count(), 0)

    async def test_stake_button_cannot_accept_a_reopened_offer_for_the_same_amount(self) -> None:
        match = await Match(token="stake-reopened", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        self.stake_announcement(match)
        await self.stake("1", "500")
        post = self.stake_posts[700]
        accept = post.components[0].components[0]
        await ensure_wallet("2")
        accepting = asyncio.Event()
        reopened = asyncio.Event()

        async def wallet_during_reply(user_id):
            if user_id == "2":
                accepting.set()
                await asyncio.wait_for(reopened.wait(), timeout=5)
            return await ensure_wallet(user_id)

        with patch("elo_bot.features.tokens.stakes.ensure_wallet", side_effect=wallet_during_reply):
            async with asyncio.TaskGroup() as group:
                reply = group.create_task(self.stake_reply(post, accept))
                await asyncio.wait_for(accepting.wait(), timeout=5)
                await self.stake("1", "0")
                await self.stake("1", "500")
                reopened.set()

        self.assertIn("no longer open", reply.result()["content"])
        state = await ParticipantStake.get(str(match.id))
        self.assertEqual((state.state, state.amount), ("offer", 500))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5000, 5000])
        self.assertEqual(await TokenLedgerEntry.find({"kind": "match_hold"}).count(), 0)

    async def test_stake_recovery_reuses_an_offer_post_after_discord_loses_the_response(self) -> None:
        match = await Match(token="stake-post-recovery", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        self.stake_announcement(match)
        send = self.stake_channel.send.side_effect

        async def lose_response(*args, **kwargs):
            await send(*args, **kwargs)
            raise ipy.errors.HTTPException(SimpleNamespace(status=503, reason="Unavailable"))

        self.stake_channel.send.side_effect = lose_response
        with self.assertLogs(level="ERROR"):
            response = await self.stake("1", "500")
        self.assertIn("thread update is pending", response["content"])
        self.assertIsNone((await ParticipantStake.get(str(match.id))).offer_message_id)

        await recover_stakes(self.discord)

        state = await ParticipantStake.get(str(match.id))
        self.assertFalse(state.publication_pending)
        self.assertEqual(state.offer_message_id, 700)
        self.stake_channel.send.assert_awaited_once()

    async def test_challenge_players_can_place_and_match_a_stake_before_start(self) -> None:
        match = await Match(token="challenge-stake", thread="100", thread_message_id=456, matched_time=1,
                            is_challenge=True, participants=[Participant(user_id="1", confirmed=True),
                                                             Participant(user_id="2", confirmed=True)]).insert()
        self.stake_announcement(match)
        offered = await self.stake("1", "500")
        self.assertIn("Stake offer sent.", offered["content"])
        post = self.stake_posts[700]
        accepted = await self.stake_reply(post, post.components[0].components[0])
        self.assertIn(f"Stakes fixed at 500 {TOKEN_EMOJI} each.", accepted["content"])
        self.assertEqual((await ParticipantStake.get(str(match.id))).state, "locked")
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [4500, 4500])

        await stake_window_closed(match)
        self.assertIn("Stakes close when the match starts.", (await self.stake("2", "500"))["content"])
        self.assertTrue(await delete_unreserved_match(await Match.get(match.id)))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5000, 5000])

    async def test_callback_rejects_matching_an_open_offer_after_start(self):
        match = await Match(token="late-stake", thread="100", thread_message_id=456, matched_time=1,
                            participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()
        embed, _ = self.stake_announcement(match)
        await self.stake("1", "500")
        post = self.stake_posts[700]
        accept = post.components[0].components[0]
        await stake_window_closed(match)
        late = await self.stake("2", "500")
        self.assertIn("Stakes close when the match starts.", late["content"])
        self.assertIn("Stakes close when the match starts.", (await self.stake_reply(post, accept))["content"])
        await recover_stakes(self.discord)
        self.assertEqual(post.components, [])
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

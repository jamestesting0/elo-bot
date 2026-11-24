import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

from beanie import PydanticObjectId
from pymongo import IndexModel
from pymongo.errors import DuplicateKeyError, OperationFailure

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import TokenItem
from elo_bot.db import Match, MatchPlayerStats, Participant, Player, RecordedMatch, Season
from elo_bot.db.token_models import TokenItemUse, TokenWallet
from elo_bot.features.finals.persistence import register_final_indexes, transaction
from elo_bot.features.tokens.challenge_items import accept_challenge, insert_challenge, owned_challenge_code
from elo_bot.features.tokens.exp import fulfill_exp, recover_exp, redeem_exp
from elo_bot.features.tokens.item_management import ItemAdjustment, adjust_items, deactivate_item
from elo_bot.features.tokens.item_view import active_item_panel
from elo_bot.features.tokens.items import activate, activate_many, finish_use, release_expired_codes
from elo_bot.features.tokens.queue_items import finish_district_items, insert_queued_match, release_queue_items, reserve_queue_items
from elo_bot.features.tokens.ranked import protect_result
from tests.integration._final_fixture import FinalScenarioCase


class TestItemActivationQueue(FinalScenarioCase):
    async def give(self, item, quantity):
        return await adjust_items(ItemAdjustment("1", item, quantity, "9", f"give:{item}"))

    def match(self, token):
        return Match(id=PydanticObjectId(), token=token, matched_time=1,
                     participants=[Participant(user_id="1"), Participant(user_id="2")])

    async def test_queue_and_district_activations_apply_one_at_a_time_in_activation_order(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 2)
        await self.give(TokenItem.PRIORITY_ULTRA, 1)
        await self.give(TokenItem.DISTRICT, 2)
        boosts = [await activate(wallet, item, f"boost:{index}") for index, item in enumerate(
            (TokenItem.PRIORITY_PRO, TokenItem.PRIORITY_ULTRA, TokenItem.PRIORITY_PRO))]
        districts = [await activate(wallet, TokenItem.DISTRICT, f"district:{index}", district=district)
                     for index, district in enumerate(("Barbarian Camp", "Golem Quarry"))]
        self.assertEqual([use.state for use in boosts], ["armed", "queued", "queued"])
        for index, expected_boost in enumerate((1, 2, 1)):
            attempt, boost = await reserve_queue_items(await Player.get("1"))
            self.assertEqual(boost, expected_boost)
            reserved = await TokenItemUse.find({"target": attempt, "state": "reserved"}).to_list()
            self.assertEqual([use.id for use in reserved if use.slot == "queue"], [boosts[index].id])
            expected_districts = [districts[index].id] if index < 2 else []
            self.assertEqual([use.id for use in reserved if use.slot == "district"], expected_districts)
            match = self.match(f"sequential:{index}")
            await insert_queued_match(match, [["Barbarian Camp", "Golem Quarry"], ["Goblin Mines"],
                                              ["Dragon Cliffs"]], (attempt, None))
            self.assertEqual((await TokenItemUse.get(boosts[index].id)).state, "used")
            await finish_district_items(match, consume=True)
        self.assertEqual(await TokenItemUse.find({"state": "used"}).count(), 5)
        self.assertEqual(await TokenItemUse.find({"active": True}).count(), 0)

    async def test_competing_activations_cannot_duplicate_or_overdraw_owned_copies(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 2)
        outcomes = await asyncio.gather(*(activate(wallet, TokenItem.PRIORITY_PRO, f"race:{index}")
                                          for index in range(5)), return_exceptions=True)
        self.assertEqual(sum(isinstance(outcome, TokenItemUse) for outcome in outcomes), 2)
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 3)
        rows = await TokenItemUse.find({"active": True}).sort("activation_order").to_list()
        self.assertEqual([row.state for row in rows], ["armed", "queued"])
        self.assertLess(rows[0].activation_order, rows[1].activation_order)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 0)
        replay = await activate(wallet, TokenItem.PRIORITY_PRO, rows[1].id.split(":", 1)[1])
        self.assertEqual(replay.id, rows[1].id)
        self.assertEqual(await TokenItemUse.find({"active": True}).count(), 2)

    async def test_bulk_activation_replays_one_atomic_batch_and_preserves_sequential_use(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 6)
        await self.give(TokenItem.PRIORITY_ULTRA, 1)
        earlier = await activate(wallet, TokenItem.PRIORITY_ULTRA, "earlier")
        first, replay = await asyncio.gather(*(activate_many(wallet, TokenItem.PRIORITY_PRO, "batch", amount=3)
                                              for _ in range(2)))
        self.assertEqual([use.id for use in first], [use.id for use in replay])
        self.assertEqual([use.state for use in first], ["queued"] * 3)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 3)
        with self.assertRaises(FinalError):
            await activate_many(wallet, TokenItem.PRIORITY_PRO, "batch", amount=2)
        with self.assertRaises(FinalError):
            await activate_many(wallet, TokenItem.PRIORITY_ULTRA, "batch", amount=3)
        await transaction(lambda session: finish_use(earlier, session, consume=True))
        for expected in first:
            attempt, boost = await reserve_queue_items(await Player.get("1"))
            self.assertEqual(boost, 1)
            current = await TokenItemUse.find_one({"target": attempt, "slot": "queue"})
            self.assertEqual(current.id, expected.id)
            await transaction(lambda session: finish_use(current, session, consume=True))
        replay = await activate_many(wallet, TokenItem.PRIORITY_PRO, "batch", amount=3)
        self.assertEqual([use.state for use in replay], ["used"] * 3)
        wallet = await TokenWallet.get("1")
        self.assertEqual((wallet.balance, wallet.inventory[TokenItem.PRIORITY_PRO]), (5000, 3))

    async def test_bulk_activation_rejects_invalid_requests_without_taking_any_owned_copies(self):
        wallet = await self.give(TokenItem.DISTRICT, 2)
        cases = ((TokenItem.DISTRICT, 3, "Barbarian Camp"), (TokenItem.DISTRICT, 1, None),
                 (TokenItem.PRIORITY_PRO, 1, "Barbarian Camp"), (TokenItem.DISTRICT, 0, "Barbarian Camp"),
                 (TokenItem.DISTRICT, 101, "Barbarian Camp"))
        for item, amount, district in cases:
            with self.subTest(item=item, amount=amount, district=district):
                with self.assertRaises(FinalError):
                    await activate_many(wallet, item, "invalid", amount=amount, district=district)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.DISTRICT], 2)
        self.assertEqual(await TokenItemUse.find_all().count(), 0)
        self.assertFalse((await Player.get("1")).token_items_active)

    async def test_partial_bulk_activation_insert_rolls_back_every_copy_and_can_be_retried(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 3)
        uses = TokenItemUse.get_pymongo_collection()
        insert = uses.insert_many

        async def partial(rows, *, session):
            await insert(rows[:1], session=session)
            raise OperationFailure("batch insert interrupted")

        with patch.object(uses, "insert_many", new=partial):
            with self.assertRaises(OperationFailure):
                await activate_many(wallet, TokenItem.PRIORITY_PRO, "partial", amount=3)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 3)
        self.assertEqual(await TokenItemUse.find_all().count(), 0)
        self.assertFalse((await Player.get("1")).token_items_active)
        complete = await activate_many(wallet, TokenItem.PRIORITY_PRO, "partial", amount=3)
        self.assertEqual([use.state for use in complete], ["armed", "queued", "queued"])
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 0)

    async def test_competing_bulk_activations_cannot_partially_fill_or_overdraw_inventory(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 5)
        outcomes = await asyncio.gather(
            activate_many(wallet, TokenItem.PRIORITY_PRO, "first-batch", amount=3),
            activate_many(wallet, TokenItem.PRIORITY_PRO, "second-batch", amount=3), return_exceptions=True)
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 1)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 2)
        self.assertEqual(await TokenItemUse.find_all().count(), 3)
        self.assertEqual(await TokenItemUse.find({"state": "armed"}).count(), 1)

    async def test_cancelled_queue_attempt_returns_only_reserved_copies_and_promotes_the_next_ones(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 2)
        await self.give(TokenItem.DISTRICT, 2)
        boosts = [await activate(wallet, TokenItem.PRIORITY_PRO, f"boost:{index}") for index in range(2)]
        districts = [await activate(wallet, TokenItem.DISTRICT, f"district:{index}", district="Barbarian Camp")
                     for index in range(2)]
        attempt, _ = await reserve_queue_items(await Player.get("1"))
        await release_queue_items(attempt)
        await release_queue_items(attempt)
        self.assertEqual([(await TokenItemUse.get(use.id)).state for use in (boosts[0], districts[0])],
                         ["returned", "returned"])
        self.assertEqual([(await TokenItemUse.get(use.id)).state for use in (boosts[1], districts[1])],
                         ["armed", "armed"])
        inventory = (await TokenWallet.get("1")).inventory
        self.assertEqual((inventory[TokenItem.PRIORITY_PRO], inventory[TokenItem.DISTRICT]), (1, 1))
        next_attempt, boost = await reserve_queue_items(await Player.get("1"))
        self.assertNotEqual(next_attempt, attempt)
        self.assertEqual(boost, 1)
        self.assertCountEqual([use.id for use in await TokenItemUse.find({"target": next_attempt}).to_list()],
                              [boosts[1].id, districts[1].id])

    async def test_legacy_active_slot_index_migrates_without_rewriting_existing_activations(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 2)
        first = await activate(wallet, TokenItem.PRIORITY_PRO, "legacy")
        uses = TokenItemUse.get_pymongo_collection()
        await uses.update_one({"_id": first.id}, {"$unset": {"batch_size": ""}})
        self.assertEqual((await activate(wallet, TokenItem.PRIORITY_PRO, "legacy")).id, first.id)
        await uses.drop_index("token_item_head")
        await uses.create_indexes([IndexModel([("wallet_id", 1), ("slot", 1)], unique=True,
                                              partialFilterExpression={"active": True}, name="token_active_item_slot")])
        before = await uses.find_one({"_id": first.id})
        await register_final_indexes()
        await register_final_indexes()
        self.assertEqual(await uses.find_one({"_id": first.id}), before)
        second = await activate(wallet, TokenItem.PRIORITY_PRO, "new")
        self.assertEqual(second.state, "queued")
        self.assertNotIn("token_active_item_slot", await uses.index_information())
        with self.assertRaises(DuplicateKeyError):
            await uses.update_one({"_id": second.id}, {"$set": {"state": "armed"}})
        self.assertEqual((await TokenItemUse.get(second.id)).state, "queued")

    async def test_issued_challenge_codes_can_expire_and_redeem_independently(self):
        wallet = await self.give(TokenItem.CHALLENGE, 3)
        first, second, third = await activate_many(wallet, TokenItem.CHALLENGE, "code", amount=3)
        self.assertFalse((await Player.get("1")).token_items_active)
        for use in (first, second, third):
            self.assertEqual((await owned_challenge_code("1", use.target)).id, use.id)
            self.assertGreater(use.expires_at, int(datetime.now(UTC).timestamp()))
        await TokenItemUse.get_pymongo_collection().update_one({"_id": first.id}, {"$set": {"expires_at": 0}})
        await release_expired_codes()
        self.assertIsNone(await owned_challenge_code("1", first.target))
        match = self.match("challenge-instant")
        await insert_challenge(match, second)
        self.assertEqual((await owned_challenge_code("1", third.target)).id, third.id)
        await accept_challenge(match)
        self.assertIsNone(await owned_challenge_code("1", second.target))
        self.assertEqual((await owned_challenge_code("1", third.target)).id, third.id)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.CHALLENGE], 1)

    async def test_legacy_queued_challenge_codes_receive_a_fresh_lifetime_when_their_turn_starts(self):
        wallet = await self.give(TokenItem.CHALLENGE, 3)
        first, second, third = await activate_many(wallet, TokenItem.CHALLENGE, "legacy-code", amount=3)
        await first.set({TokenItemUse.active: True})
        for use in (second, third):
            await use.set({TokenItemUse.active: True, TokenItemUse.state: "queued", TokenItemUse.target: None,
                           TokenItemUse.expires_at: None})
        self.assertIsNone(second.target)
        self.assertIsNone(second.expires_at)
        self.assertIsNone(third.target)
        await TokenItemUse.get_pymongo_collection().update_one({"_id": first.id}, {"$set": {"expires_at": 0}})
        await release_expired_codes()
        second = await TokenItemUse.get(second.id)
        self.assertEqual(second.state, "armed")
        self.assertGreater(second.expires_at, int(datetime.now(UTC).timestamp()))
        self.assertIsNone(await owned_challenge_code("1", first.target))
        self.assertEqual((await owned_challenge_code("1", second.target)).id, second.id)
        match = self.match("challenge-queue")
        await insert_challenge(match, second)
        self.assertEqual((await TokenItemUse.get(third.id)).state, "queued")
        await accept_challenge(match)
        third = await TokenItemUse.get(third.id)
        self.assertEqual(third.state, "armed")
        self.assertIsNotNone(third.target)
        self.assertNotEqual(third.target, second.target)
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.CHALLENGE], 1)

    def result(self, *, won):
        return RecordedMatch(id="sequence-result", time=1, players={
            "1": MatchPlayerStats(stars=9 if won else 6, destruction=300 if won else 240,
                                  opponent_stars=6 if won else 9, opponent_destruction=240 if won else 300,
                                  opponent="2", elo_change=25 if won else -30, won=won),
            "2": MatchPlayerStats(stars=6 if won else 9, destruction=240 if won else 300,
                                  opponent_stars=9 if won else 6, opponent_destruction=300 if won else 240,
                                  opponent="1", elo_change=-30 if won else 25, won=not won),
        })

    async def test_rank_protection_queue_keeps_the_season_limit_and_resumes_next_season(self):
        wallet = await self.give(TokenItem.PROTECTION, 3)
        first, second, third = [await activate(wallet, TokenItem.PROTECTION, f"protect:{index}") for index in range(3)]
        await Player.get_pymongo_collection().update_one({"_id": "1"}, {"$set": {"elo": 1210}})
        win = self.match("protected-win")
        await insert_queued_match(win, [], (None, None))
        await transaction(lambda session: protect_result(win, self.result(won=True), session))
        self.assertEqual((await TokenItemUse.get(first.id)).state, "returned")
        loss = self.match("protected-loss")
        await insert_queued_match(loss, [], (None, None))
        result = self.result(won=False)
        await transaction(lambda session: protect_result(loss, result, session))
        self.assertEqual(result.players["1"].elo_change, -10)
        self.assertEqual((await TokenItemUse.get(second.id)).state, "used")
        fourth = await activate(wallet, TokenItem.PROTECTION, "protect:extra")
        self.assertEqual(fourth.state, "queued")
        blocked = self.match("same-season")
        await insert_queued_match(blocked, [], (None, None))
        self.assertEqual(blocked.token_protections, {})
        self.assertEqual((await TokenItemUse.get(third.id)).state, "queued")
        await Season(season_number=7, start=3, end=4, players=[]).insert()
        next_season = self.match("next-season")
        await insert_queued_match(next_season, [], (None, None))
        self.assertEqual(next_season.token_protections, {"1": third.id})
        self.assertEqual((await TokenItemUse.get(fourth.id)).state, "queued")

    async def test_disabling_a_waiting_copy_preserves_fifo_and_large_batches_fit_discord_controls(self):
        wallet = await self.give(TokenItem.PRIORITY_PRO, 30)
        uses = [await activate(wallet, TokenItem.PRIORITY_PRO, f"batch:{index}") for index in range(30)]
        embed, rows = await active_item_panel(wallet)
        self.assertEqual(len(embed.fields), 1)
        self.assertIn("Queued: `29`", embed.fields[0].value)
        options = rows[0].to_dict()["components"][0]["options"]
        self.assertEqual(len(options), 25)
        _, second_page = await active_item_panel(wallet, page=1)
        later_options = second_page[0].to_dict()["components"][0]["options"]
        self.assertEqual([option["value"] for option in [*options, *later_options]], [use.id for use in uses])
        await deactivate_item(wallet, uses[1].id)
        await deactivate_item(wallet, uses[1].id)
        await transaction(lambda session: finish_use(uses[0], session, consume=True))
        self.assertEqual((await TokenItemUse.get(uses[2].id)).state, "armed")
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PRIORITY_PRO], 1)
        self.assertEqual(await TokenItemUse.find({"state": "armed"}).count(), 1)

    async def test_exp_copies_wait_for_confirmation_and_resume_sequentially_from_persisted_state(self):
        wallet = await self.give(TokenItem.EXP, 3)
        started, release = asyncio.Event(), asyncio.Event()
        granted = []

        async def grant(user, reserve):
            await reserve()
            granted.append(user)
            if len(granted) == 1:
                started.set()
                await release.wait()
            return str(len(granted))

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            async with asyncio.timeout(10):
                first = asyncio.create_task(redeem_exp(wallet, "first-exp"))
                try:
                    await started.wait()
                    second = await redeem_exp(wallet, "second-exp")
                    third = await redeem_exp(wallet, "third-exp")
                    self.assertEqual((second.state, third.state), ("queued", "queued"))
                    await recover_exp()
                    self.assertEqual(granted, ["1"])
                    await deactivate_item(wallet, third.id)
                finally:
                    release.set()
                    await first
            self.assertEqual((await TokenItemUse.get(second.id)).state, "armed")
            await recover_exp()
            await recover_exp()
        self.assertEqual(granted, ["1", "1"])
        self.assertEqual((await TokenItemUse.get(second.id)).state, "used")
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.EXP], 1)

    async def test_failed_exp_completion_does_not_promote_or_submit_a_waiting_copy(self):
        wallet = await self.give(TokenItem.EXP, 2)
        first = await activate(wallet, TokenItem.EXP, "first-exp")
        second = await activate(wallet, TokenItem.EXP, "second-exp")
        granted = []

        async def grant(user, reserve):
            await reserve()
            granted.append(user)
            return "123456"

        async def interrupted(use, session, **kwargs):
            await finish_use(use, session, **kwargs)
            raise OperationFailure("completion interrupted")

        with patch("elo_bot.features.tokens.exp.grant_exp", new=grant):
            with patch("elo_bot.features.tokens.exp.finish_use", new=interrupted):
                with self.assertRaises(FinalError):
                    await fulfill_exp(wallet, first)
            await recover_exp()
        self.assertEqual(granted, ["1"])
        self.assertEqual((await TokenItemUse.get(first.id)).state, "reserved")
        self.assertEqual((await TokenItemUse.get(second.id)).state, "queued")

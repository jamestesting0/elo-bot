import asyncio
from unittest.mock import AsyncMock, patch

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.finals.lifecycle import add_entrant, confirm_bracket, reset_bracket, final_for_view, save_pairing_draft, start_final
from elo_bot.core.finals import FinalError
from pymongo.errors import DuplicateKeyError

from elo_bot.db import EloFinal, Player
from elo_bot.db.final_models import FinalPlayerProfile


class TestFinalLifecycle(FinalScenarioCase):
    async def test_bracket_confirmation_and_reset_keep_durable_bye_counts(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        for user in final.entrant_ids:
            stats = (await FinalPlayerProfile.get(user)).editions[str(final.id)]
            self.assertEqual(stats.byes, sum(node.bye and node.winner == user for node in final.nodes.values()))
            self.assertEqual(stats.played, 0)
            self.assertEqual((await Player.get(user)).elo, 1000 - int(user))
        await reset_bracket(final)
        self.assertFalse((await final_for_view()).nodes)
        for user in final.entrant_ids:
            self.assertEqual((await FinalPlayerProfile.get(user)).editions[str(final.id)].byes, 0)

    async def test_failed_profile_write_does_not_confirm_half_a_bracket(self):
        final = await self.roster(5)
        with patch('elo_bot.features.finals.lifecycle.rebuild_profiles', AsyncMock(side_effect=RuntimeError("profile unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "profile unavailable"):
                await confirm_bracket(final, "manager")
        saved = await final_for_view()
        self.assertEqual(saved.state, "REGISTRATION")
        self.assertFalse(saved.nodes)
        self.assertEqual(await FinalPlayerProfile.count(), 0)

    async def test_recovery_repairs_missing_bye_projection_without_ranked_changes(self):
        from elo_bot.features.finals.recovery import recover_final
        final = await confirm_bracket(await self.roster(5), "manager")
        await FinalPlayerProfile.get_pymongo_collection().delete_many({})
        await final.set({"predictions_scoring_revision": -1})
        await recover_final(final)
        self.assertEqual(sum(profile.editions[str(final.id)].byes
                             for profile in await FinalPlayerProfile.find_all().to_list()), 3)
        self.assertEqual(sorted(player.elo for player in await Player.find_all().to_list()),
                         sorted(1000 - user for user in range(1, 10)))

    async def test_start_claims_the_ongoing_season_until_its_final_is_cancelled(self):
        final = await start_final(123, 456)
        self.assertEqual(final.season_number, self.archive.season_number + 1)
        with self.assertRaises(FinalError):
            await start_final(123, 456)
        await final.set({"state": "CANCELLING"})
        with self.assertRaises(FinalError):
            await start_final(123, 456)
        await final.set({"state": "CANCELLED", "active": False})
        restarted = await start_final(123, 456)
        self.assertEqual(restarted.season_number, final.season_number)
        self.assertEqual(await EloFinal.count(), 2)
        self.assertEqual((await final_for_view(final.season_number)).id, restarted.id)
        with self.assertRaises(DuplicateKeyError):
            await EloFinal(season_number=final.season_number, schema_version=2).insert()

    async def test_entrants_are_seeded_by_their_elo_when_added(self):
        final = await start_final(123, 456)
        final = await add_entrant(final, "3", "Player 3")
        await Player.find_one({"user_id": "3"}).set({"elo": 1500})
        self.assertEqual((await final_for_view()).seed_elo, {"3": 997})

    async def test_concurrent_eighth_and_ninth_entrant_yield_one_valid_roster(self):
        final = await self.roster(7)
        barrier = asyncio.Event()

        async def attempt(user):
            async with asyncio.timeout(5):
                await barrier.wait()
            try:
                return await add_entrant(final, user, user)
            except FinalError as error:
                return error

        tasks = [asyncio.create_task(attempt(user)) for user in ("8", "9")]
        barrier.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
        saved = await final_for_view()
        self.assertEqual(len(saved.entrant_ids), 8)
        self.assertEqual(sum(isinstance(outcome, FinalError) for outcome in outcomes), 1)
        self.assertEqual(set(saved.entrant_ids[:7]), set(map(str, range(1, 8))))

    async def test_manual_draft_stale_confirmation_preserves_winning_arrangement(self):
        final = await self.roster(8)
        pairs = {"QF1": ["8", "1"], "QF2": ["2", "7"], "QF3": ["3", "6"], "QF4": ["4", "5"]}
        draft = await save_pairing_draft(final, "manager", pairs)
        changed = await save_pairing_draft(draft, "manager", {**pairs, "QF1": ["1", "8"]})
        with self.assertRaises(FinalError):
            await confirm_bracket(draft, "manager", manual=True)
        saved = await confirm_bracket(changed, "manager", manual=True)
        self.assertEqual(saved.nodes["QF1"].contestants, ["1", "8"])
        self.assertEqual(saved.state, "BRACKET_READY")

    async def test_draft_owner_and_roster_revision_are_rechecked(self):
        final = await self.roster(5)
        pairs = {"QF1": ["1", None], "QF2": ["4", "5"], "QF3": ["2", None], "QF4": ["3", None]}
        draft = await save_pairing_draft(final, "manager", pairs)
        with self.assertRaises(FinalError):
            await save_pairing_draft(draft, "other", pairs)
        changed = await add_entrant(draft, "6", "Six")
        with self.assertRaises(FinalError):
            await confirm_bracket(changed, "manager", manual=True)
        self.assertFalse((await final_for_view()).nodes)


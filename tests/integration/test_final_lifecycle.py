import asyncio
from unittest.mock import AsyncMock, patch

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.finals.lifecycle import add_entrant, confirm_bracket, reset_bracket, resolve_final, save_pairing_draft, start_final
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, Player
from elo_bot.db.final_models import FinalPlayerProfile


class TestFinalLifecycle(FinalScenarioCase):
    async def test_bracket_confirmation_and_reset_keep_durable_bye_counts(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        for user in final.entrant_ids:
            stats = (await FinalPlayerProfile.get(user)).editions[str(final.id)]
            self.assertEqual(stats.byes, sum(node.bye and node.winner == user for node in final.nodes.values()))
            self.assertEqual(stats.played, 0)
            self.assertEqual((await Player.get(user)).elo, 2000)
        await reset_bracket(final)
        self.assertFalse((await resolve_final()).nodes)
        for user in final.entrant_ids:
            self.assertEqual((await FinalPlayerProfile.get(user)).editions[str(final.id)].byes, 0)

    async def test_failed_profile_write_does_not_confirm_half_a_bracket(self):
        final = await self.roster(5)
        with patch('elo_bot.features.finals.lifecycle.rebuild_profiles', AsyncMock(side_effect=RuntimeError("profile unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "profile unavailable"):
                await confirm_bracket(final, "manager")
        saved = await resolve_final()
        self.assertEqual(saved.state, "REGISTRATION")
        self.assertFalse(saved.nodes)
        self.assertEqual(await FinalPlayerProfile.count(), 0)

    async def test_recovery_repairs_missing_bye_projection_without_ranked_changes(self):
        from elo_bot.features.finals.recovery import recover_final
        final = await confirm_bracket(await self.roster(5), "manager")
        await FinalPlayerProfile.get_pymongo_collection().delete_many({})
        await recover_final(final)
        self.assertEqual(sum(profile.editions[str(final.id)].byes
                             for profile in await FinalPlayerProfile.find_all().to_list()), 3)
        self.assertTrue(all(player.elo == 2000 for player in await Player.find_all().to_list()))

    async def test_start_captures_completed_archive_and_duplicate_start_cannot_claim(self):
        final = await start_final(None, 123, 456)
        self.assertEqual(final.season_id, self.archive.id)
        self.assertEqual(final.season_number, 7)
        with self.assertRaises(FinalError):
            await start_final(None, 123, 456)
        self.assertEqual(await EloFinal.count(), 1)

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
        saved = await resolve_final()
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
        self.assertFalse((await resolve_final()).nodes)


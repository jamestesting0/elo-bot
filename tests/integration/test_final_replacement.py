from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.config.ids import STAFF_CHANNEL
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.match_models import Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.finals.dashboard import DashboardState, dashboard_controls
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.recovery import select_financial_work
from elo_bot.features.finals.replacement import STARTED, replace_entrant, replaceable_entrants
from elo_bot.features.tokens.bets import open_market
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalReplacement(FinalScenarioCase):
    async def bracket(self, count=8):
        return await confirm_bracket(await self.roster(count), "manager")

    async def test_replacement_takes_the_slot_seed_and_predictions(self):
        final = await self.bracket()
        node = next(node for node in final.nodes.values() if "8" in node.contestants)
        side = node.contestants.index("8")
        await FinalPrediction(id=f"{final.id}:5", final_id=str(final.id), season_number=final.season_number,
                              user_id="5", setup_revision=final.setup_revision,
                              draft={node.id: "8"}, submitted={node.id: "8", "F1": "8", "SF1": "1"}).insert()

        replaced = await replace_entrant(final, "8", "9", "Player 9")

        saved = await EloFinal.get(final.id)
        self.assertEqual(saved.nodes[node.id].contestants[side], "9")
        self.assertEqual(saved.nodes[node.id].contestant_revision, node.contestant_revision + 1)
        self.assertEqual((replaced.node_id, replaced.thread_id, replaced.round_started), (node.id, None, False))
        self.assertEqual(sorted(saved.entrant_ids), [str(user) for user in range(1, 10) if user != 8])
        self.assertEqual((saved.seed_elo.get("8"), saved.seed_elo["9"]), (None, 991))
        self.assertTrue(saved.publication_pending)
        prediction = await FinalPrediction.get(f"{final.id}:5")
        self.assertEqual(prediction.draft, {node.id: "9"})
        self.assertEqual(prediction.submitted, {node.id: "9", "F1": "9", "SF1": "1"})

    async def started_pairing(self):
        final = await self.bracket()
        node_id = next(key for key, node in final.nodes.items() if "8" in node.contestants)
        source = await self.scheduled_pairing(final, node_id)
        await (await EloFinal.get(final.id)).set({f"nodes.{node_id}.thread_id": "777"})
        await source.set({"thread": "777", "thread_message_id": 555})
        return final, node_id, source

    async def test_replacement_after_the_round_started_keeps_the_match_and_refunds_betting(self):
        final, node_id, source = await self.started_pairing()
        market = await open_market(await EloFinal.get(final.id), node_id)

        replaced = await replace_entrant(await EloFinal.get(final.id), "8", "9", "Player 9")

        self.assertEqual((replaced.thread_id, replaced.round_started), ("777", True))
        saved, match = await EloFinal.get(final.id), await Match.get(source.id)
        node = saved.nodes[node_id]
        self.assertEqual((node.match_id, node.thread_id), (source.id, "777"))
        self.assertFalse(match.final_superseded)
        self.assertEqual([participant.user_id for participant in match.participants], node.contestants)
        self.assertEqual(match.final_contestant_revision, node.contestant_revision)
        await select_financial_work(final.id)
        self.assertTrue((await FinalBettingMarket.get(market.id)).desired_refund)

    async def test_stored_bases_must_be_undone_before_replacing(self):
        final, _, source = await self.started_pairing()
        await source.set({"participants.0.opponent_bases_stored": True})
        with self.assertRaises(FinalError):
            await replace_entrant(await EloFinal.get(final.id), "8", "9", "Player 9")
        self.assertIn("8", (await EloFinal.get(final.id)).entrant_ids)

    async def test_started_matches_and_later_rounds_cannot_be_replaced(self):
        final = await self.bracket()
        node_id = next(key for key, node in final.nodes.items() if "8" in node.contestants)
        await (await EloFinal.get(final.id)).set({f"nodes.{node_id}.first_actual_started_at": 1})
        with self.assertRaises(FinalError) as refused:
            await replace_entrant(await EloFinal.get(final.id), "8", "9", "Player 9")
        self.assertEqual(str(refused.exception), STARTED)
        for leaving, joining in (("42", "9"), ("1", "2")):
            with self.subTest(leaving=leaving), self.assertRaises(FinalError):
                await replace_entrant(await EloFinal.get(final.id), leaving, joining, "Player")
        self.assertNotIn("8", replaceable_entrants(await EloFinal.get(final.id)))

    async def test_a_bye_player_is_replaced_in_their_semifinal(self):
        final = await self.bracket(7)
        bye = next(node for node in final.nodes.values() if node.bye)
        player = bye.winner
        semifinal = next(node for node in final.nodes.values() if bye.id in node.feeders)

        replaced = await replace_entrant(final, player, "9", "Player 9")

        saved = await EloFinal.get(final.id)
        self.assertEqual((saved.nodes[bye.id].winner, replaced.node_id), ("9", semifinal.id))
        self.assertIn("9", saved.nodes[semifinal.id].contestants)
        self.assertEqual(saved.nodes[semifinal.id].contestant_revision, semifinal.contestant_revision + 1)

    async def test_replacing_after_round_start_updates_the_thread_and_tells_staff(self):
        final, node_id, _ = await self.started_pairing()
        opening_embed = ipy.Embed().set_image("https://cdn.discordapp.com/attachments/777/1/final-matchup.png")
        old_art = SimpleNamespace(filename="final-matchup.png")
        other_file = SimpleNamespace(filename="rules.txt")
        opening = MagicMock(content="<@1>\n<@8>\n<@&5>", edit=AsyncMock(),
                            embeds=[opening_embed], attachments=[old_art, other_file])
        opening._channel_id = 777
        thread = MagicMock(send=AsyncMock(), add_member=AsyncMock(), remove_member=AsyncMock(),
                           fetch_message=AsyncMock(return_value=opening))
        thread.name = f"🟡 {node_id} · Player 1 vs. Player 8"
        staff = MagicMock(send=AsyncMock())
        bot = MagicMock(ext={}, fetch_channel=AsyncMock(side_effect=lambda channel: staff if channel == STAFF_CHANNEL
                                                         else thread))

        def username(user):
            view = MagicMock(id=user)
            view.__str__.return_value = f"user{user}"
            view.display_avatar.fetch = AsyncMock(return_value=b"")
            return view

        bot.fetch_user = AsyncMock(side_effect=username)
        cog = FinalCommands(bot)
        guild = MagicMock(fetch_member=AsyncMock(side_effect=lambda user: MagicMock(
            id=int(user), display_name=f"Player {user}", has_role=MagicMock(return_value=False))))
        with patch.object(FinalCommands, "_recover", new=AsyncMock(return_value=[])), \
                patch.object(FinalCommands, "_provision_node", new=AsyncMock(return_value=("777", True, []))) as provision, \
                patch("elo_bot.extensions.final.roster.schedule_channel_name_edit") as rename:
            await cog._replace(await EloFinal.get(final.id), guild, "8", "9")
        self.assertEqual(rename.call_args.args[1:], ("🟡", f"{node_id} · user1 vs. user9"))
        self.assertEqual(opening.edit.await_args.kwargs["content"], "<@1>\n<@9>\n<@&5>")
        self.assertEqual(opening.edit.await_args.kwargs["attachments"], [other_file])
        self.assertEqual(opening.edit.await_args.kwargs["embeds"][0].images, [])
        self.assertIsNone(opening.edit.await_args.kwargs.get("file"))
        thread.add_member.assert_awaited_once_with(9)
        thread.remove_member.assert_awaited_once_with(8)
        self.assertEqual(thread.send.await_args.kwargs["allowed_mentions"].users, [9])
        self.assertEqual(staff.send.await_args.kwargs["allowed_mentions"].parse, [])
        self.assertEqual(provision.await_args.args[2], node_id)

    async def test_dashboard_offers_replace_only_while_a_quarterfinal_entrant_is_replaceable(self):
        final = await self.bracket()
        labels = [button.label for button in dashboard_controls(DashboardState(final), "9")[0].components]
        self.assertIn("Replace Player", labels)
        await final.set({f"nodes.{key}.first_actual_started_at": 1 for key in ("QF1", "QF2", "QF3", "QF4")})
        rows = dashboard_controls(DashboardState(await EloFinal.get(final.id)), "9")
        self.assertNotIn("Replace Player", [button.label for row in rows for button in row.components])

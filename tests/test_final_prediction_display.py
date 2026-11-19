import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import MAIN_GUILD
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, EloFinalMatch, Match
from elo_bot.db.final_models import FinalMatchResult, FinalPrediction, FinalResultSnapshot
from elo_bot.extensions.final import FinalCommands
from elo_bot.features.finals.bracket import automatic_pairs, build_bracket
from elo_bot.features.finals.rendering import render_bracket


class TestFinalPredictionView(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = FinalCommands(MagicMock(ext={}))

    async def test_submitted_prediction_is_sent_privately_with_real_graphic(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
                                         entrant_ids=[str(i) for i in range(8)],
                                         player_nicknames={str(i): f"Player {i}" for i in range(8)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        picks = {"QF1": "0", "QF2": "3", "QF3": "1", "QF4": "2",
                 "SF1": "3", "SF2": "2", "F1": "2"}
        prediction = FinalPrediction.model_construct(submitted=picks, points=2, eligible=3)
        self.cog._edition = AsyncMock(return_value=final)
        response = {}

        async def send(**payload):
            response.update(payload)
            response["image_bytes"] = payload["file"].file.read()

        async def find_prediction(query):
            if query == {"final_id": str(final.id), "user_id": "99", "submitted_at": {"$ne": None}}:
                return prediction
            return None

        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock(side_effect=send))
        ctx.author.has_role.return_value = False
        ctx.author.id = 99
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(side_effect=find_prediction)):
            await self.cog.final_predict.callback.func(self.cog, ctx, 13)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(response["ephemeral"])
        self.assertEqual(response["image_bytes"], render_bracket(final, picks, {}))
        self.assertNotEqual(response["image_bytes"], render_bracket(final))
        self.assertEqual(response["embed"].fields[0].value, "`2/3` → `66.67%`")

    async def test_missing_prediction_remains_private_error(self):
        self.cog._edition = AsyncMock(return_value=EloFinal.model_construct(id=PydanticObjectId()))
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=None)):
            await self.cog.final_predict.callback.func(self.cog, ctx, 13)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_prediction_attachment_describes_picks_and_statuses_within_discord_limit(self):
        for long_names in (False, True):
            with self.subTest(long_names=long_names):
                final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
                    entrant_ids=[str(i) for i in range(8)],
                    player_nicknames={str(i): (f"Player {i}" + "😀" * 80 if long_names else f"Player {i}")
                                      for i in range(8)})
                final.nodes = build_bracket(final, automatic_pairs(final))
                for node_id, winner, outcome in (("QF1", "0", "Played"), ("QF2", "3", "Played"),
                                                 ("QF3", "1", "Forfeit")):
                    node = final.nodes[node_id]
                    node.result_id, node.winner, node.outcome = node_id, winner, outcome
                final.nodes["QF4"].bye = True
                picks = {"QF1": "0", "QF2": "4", "QF3": "1", "QF4": "2",
                         "SF1": "0", "SF2": "1", "F1": "0"}
                prediction = FinalPrediction.model_construct(submitted=picks)
                self.cog._edition = AsyncMock(return_value=final)
                ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
                ctx.author.has_role.return_value = False
                ctx.guild.fetch_member = AsyncMock(return_value=None)
                query = MagicMock(to_list=AsyncMock(return_value=[]))
                with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                        patch.object(FinalMatchResult, "find", return_value=query), \
                        patch('elo_bot.features.finals.prediction_board.render_bracket', return_value=b"image"):
                    await self.cog.final_predict.callback.func(self.cog, ctx, 13)
                payload = ctx.send.await_args.kwargs
                description = payload["file"].description
                self.assertTrue(payload["ephemeral"])
                self.assertLessEqual(len(description.encode("utf-16-le")) // 2, 1024)
                lines = description.splitlines()
                for node, name, status in (("QF1", "Player 0", "Correct"), ("QF2", "Player 4", "Incorrect"),
                                           ("QF3", "Player 1", "Excluded"), ("QF4", "Player 2", "Excluded"),
                                           ("SF1", "Player 0", "Pending"), ("SF2", "Player 1", "Pending"),
                                           ("F1", "Player 0", "Pending")):
                    line = next(line for line in lines if line.startswith(f"{node}:"))
                    self.assertIn(name, line)
                    self.assertIn(status, line)
                self.assertIsNone(payload["embed"].description)

    async def test_unavailable_graphic_preserves_private_prediction_and_score(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, entrant_ids=["1", "2"], nodes={
            "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
        })
        prediction = FinalPrediction.model_construct(submitted={"QF1": "1"}, points=1, eligible=1)
        self.cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch('elo_bot.features.finals.prediction_board.render_bracket', side_effect=FinalError("Image unavailable.")):
            await self.cog.final_predict.callback.func(self.cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["embed"].fields[0].value, "`1/1` → `100.00%`")
        self.assertEqual(payload["embed"].fields[-2].value, "Image unavailable.")
        self.assertIn("QF1:", payload["embed"].fields[-1].value)

    async def test_official_results_use_accepted_start_and_skip_forfeit_scores(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
            entrant_ids=[str(i) for i in range(8)],
            player_nicknames={str(i): f"Player {i}" for i in range(8)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        for node_id, result_id, winner, outcome in (
                ("QF1", "played", "0", "Played"), ("QF2", "forfeit", "1", "Forfeit")):
            node = final.nodes[node_id]
            node.result_id, node.winner, node.outcome = result_id, winner, outcome
        played = FinalMatchResult.model_construct(id="played", node_id="QF1", source_match_id=None,
            snapshot=SimpleNamespace(contestants=["0", "7"], winner="0", outcome="Played",
                scores={"0": ["3.100"] * 7, "7": ["2.50"] * 7}, actual_started_at=1234567890))
        forfeited = FinalMatchResult.model_construct(id="forfeit", node_id="QF2", source_match_id=None,
            snapshot=FinalResultSnapshot(contestants=["1", "6"], winner="1", outcome="Forfeit",
                scores={"1": ["0.0"] * 7, "6": ["0.0"] * 7}))
        prediction = FinalPrediction.model_construct(submitted={"QF1": "7", "QF2": "1"}, points=0, eligible=1)
        self.cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[forfeited, played]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query):
            await self.cog.final_predict.callback.func(self.cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        text = payload["embed"].description
        self.assertIn("<t:1234567890:R>", text)
        self.assertIn("21<:cocstar:826130043251982416> 700%", text)
        self.assertIn("14<:cocstar:826130043251982416> 350%", text)
        self.assertIn("Forfeit", text)
        self.assertEqual(text.count("<:cocstar:826130043251982416>"), 2)

    async def test_stale_prediction_totals_match_current_official_picks(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, result_revision=3,
            entrant_ids=[str(i) for i in range(8)], player_nicknames={})
        final.nodes = build_bracket(final, automatic_pairs(final))
        for node_id, winner, outcome in (("QF1", "0", "Played"), ("QF2", "3", "Played"),
                                         ("QF3", "1", "Forfeit")):
            node = final.nodes[node_id]
            node.result_id, node.winner, node.outcome = node_id, winner, outcome
        prediction = FinalPrediction.model_construct(submitted={"QF1": "0", "QF2": "4", "QF3": "1"},
            points=9, eligible=9, scoring_revision=2)
        self.cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query):
            await self.cog.final_predict.callback.func(self.cog, ctx, 13)
        self.assertEqual(ctx.send.await_args.kwargs["embed"].fields[0].value, "`1/2` → `50.00%`")

    async def test_unknown_official_start_stays_unknown_before_and_after_source_removal(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
            entrant_ids=[str(i) for i in range(8)], player_nicknames={})
        final.nodes = build_bracket(final, automatic_pairs(final))
        final.nodes["QF1"].result_id, final.nodes["QF1"].winner = "played", "0"
        final.nodes["QF1"].outcome = "Played"
        source_id = PydanticObjectId()
        source = Match.model_construct(final_id=str(final.id), final_node_id="QF1",
            final_contestant_revision=2, final_attempt=3, final_started_at=1800000000)
        snapshot_data = {"contestants": ["0", "7"], "winner": "0",
                         "scores": {"0": ["3.100"] * 7, "7": ["2.50"] * 7}}
        prediction = FinalPrediction.model_construct(submitted={"QF1": "0"})
        self.cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        for timestamp in ({"actual_started_at": None}, {}):
            with self.subTest(snapshot=timestamp):
                snapshot = FinalResultSnapshot.model_validate({**snapshot_data, **timestamp})
                result = FinalMatchResult.model_construct(id="played", node_id="QF1",
                    source_match_id=str(source_id), contestant_revision=2, attempt=3, snapshot=snapshot)
                query = MagicMock(to_list=AsyncMock(return_value=[result]))
                with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                        patch.object(FinalMatchResult, "find", return_value=query), \
                        patch.object(Match, "get", new=AsyncMock(return_value=source)) as get_source:
                    for current_source in (source, None):
                        get_source.return_value = current_source
                        await self.cog.final_predict.callback.func(self.cog, ctx, 13)
                        payload = ctx.send.await_args.kwargs
                        self.assertTrue(payload["ephemeral"])
                        self.assertNotIn("<t:", payload["embed"].description)
                        self.assertIn("21<:cocstar:826130043251982416>", payload["embed"].description)
                    get_source.assert_not_awaited()

    async def test_recorded_start_survives_source_removal(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
            entrant_ids=[str(i) for i in range(8)], player_nicknames={})
        final.nodes = build_bracket(final, automatic_pairs(final))
        node = final.nodes["QF1"]
        node.result_id, node.winner, node.outcome = "played", "0", "Played"
        source_id = PydanticObjectId()
        result = FinalMatchResult.model_construct(id="played", node_id="QF1", source_match_id=str(source_id),
            contestant_revision=2, attempt=3,
            snapshot=FinalResultSnapshot(contestants=["0", "7"], winner="0",
                scores={"0": ["3.100"] * 7, "7": ["2.50"] * 7}, actual_started_at=1234567890))
        prediction = FinalPrediction.model_construct(submitted={"QF1": "0"})
        source = Match.model_construct(final_id=str(final.id), final_node_id="QF1",
            final_contestant_revision=2, final_attempt=3, final_started_at=1234567890)
        self.cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[result]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query), \
                patch.object(Match, "get", new=AsyncMock(return_value=source)) as get_source:
            for current_source in (source, None):
                get_source.return_value = current_source
                await self.cog.final_predict.callback.func(self.cog, ctx, 13)
                self.assertIn("<t:1234567890:R>", ctx.send.await_args.kwargs["embed"].description)
            get_source.assert_not_awaited()

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elobot.features.finals.publication import publish_bracket
from elobot.features.finals.bracket import automatic_pairs, build_bracket
from elobot.features.finals.recovery import publish_result
from elobot.features.finals.rendering import render_bracket
from elobot.extensions.final import FinalCommands
from elobot.features.finals.types import FinalError
from elobot.db import EloFinal, EloFinalMatch
from elobot.db.final_models import FinalMatchResult, FinalPrediction, FinalResultSnapshot
from elobot.db.season_models import FinalResultProgress
from elobot.db import Match


class TestFinalBracketAcknowledgement(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledges_before_lookup_render_and_upload(self):
        events = []
        cog = FinalCommands(MagicMock(ext={}))
        edition = MagicMock()
        embed = ipy.Embed(title="Season 13 Final · Bracket")

        async def defer():
            events.append("acknowledged")

        async def lookup(ctx, season):
            events.append("loaded")
            return edition

        def render(final):
            self.assertIs(final, edition)
            events.append("rendered")
            return b"bracket-image"

        async def send(**payload):
            events.append("sent")
            self.assertIs(payload["embed"], embed)
            self.assertEqual(payload["file"].file.read(), b"bracket-image")

        cog._edition = AsyncMock(side_effect=lookup)
        ctx = MagicMock(defer=AsyncMock(side_effect=defer), send=AsyncMock(side_effect=send))
        with patch("elobot.features.finals.publication.bracket_embed", return_value=embed), \
                patch("elobot.features.finals.rendering.render_bracket", side_effect=render):
            await cog.final_bracket_view.callback.func(cog, ctx, 13)
        self.assertEqual(events, ["acknowledged", "loaded", "rendered", "sent"])
        ctx.defer.assert_awaited_once_with()

    async def test_missing_image_keeps_acknowledged_text_bracket(self):
        cog = FinalCommands(MagicMock(ext={}))
        edition = EloFinal.model_construct(season_number=13, nodes={
            "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
        })
        cog._edition = AsyncMock(return_value=edition)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A") if user == "1" else None,
        )
        with patch("elobot.features.finals.rendering.render_bracket", side_effect=FinalError("Image unavailable.")):
            await cog.final_bracket_view.callback.func(cog, ctx, 13)
        ctx.defer.assert_awaited_once_with()
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertEqual(embed.description,
                         "QF1: **[Player A](<https://discordapp.com/users/1>)** vs. "
                         "**[❌Invalid Player](<https://discordapp.com/users/2>)**"
                         " → **[Player A](<https://discordapp.com/users/1>)**")
        self.assertEqual(embed.fields[0].value, "Image unavailable.")


class TestFinalPredictionView(unittest.IsolatedAsyncioTestCase):
    async def test_submitted_prediction_is_sent_privately_with_real_graphic(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
                                         entrant_ids=[str(i) for i in range(8)],
                                         player_nicknames={str(i): f"Player {i}" for i in range(8)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        picks = {"QF1": "0", "QF2": "3", "QF3": "1", "QF4": "2",
                 "SF1": "3", "SF2": "2", "F1": "2"}
        prediction = FinalPrediction.model_construct(submitted=picks, points=2, eligible=3)
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        response = {}

        async def send(**payload):
            response.update(payload)
            response["image_bytes"] = payload["file"].file.read()

        async def find_prediction(query):
            if query == {"final_id": str(final.id), "user_id": "99"}:
                return prediction
            return None

        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(side_effect=send))
        ctx.author.id = 99
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(side_effect=find_prediction)):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertTrue(response["ephemeral"])
        self.assertEqual(response["image_bytes"], render_bracket(final, picks, {}))
        self.assertNotEqual(response["image_bytes"], render_bracket(final))
        self.assertEqual(response["embed"].fields[0].value, "2/3 → 66.67%")

    async def test_missing_prediction_remains_private_error(self):
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=EloFinal.model_construct(id=PydanticObjectId()))
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=None)):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("/final predict", ctx.send.await_args.args[0])

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
                cog = FinalCommands(MagicMock(ext={}))
                cog._edition = AsyncMock(return_value=final)
                ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
                ctx.guild.fetch_member = AsyncMock(return_value=None)
                query = MagicMock(to_list=AsyncMock(return_value=[]))
                with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                        patch.object(FinalMatchResult, "find", return_value=query), \
                        patch("elobot.features.finals.rendering.render_bracket", return_value=b"image"):
                    await cog.final_prediction_view.callback.func(cog, ctx, 13)
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
                self.assertNotIn("QF1:", payload["embed"].description)

    async def test_unavailable_graphic_preserves_private_prediction_and_score(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, nodes={
            "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
        })
        prediction = FinalPrediction.model_construct(submitted={"QF1": "1"}, points=1, eligible=1)
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch("elobot.features.finals.rendering.render_bracket", side_effect=FinalError("Image unavailable.")):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["embed"].fields[0].value, "1/1 → 100.00%")
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
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[forfeited, played]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        text = payload["embed"].description
        self.assertIn("<t:1234567890:R>", text)
        self.assertIn("21<:cocstar:826130043251982416> 700%", text)
        self.assertIn("14<:cocstar:826130043251982416> 350%", text)
        self.assertIn("Forfeit", text)
        self.assertEqual(text.count("<:cocstar:826130043251982416>"), 2)

    async def test_official_tie_is_displayed_without_win_loss_markers(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13,
            entrant_ids=[str(i) for i in range(8)], player_nicknames={})
        final.nodes = build_bracket(final, automatic_pairs(final))
        node = final.nodes["QF1"]
        node.result_id, node.winner, node.outcome, node.state = "tied", None, "Played", "TIED"
        result = FinalMatchResult.model_construct(id="tied", node_id="QF1", source_match_id=None,
            snapshot=SimpleNamespace(contestants=["0", "7"], winner=None, outcome="Played",
                scores={"0": ["3.100"] * 7, "7": ["3.100"] * 7}, actual_started_at=1234567890))
        prediction = FinalPrediction.model_construct(submitted={"QF1": "0"})
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[result]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        text = payload["embed"].description
        self.assertTrue(payload["ephemeral"])
        self.assertIn("<t:1234567890:R>", text)
        self.assertIn("Tie", text)
        self.assertEqual(text.count("21<:cocstar:826130043251982416> 700%"), 2)
        self.assertNotIn("<:upkey:", text)
        self.assertNotIn("<:downkey:", text)
        self.assertEqual(payload["embed"].fields[0].value, "0/0 → 0.00%")

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
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        self.assertEqual(ctx.send.await_args.kwargs["embed"].fields[0].value, "1/2 → 50.00%")

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
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
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
                        await cog.final_prediction_view.callback.func(cog, ctx, 13)
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
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        query = MagicMock(to_list=AsyncMock(return_value=[result]))
        with patch.object(FinalPrediction, "find_one", new=AsyncMock(return_value=prediction)), \
                patch.object(FinalMatchResult, "find", return_value=query), \
                patch.object(Match, "get", new=AsyncMock(return_value=source)) as get_source:
            for current_source in (source, None):
                get_source.return_value = current_source
                await cog.final_prediction_view.callback.func(cog, ctx, 13)
                self.assertIn("<t:1234567890:R>", ctx.send.await_args.kwargs["embed"].description)
            get_source.assert_not_awaited()


class TestFinalPublicationNames(unittest.IsolatedAsyncioTestCase):
    async def test_bracket_send_and_refresh_resolve_members_and_missing_players(self):
        for message_id in (None, "99"):
            with self.subTest(message_id=message_id):
                final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, channel_id=7,
                    bracket_message_id=message_id, nodes={
                        "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
                    })
                message = MagicMock(id=99, pin=AsyncMock(), edit=AsyncMock())
                channel = MagicMock(send=AsyncMock(return_value=message),
                                    fetch_message=AsyncMock(return_value=message))
                channel.guild.fetch_member = AsyncMock(
                    side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A") if user == "1" else None,
                )
                bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
                collection = MagicMock(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
                with patch("elobot.features.finals.publication.resolve_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection), \
                        patch("elobot.features.finals.publication.render_bracket", return_value=b"bracket-image"):
                    await publish_bracket(bot, final)
                publication = message.edit if message_id else channel.send
                self.assertEqual(publication.await_args.kwargs["embed"].description,
                                 "QF1: **[Player A](<https://discordapp.com/users/1>)** vs. "
                                 "**[❌Invalid Player](<https://discordapp.com/users/2>)**"
                                 " → **[Player A](<https://discordapp.com/users/1>)**")

    async def test_result_send_refresh_and_deleted_thread_use_publication_guild_names(self):
        for message_id, deleted in ((None, False), ("99", False), (None, True)):
            with self.subTest(message_id=message_id, deleted=deleted):
                result = FinalMatchResult.model_construct(id="result1", node_id="QF1", source_thread="8",
                    snapshot=FinalResultSnapshot(contestants=["1", "2"], winner="1",
                        scores={"1": ["3.100"] * 7, "2": ["3.100"] * 7}))
                final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, channel_id=7,
                    result_progress={result.id: FinalResultProgress(message_id=message_id)})
                message = MagicMock(id=99, edit=AsyncMock())
                channel = MagicMock(send=AsyncMock(return_value=message),
                                    fetch_message=AsyncMock(return_value=message))
                channel.guild.fetch_member = AsyncMock(
                    side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A") if user == "1" else None,
                )
                bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
                if deleted:
                    bot.fetch_channel.side_effect = [
                        ipy.errors.NotFound(MagicMock(status=404, reason="Not Found")), channel,
                    ]
                collection = MagicMock(update_one=AsyncMock())
                with patch("elobot.features.finals.recovery.resolve_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection):
                    await publish_result(bot, final, result)
                publication = message.edit if message_id else channel.send
                embed = publication.await_args.kwargs["embed"]
                self.assertTrue(embed.description.startswith(
                    "**[Player A](<https://discordapp.com/users/1>)** vs. "
                    "**[❌Invalid Player](<https://discordapp.com/users/2>)**\n\n"))
                self.assertEqual(embed.fields[1].value, "**[Player A](<https://discordapp.com/users/1>)**")

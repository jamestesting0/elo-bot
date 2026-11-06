import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from common.finals.publication import publish_bracket
from common.finals.bracket import automatic_pairs, build_bracket
from common.finals.recovery import publish_result
from common.finals.rendering import render_bracket
from cogs.final_commands import FinalCommands
from common.finals.types import FinalError
from database_models import EloFinal, EloFinalMatch
from database_models.final_models import FinalMatchResult, FinalPrediction, FinalResultSnapshot
from database_models.season_models import FinalResultProgress


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
        with patch("common.finals.publication.bracket_embed", return_value=embed), \
                patch("common.finals.rendering.render_bracket", side_effect=render):
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
        with patch("common.finals.rendering.render_bracket", side_effect=FinalError("Image unavailable.")):
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
        self.assertEqual(response["image_bytes"], render_bracket(final, picks))
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
                patch("common.finals.rendering.render_bracket", side_effect=FinalError("Image unavailable.")):
            await cog.final_prediction_view.callback.func(cog, ctx, 13)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["embed"].fields[0].value, "1/1 → 100.00%")
        self.assertEqual(payload["embed"].fields[-1].value, "Image unavailable.")


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
                with patch("common.finals.publication.resolve_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection), \
                        patch("common.finals.publication.render_bracket", return_value=b"bracket-image"):
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
                with patch("common.finals.recovery.resolve_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection):
                    await publish_result(bot, final, result)
                publication = message.edit if message_id else channel.send
                embed = publication.await_args.kwargs["embed"]
                self.assertTrue(embed.description.startswith(
                    "**[Player A](<https://discordapp.com/users/1>)** vs. "
                    "**[❌Invalid Player](<https://discordapp.com/users/2>)**\n\n"))
                self.assertEqual(embed.fields[1].value, "**[Player A](<https://discordapp.com/users/1>)**")

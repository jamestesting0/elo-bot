import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.extensions.final import FinalCommands
from elo_bot.core.finals import FinalError
from elo_bot.config.ids import MAIN_GUILD
from elo_bot.config.emojis import WIN_EMOJI, LOSS_EMOJI
from elo_bot.db import EloFinal, EloFinalMatch
from tests._patch_sites import patch_sites


class TestFinalBracketAcknowledgement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        count = patch("elo_bot.extensions.final.views.submitted_predictions", new=AsyncMock(return_value=0))
        count.start()
        self.addCleanup(count.stop)

    async def test_acknowledges_before_lookup_render_and_upload(self):
        events = []
        cog = FinalCommands(MagicMock(ext={}))
        edition = MagicMock(entrant_ids=[], nodes={"QF1": EloFinalMatch(id="QF1")})
        embed = ipy.Embed(title="Season 13 Final · Bracket")

        async def defer(**kwargs):
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
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(side_effect=defer), send=AsyncMock(side_effect=send))
        ctx.author.has_role.return_value = False
        with patch_sites(('elo_bot.features.finals.publication.bracket_embed', 'elo_bot.extensions.final.views.bracket_embed'), return_value=embed), \
                patch('elo_bot.extensions.final.views.render_bracket', side_effect=render):
            await cog.final_bracket_view.callback.func(cog, ctx, 13)
        self.assertEqual(events, ["acknowledged", "loaded", "rendered", "sent"])
        ctx.defer.assert_awaited_once_with(ephemeral=False)

    async def test_setup_view_lists_entrants_privately_without_an_image_or_result_select(self):
        final = EloFinal.model_construct(season_number=13, entrant_ids=["1", "2"])
        cog = FinalCommands(MagicMock(ext={}))
        cog._edition = AsyncMock(return_value=final)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(side_effect=lambda user: SimpleNamespace(id=int(user), display_name=f"Player {user}"))
        with patch("elo_bot.extensions.final.views.render_bracket", return_value=b"unused"):
            await cog.final_bracket_view.callback.func(cog, ctx, 13, hidden=True)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        payload = ctx.send.await_args.kwargs
        self.assertNotIn("file", payload)
        self.assertEqual(payload["components"], [])
        self.assertEqual(payload["embed"].fields, [])
        for user in final.entrant_ids:
            self.assertIn(f"https://discordapp.com/users/{user}", payload["embed"].description)

    async def test_missing_image_keeps_acknowledged_text_bracket(self):
        cog = FinalCommands(MagicMock(ext={}))
        edition = EloFinal.model_construct(season_number=13, entrant_ids=["1", "2"], nodes={
            "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
        })
        cog._edition = AsyncMock(return_value=edition)
        ctx = MagicMock(guild_id=MAIN_GUILD, defer=AsyncMock(), send=AsyncMock())
        ctx.author.has_role.return_value = False
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A") if user == "1" else None,
        )
        with patch('elo_bot.extensions.final.views.render_bracket', side_effect=FinalError("Image unavailable.")):
            await cog.final_bracket_view.callback.func(cog, ctx, 13)
        ctx.defer.assert_awaited_once_with(ephemeral=False)
        ctx.send.assert_awaited_once()
        embed = ctx.send.await_args.kwargs["embed"]
        self.assertIn("https://discordapp.com/users/1", embed.fields[0].value)
        self.assertIn(str(WIN_EMOJI), embed.fields[0].value)
        self.assertIn(str(LOSS_EMOJI), embed.fields[0].value)
        self.assertEqual(embed.fields[-1].value, "Image unavailable.")

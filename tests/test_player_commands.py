import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock, PropertyMock
import sys
import os
import interactions as ipy

# Add project root to sys.path
sys.path.append(os.getcwd())

from elobot.extensions.player import PlayerCommands
from elobot.config.emojis import ERROR_EMOJI, TOKEN_EMOJI
from elobot.config.ids import PLAYER_ROLE
from tests._patch_sites import patch_sites

class TestPlayerCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        wallet = SimpleNamespace(id="123", balance=5000, user_id="123", identity_claims=["123"])
        self.wallet_grant = self.enterContext(patch("elobot.features.tokens.wallets.ensure_wallet", new=AsyncMock(return_value=wallet)))
        self.deregister_write = self.enterContext(patch("elobot.features.finals.administration.deregister_player", new=AsyncMock()))
        self.enterContext(patch("elobot.features.tokens.wallets.wallet_summary", new=AsyncMock(return_value=(5000, 0, 0))))
        self.bot = MagicMock()
        self.bot.ext = {}
        self.bot.coc_client = MagicMock()
        self.bot.coc_client.get_player = AsyncMock()
        self.bot.owner = MagicMock()
        self.bot.owner.id = 999
        self.bot.match_id_choices = {}
        
        self.cog = PlayerCommands(self.bot)

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_player_capital_peak_persists_preference(self, mock_player):
        # Given: a registered player invoking /player capital-peak
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.send = AsyncMock()
        player = MagicMock(capital_peak_enabled=True)
        player.save_changes = AsyncMock()
        mock_player.find_one = AsyncMock(return_value=player)

        # When: the player disables Capital Peak
        await self.cog.player_capital_peak.callback(ctx, enabled=False)

        # Then: the preference is stored and the interaction is acknowledged privately
        self.assertFalse(player.capital_peak_enabled)
        player.save_changes.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_player_capital_peak_rejects_unregistered_user(self, mock_player):
        # Given: an unregistered user invoking /player capital-peak
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.send = AsyncMock()
        mock_player.find_one = AsyncMock(return_value=None)

        # When: the user tries to change the preference
        await self.cog.player_capital_peak.callback(ctx, enabled=False)

        # Then: the callback returns the standard registration error privately
        self.assertIn("not registered", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_player_register_success(self, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.user.id = 123
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.add_role = AsyncMock()
        ctx.send = AsyncMock()
        
        # Mock player not registered
        mock_player.find_one = AsyncMock(return_value=None)
        mock_player.return_value.insert = AsyncMock()
        
        # Mock coc player
        mock_coc_player = MagicMock()
        mock_coc_player.tag = "#TAG"
        self.bot.coc_client.get_player.return_value = mock_coc_player
        
        # Execute
        await self.cog.player_register.callback(ctx, player_tag="#TAG")

        # Verify
        mock_player.assert_called() # Constructor called
        mock_player.return_value.insert.assert_called_once()
        ctx.author.add_role.assert_called_with(PLAYER_ROLE)
        ctx.send.assert_called()
        self.assertIn("registered", ctx.send.call_args[0][0])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_player_deregister_success(self, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.has_any_role.return_value = True
        ctx.send = AsyncMock()
        
        target_user = MagicMock(spec=ipy.User)
        target_user.id = 123
        target_user.mention = "@User"
        
        # Mock player found
        mock_player_doc = MagicMock()
        mock_player_doc.delete = AsyncMock()
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)
        
        # Execute
        await self.cog.player_deregister.callback(ctx, player=target_user)

        # Verify
        self.deregister_write.assert_awaited_once_with("123")
        ctx.send.assert_called()
        self.assertIn("deregistered", ctx.send.call_args[0][0])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.Season")
    async def test_player_profile_success(self, mock_season, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        # Mock player found
        mock_player_doc = MagicMock()
        mock_player_doc.elo = 1000
        mock_player_doc.highest_elo = 1100
        mock_player_doc.matches = []
        mock_player_doc.tag = "#TAG"
        mock_player_doc.wins = 10
        mock_player_doc.avg_stars = 2.5
        mock_player_doc.avg_destruction = 80.0
        mock_player_doc.opponent_avg_stars = 2.0
        mock_player_doc.opponent_avg_destruction = 70.0
        mock_player.find_one = AsyncMock(return_value=mock_player_doc)
        mock_player.find.return_value.count = AsyncMock(return_value=5) # Rank
        
        # Mock Player.elo > elo comparison
        mock_player.elo = MagicMock()
        mock_player.elo.__gt__ = MagicMock(return_value=True)
        
        # Mock coc player
        mock_coc_player = MagicMock()
        mock_coc_player.name = "TestPlayer"
        mock_coc_player.tag = "#TAG"
        mock_coc_player.share_link = "http://link"
        
        # Mock seasons
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[
            SimpleNamespace(season_number=3, start=300, end=400),
            SimpleNamespace(season_number=7, start=700, end=800),
        ])

        # Execute
        with patch("elobot.extensions.player.PlayerCommands.coc_client", new_callable=PropertyMock) as mock_coc_client_prop:
            mock_coc_client_prop.return_value.get_player = AsyncMock(return_value=mock_coc_player)
            await self.cog.player_profile.callback(ctx, user=None, hidden=False)

        # Verify
        ctx.send.assert_called()
        embed = ctx.send.call_args[1]['embed']
        self.assertIn("TestPlayer", embed.description)
        self.assertTrue(embed.description.endswith(f"**Rank:** #6 in the server\n**Tokens:** 5,000 {TOKEN_EMOJI}"))
        self.assertNotIn("__**Tokens**__", [field.name for field in embed.fields])
        season_options = ctx.send.call_args[1]['components'].options
        self.assertEqual(
            [(option.label, option.value) for option in season_options],
            [("All Seasons", "All Seasons"), ("Current Season", "Current Season"), ("Season 3", "3"),
             ("Season 7", "7")],
        )

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.Season")
    async def test_player_profile_in_dm_names_recent_opponents_from_main_server(self, mock_season, mock_player):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild = None
        ctx.bot = self.bot
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        main_guild = MagicMock()
        main_guild.fetch_member = AsyncMock(return_value=SimpleNamespace(id=55, display_name="Kelly"))
        self.bot.fetch_guild = AsyncMock(return_value=main_guild)
        stats = {"123": SimpleNamespace(opponent="55", elo_change=-40, stars=6, destruction=244),
                 "55": SimpleNamespace(opponent="123", elo_change=40, stars=6, destruction=249)}
        player_doc = MagicMock(elo=1000, highest_elo=1100, tag="#TAG", wins=0, avg_stars=0, avg_destruction=0,
                               opponent_avg_stars=0, opponent_avg_destruction=0,
                               matches=[SimpleNamespace(id="1", time=1761900000, players=stats)])
        mock_player.find_one = AsyncMock(return_value=player_doc)
        mock_player.find.return_value.count = AsyncMock(return_value=0)
        mock_player.elo = MagicMock()
        mock_player.elo.__gt__ = MagicMock(return_value=True)
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
        coc_player = MagicMock(tag="#TAG", share_link="http://link")
        coc_player.name = "TestPlayer"

        with patch("elobot.extensions.player.PlayerCommands.coc_client", new_callable=PropertyMock) as coc_client:
            coc_client.return_value.get_player = AsyncMock(return_value=coc_player)
            await self.cog.player_profile.callback.func(self.cog, ctx, user=None, hidden=False)

        main_guild.fetch_member.assert_awaited_once_with("55")
        recent = next(field.value for field in ctx.send.await_args.kwargs["embed"].fields
                      if field.name == "__**Recent Matches**__")
        self.assertTrue(recent.startswith("vs. **[Kelly](<https://discordapp.com/users/55>)** ("))
        self.assertIn("<t:1761900000:R>", recent)

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_player_h2h_success(self, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        player1 = MagicMock(spec=ipy.User)
        player1.id = 123
        player1.avatar.url = "http://avatar1"
        player1.__str__.return_value = "Player1"
        
        player2 = MagicMock(spec=ipy.User)
        player2.id = 456
        player2.__str__.return_value = "Player2"
        
        # Mock players found
        mock_p1_doc = MagicMock()
        mock_p1_doc.matches = []
        mock_player.find_one = AsyncMock(side_effect=[mock_p1_doc, MagicMock()]) # p1 found, p2 found
        
        # Mock match against player2
        mock_match = MagicMock()
        mock_match.players = {
            "123": MagicMock(opponent="456", won=True, stars=3, destruction=100, districts={"D1": MagicMock(), "D2": MagicMock(), "D3": MagicMock()}, elo_change=10),
            "456": MagicMock(opponent="123", won=False, stars=1, destruction=50, districts={"D1": MagicMock(), "D2": MagicMock(), "D3": MagicMock()}, elo_change=-10)
        }
        mock_match.id = "1"
        mock_match.time = 1600000000
        mock_p1_doc.matches = [mock_match]

        # Execute
        await self.cog.player_h2h.callback(ctx, player1=player1, player2=player2, hidden=False)

        # Verify
        ctx.send.assert_called()
        embed = ctx.send.call_args[1]['embed']
        self.assertIn("Player1's Record Against Player2", embed.title)
        self.assertIn("1 - 0", embed.description)

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.construct_match_embed")
    async def test_player_matches_view_success(self, mock_construct_match_embed, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild = MagicMock()
        ctx.send = AsyncMock()
        
        user = MagicMock(spec=ipy.User)
        user.id = 123
        
        # Mock player found with matches
        mock_p_doc = MagicMock()
        mock_match = MagicMock()
        mock_match.id = "1"
        mock_p_doc.matches = [mock_match]
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock construct_match_embed
        mock_embed = MagicMock(spec=ipy.Embed)
        mock_construct_match_embed.return_value = mock_embed

        # Execute
        await self.cog.player_matches_view.callback(ctx, user=user, hidden=False)

        # Verify
        mock_construct_match_embed.assert_called_with(unittest.mock.ANY, ctx.guild, "1", "123")
        ctx.send.assert_called()
        self.assertEqual(ctx.send.call_args[1]['embed'], mock_embed)

    @patch("elobot.extensions.player.construct_match_embed", new_callable=AsyncMock)
    async def test_player_matches_search_uses_requested_visibility_and_hides_inconsistent_data(self, construct):
        user = MagicMock(spec=ipy.User)
        user.id = 123
        embed = MagicMock(spec=ipy.Embed)
        construct.return_value = embed
        for hidden in (False, True):
            with self.subTest(hidden=hidden):
                ctx = MagicMock(spec=ipy.SlashContext)
                ctx.send = AsyncMock()

                await self.cog.player_matches_search.callback.func(
                    self.cog, ctx, user=user, match_id="7", hidden=hidden,
                )

                construct.assert_awaited_with(self.cog.bot, ctx.guild, "7", "123")
                ctx.send.assert_awaited_once_with(embed=embed, ephemeral=hidden)

        construct.side_effect = KeyError("7")
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()

        await self.cog.player_matches_search.callback.func(self.cog, ctx, user=user, match_id="7", hidden=False)

        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertNotIn("embed", ctx.send.await_args.kwargs)
        self.assertIn(str(ERROR_EMOJI), ctx.send.await_args.args[0])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.Season")
    async def test_player_hitrates_success(self, mock_season, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        user = MagicMock(spec=ipy.User)
        user.id = 123
        user.__str__.return_value = "Player1"
        
        # Mock player found with matches
        mock_p_doc = MagicMock()
        mock_match = MagicMock()
        mock_match.time = 1600000000
        mock_match.players = {
            "123": MagicMock(districts={
                "Barbarian Camp": MagicMock(stars=3, destruction=100),
                "Wizard Valley": MagicMock(stars=2, destruction=80)
            })
        }
        mock_p_doc.matches = [mock_match]
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock seasons
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[
            SimpleNamespace(season_number=3, start=300, end=400),
            SimpleNamespace(season_number=7, start=700, end=800),
        ])

        # Execute
        await self.cog.player_hitrates.callback(ctx, user=user, season="All Seasons", hidden=False)

        # Verify
        ctx.send.assert_called()
        embed = ctx.send.call_args[1]['embed']
        self.assertIn("Player1's Hitrates", embed.title)
        self.assertIn("Barbarian Camp", embed.description)
        self.assertIn("Wizard Valley", embed.description)
        season_options = ctx.send.call_args[1]['components'].options
        self.assertEqual(
            [(option.label, option.value) for option in season_options],
            [("All Seasons", "All Seasons"), ("Current Season", "Current Season"), ("Season 3", "3"),
             ("Season 7", "7")],
        )

    @patch("elobot.extensions.player.Queue")
    @patch("elobot.extensions.player.Match")
    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    async def test_elo_select_success(self, mock_player, mock_match, mock_queue):
        # Setup
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.values = ["1050"]
        ctx.send = AsyncMock()
        
        # Mock queue (empty)
        mock_queue.find_all.return_value.to_list = AsyncMock(return_value=[])
        
        # Mock mixed_queue (empty)
        self.cog.bot.ext = {} # Reset ext
        # Mock match (not in match)
        mock_match.find.return_value.exists = AsyncMock(return_value=False)

        # Mock player found
        mock_p_doc = MagicMock()
        mock_p_doc.matches = []
        mock_p_doc.save_changes = AsyncMock()
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)

        # Execute
        await self.cog.elo_select.callback(ctx)

        # Verify
        mock_p_doc.save_changes.assert_called_once()
        self.assertEqual(mock_p_doc.elo, 1050)
        self.assertEqual(mock_p_doc.highest_elo, 1050)
        ctx.send.assert_called()
        self.assertIn("1050", ctx.send.call_args[0][0])

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.Season")
    async def test_stats_select_success(self, mock_season, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.custom_id = "stats_select|123"
        ctx.values = ["All Seasons"]
        ctx.edit_origin = AsyncMock()
        ctx.message.embeds = [MagicMock()]
        ctx.message.embeds[0].fields = [MagicMock()]
        
        # Mock player found
        mock_p_doc = MagicMock()
        mock_p_doc.matches = []
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock seasons
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])

        # Execute
        await self.cog.stats_select.callback(ctx)

        # Verify
        ctx.edit_origin.assert_called()
        self.assertIn("Statistics (All Seasons)", ctx.message.embeds[0].fields[0].name)

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.construct_match_embed")
    async def test_match_right_button_success(self, mock_construct_match_embed, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.custom_id = "match_right|123|0"
        ctx.edit_origin = AsyncMock()
        
        # Mock player found with matches
        mock_p_doc = MagicMock()
        mock_match1 = MagicMock()
        mock_match1.id = "1"
        mock_match2 = MagicMock()
        mock_match2.id = "2"
        # Sorted by ID descending: [match2, match1]
        # Index 0 is match2 (ID "2")
        # Index 1 is match1 (ID "1")
        mock_p_doc.matches = [mock_match1, mock_match2]
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock construct_match_embed
        mock_embed = MagicMock(spec=ipy.Embed)
        mock_construct_match_embed.return_value = mock_embed

        # Execute
        # Requesting index 0 -> next is index 1 -> match1 (ID "1")
        await self.cog.match_right_button.callback(ctx)

        # Verify
        mock_construct_match_embed.assert_called_with(unittest.mock.ANY, ctx.guild, "1", "123")
        ctx.edit_origin.assert_called()

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.construct_match_embed")
    async def test_match_left_button_success(self, mock_construct_match_embed, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.custom_id = "match_left|123|1"
        ctx.edit_origin = AsyncMock()
        
        # Mock player found with matches
        mock_p_doc = MagicMock()
        mock_match1 = MagicMock()
        mock_match1.id = "1"
        mock_match2 = MagicMock()
        mock_match2.id = "2"
        # Sorted by ID descending: [match2, match1]
        # Index 0 is match2 (ID "2")
        # Index 1 is match1 (ID "1")
        mock_p_doc.matches = [mock_match1, mock_match2]
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock construct_match_embed
        mock_embed = MagicMock(spec=ipy.Embed)
        mock_construct_match_embed.return_value = mock_embed

        # Execute
        # Requesting index 1 -> prev is index 0 -> match2 (ID "2")
        await self.cog.match_left_button.callback(ctx)

        # Verify
        mock_construct_match_embed.assert_called_with(unittest.mock.ANY, ctx.guild, "2", "123")
        ctx.edit_origin.assert_called()

    @patch_sites(('elobot.extensions.player.Player', 'elobot.features.players.match_choices.Player'))
    @patch("elobot.extensions.player.Season")
    async def test_hitrates_select_success(self, mock_season, mock_player):
        # Setup
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author.id = 123
        ctx.message.interaction.user = 123
        ctx.custom_id = "hitrates_select|123"
        ctx.values = ["All Seasons"]
        ctx.edit_origin = AsyncMock()
        ctx.message.embeds = [MagicMock()]
        ctx.message.embeds[0].fields = [MagicMock()]
        
        # Mock player found
        mock_p_doc = MagicMock()
        mock_p_doc.matches = []
        mock_player.find_one = AsyncMock(return_value=mock_p_doc)
        
        # Mock seasons
        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])

        # Execute
        await self.cog.hitrates_select.callback(ctx)

        # Verify
        ctx.edit_origin.assert_called()
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "All Seasons")

if __name__ == "__main__":
    unittest.main()

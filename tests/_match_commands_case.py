import sys
import os
sys.path.append(os.getcwd())

import asyncio
import random
import unittest
from unittest.mock import MagicMock, AsyncMock, patch
import interactions as ipy

from elo_bot.extensions.match import MatchCommands
from elo_bot.features.matches.attack_bases import choose_bases
from elo_bot.db import Player, Queue
from elo_bot.config.ids import BOT_ID
from tests._patch_sites import patch_sites
import elo_bot.features.matches.resources as resources_module




class MatchCommandsCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def enable_base_store_clear(match: MagicMock) -> None:
        def clear(index: int, reset_stored_flag: bool = False) -> bool:
            match.participants[index].base_store = None
            if reset_stored_flag:
                match.participants[index].opponent_bases_stored = False
            return True

        match.clear_base_store = AsyncMock(side_effect=clear)

    @staticmethod
    def enable_base_store_message_binding(match: MagicMock) -> None:
        match.result_operation = None
        match.autowin_winner_id = None

        def attach(index: int, message_id: int, *, ready: bool = True) -> bool:
            store = match.participants[index].base_store
            if store is None:
                return False
            store.message_id = message_id
            store.ready = ready
            return True

        match.attach_base_store_message = AsyncMock(side_effect=attach)
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1),
        )

    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.owner = AsyncMock()
        self.bot.owner.id = 123456789
        self.bot.owner.send = AsyncMock()
        self.bot.user.id = BOT_ID
        self.bot.fetch_guild = AsyncMock()
        mock_user = MagicMock()
        mock_user.send = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=mock_user)
        self.bot.fetch_channel = AsyncMock()
        self.bot.ext = {}
        self.bot.coc_client = MagicMock()
        self.bot.coc_client.get_clan = AsyncMock()
        for klass in MatchCommands.__mro__[:MatchCommands.__mro__.index(ipy.Extension)]:
            for command in vars(klass).values():
                if hasattr(command, "callback"):
                    command.callback = getattr(command.callback, "func", command.callback)
        self.cog = MatchCommands(self.bot)
        self.enterContext(patch.dict(resources_module.RESOURCE_MATCH_SIDE_LOCKS, clear=True))
        self.enterContext(patch.dict(resources_module.RESOURCE_CLAN_LOCKS, clear=True))
        self.enterContext(patch("elo_bot.features.base_store.preparation.BASE_STORE_RESERVATION_LOCK", new=asyncio.Lock()))
        self.enterContext(patch_sites(('elo_bot.extensions.match.results.invite_eligible_players', 'elo_bot.extensions.match.result_store.invite_eligible_players', 'elo_bot.features.results.recording.invite_eligible_players'), new=AsyncMock()))
        self.queue_docs = {}
        self.enterContext(patch('elo_bot.extensions.match.queue.find_attempt_match', new=AsyncMock(return_value=None)))
        self.enterContext(patch_sites(('elo_bot.extensions.match.queue.find_attempt_queue', 'elo_bot.features.queue.state.find_attempt_queue'), new=AsyncMock(
            side_effect=lambda user_id, attempt_id, _tiers: next(
                (queue for queue in self.queue_docs.values()
                 if user_id in queue.district_selections
                 and queue.district_selections[user_id].attempt_id == attempt_id), None),
        )))

    @staticmethod
    def make_player(user_id: str, elo: int, priority: bool = False):
        player = MagicMock(spec=Player)
        player.user_id = user_id
        player.elo = elo
        player.priority = priority
        player.matches = []
        player.district_presets = [None, None, None]
        player.capital_peak_enabled = True
        player.save_changes = AsyncMock()
        return player

    def make_queue(self, tier: int, players=None, entered_at=None):
        queue = MagicMock(spec=Queue)
        queue.tier = tier
        queue.players = list(players or [])
        queue.entered_at = dict(entered_at or {})
        queue.district_selections = {}
        queue.save_changes = AsyncMock()
        self.queue_docs[tier] = queue
        return queue

    @staticmethod
    def make_layout(link: str, records=None):
        layout = MagicMock()
        layout.link = link
        layout.image_url = f"https://example.test/{link}.png"
        layout.quality = "Good"
        layout.records = list(records or [])
        return layout

    async def run_choose_bases_with_layouts(
            self, layouts, district_quality=("Capital Peak", ("Primary", "Secondary"))):
        district, qualities = district_quality
        player = MagicMock(spec=ipy.User)
        player.id = 123
        opponent = MagicMock(spec=ipy.User)
        opponent.id = 456

        player_participant = MagicMock(user_id="123", bases=[None])
        opponent_participant = MagicMock(user_id="456", bases=[None], secondary_base_index=2)
        match = MagicMock(
            participants=[player_participant, opponent_participant],
            final_districts=[district],
            token="TOKEN",
        )
        match.save_changes = AsyncMock()

        layouts_query = MagicMock()
        layouts_query.to_list = AsyncMock(return_value=layouts)
        matches_query = MagicMock()
        matches_query.to_list = AsyncMock(return_value=[])

        with patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player')) as mock_player, \
                patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as mock_match, \
                patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout')) as mock_base_layout, \
                patch('elo_bot.features.matches.attack_bases.NotIn', side_effect=lambda *args: args), \
                patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=qualities), \
                patch_sites(('elo_bot.features.matches.lifecycle.random.randint', 'elo_bot.features.matches.attack_bases.random.randint'), return_value=2), \
                patch_sites(('elo_bot.features.matches.lifecycle.random.choices', 'elo_bot.features.matches.attack_bases.random.choices'), wraps=random.Random(1).choices) as mock_choices:
            mock_player.find_one = AsyncMock(side_effect=[MagicMock(), MagicMock()])
            mock_match.find_one = AsyncMock(return_value=match)
            mock_match.find.return_value = matches_query
            mock_base_layout.expiration_date.__gt__.return_value = "unexpired"
            mock_base_layout.quality.__eq__.side_effect = lambda value: ("quality", value)
            mock_base_layout.find.return_value = layouts_query

            result = await choose_bases("TOKEN", player, opponent)

        match.save_changes.assert_awaited_once()
        self.mock_base_layout = mock_base_layout
        return result, opponent_participant, mock_choices

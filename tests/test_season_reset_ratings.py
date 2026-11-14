import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.settings.document import DocumentSettings

from elo_bot.extensions.season import SeasonCommands
from elo_bot.config.emojis import SUCCESS_EMOJI
from elo_bot.db import Player, Season


class TestSeasonResetRatings(unittest.IsolatedAsyncioTestCase):
    async def test_reset_caps_ratings_and_preserves_lifetime_protection_and_archive(self) -> None:
        # Given: players at lifetime-protection, current-rating, and ceiling boundaries.
        cases = [
            (900, 1400, 950),
            (950, 950, 950),
            (950, 1200, 950),
            (975, 975, 965),
            (975, 1200, 965),
            (1000, 1000, 980),
            (1000, 1151, 980),
            (1000, 1200, 980),
            (1040, 1200, 1004),
            (1049, 1200, 1009),
            (1050, 1150, 1010),
            (1050, 1151, 1050),
            (1050, 1200, 1050),
            (1051, 1200, 1050),
            (1100, 1150, 1040),
            (1100, 1151, 1080),
            (1100, 1200, 1080),
            (1101, 1150, 1040),
            (1101, 1200, 1080),
            (1140, 1200, 1104),
            (1149, 1200, 1109),
            (1150, 1150, 1070),
            (1150, 1151, 1110),
            (1150, 1200, 1150),
            (1175, 1199, 1125),
            (1175, 1200, 1165),
            (1190, 1199, 1134),
            (1190, 1200, 1174),
            (1199, 1199, 1139),
            (1199, 1200, 1179),
            (1200, 1200, 1180),
            (1201, 1201, 1180),
            (1220, 1250, 1192),
            (1230, 1250, 1198),
            (1235, 1250, 1201),
            (1240, 1250, 1204),
            (1250, 1250, 1210),
            (1275, 1275, 1225),
            (1300, 1400, 1240),
            (1301, 1400, 1280),
            (1333, 1400, 1299),
            (1334, 1400, 1300),
            (1400, 1400, 1340),
            (1450, 1450, 1370),
            (1499, 1499, 1399),
            (1500, 1500, 1400),
            (1501, 1501, 1400),
            (1600, 1600, 1400),
            (1800, 1800, 1400),
            (2000, 2000, 1400),
            (2500, 2500, 1400),
        ]
        for model in (Player, Season):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.__name__, use_state_management=True,
            )))
        players = [
            Player.model_construct(id=str(index), user_id=str(index), tag="#P", elo=elo, highest_elo=peak)
            for index, (elo, peak, _) in enumerate(cases)
        ]
        rows = {}
        for player in players:
            player._save_state()
            rows[player.id] = deepcopy(player.get_saved_state())
        archives = []
        events = []

        async def write(query, update, **_kwargs):
            row = rows[query["_id"]]
            row.update(deepcopy(update["$set"]))
            events.append("player")
            return deepcopy(row)

        async def insert_season(document):
            archives.append(document)
            events.append("archive")
            return document

        async def send(*_args, **_kwargs):
            events.append("success")

        player_query = MagicMock(to_list=AsyncMock(return_value=players))
        season_query = MagicMock()
        season_query.sort.return_value.first_or_none = AsyncMock(return_value=None)
        collection = MagicMock(find_one_and_update=AsyncMock(side_effect=write))
        ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock(side_effect=send))
        cog = SeasonCommands(MagicMock(ext={}, get_ext=MagicMock(return_value=None)))

        # When: the actual command persists the reset and archives the completed season.
        with (
            patch.object(Player, "find_all", return_value=player_query),
            patch.object(Player, "get_pymongo_collection", return_value=collection),
            patch.object(Season, "find_all", return_value=season_query),
            patch.object(Season, "insert", new=insert_season),
        ):
            await cog.season_reset.callback.func(cog, ctx, start_date="1", end_date="2")

        # Then: committed ratings obey the ceiling without rewriting peaks or historical ratings.
        for index, (elo, peak, expected) in enumerate(cases):
            with self.subTest(elo=elo, lifetime_peak=peak):
                self.assertEqual(rows[str(index)]["elo"], expected)
                self.assertEqual(rows[str(index)]["highest_elo"], peak)
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            {player.player_id: player.elo for player in archives[0].players},
            {str(index): elo for index, (elo, _, _) in enumerate(cases)},
        )
        self.assertEqual(events[-2:], ["archive", "success"])
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

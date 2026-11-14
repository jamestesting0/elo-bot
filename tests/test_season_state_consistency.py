import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

import elobot.extensions.season as commands
from elobot.db import BaseLayout, Player, Season


def apply_set(row, update):
    for path, value in update.get("$set", {}).items():
        target = row
        parts = path.split(".")
        for key in parts[:-1]:
            target = target.setdefault(key, {})
        target[parts[-1]] = deepcopy(value)


class TestSeasonStateConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Season, Player, BaseLayout):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.__name__, use_state_management=True,
            )))
        self.bot = MagicMock(ext={}, get_ext=MagicMock(return_value=None))
        self.cog = commands.SeasonCommands(self.bot)

    async def test_distinct_district_records_preserve_each_success(self) -> None:
        seed = Season.model_construct(
            id=PydanticObjectId(), season_number=1, start=1, end=2, players=[], districts={},
        )
        seed._save_state()
        row = deepcopy(seed.get_saved_state())
        updates = []

        async def write(_query, update, **_kwargs):
            updates.append(deepcopy(update))
            apply_set(row, update)
            return deepcopy(row)

        collection = MagicMock(find_one_and_update=AsyncMock(side_effect=write))
        query = MagicMock()
        query.sort.return_value.first_or_none = AsyncMock(side_effect=lambda: seed.model_copy(deep=True))
        contexts = [MagicMock(spec=ipy.SlashContext, send=AsyncMock()) for _ in range(2)]
        with (
            patch.object(Season, "find_all", return_value=query),
            patch.object(Season, "get_pymongo_collection", return_value=collection),
            patch.object(commands, "BaseLayout"),
            patch.object(commands, "sort_bases", new=AsyncMock(return_value=[])),
        ):
            for ctx, district in zip(contexts, ("Barbarian Camp", "Balloon Lagoon")):
                await self.cog.season_base_record.callback.func(self.cog, ctx, district)

        self.assertEqual(set(row["districts"]), {"Barbarian Camp", "Balloon Lagoon"})
        self.assertEqual(updates, [
            {"$set": {"districts.Barbarian Camp": []}}, {"$set": {"districts.Balloon Lagoon": []}},
        ])
        for ctx in contexts:
            self.assertIn("Successfully recorded", ctx.send.await_args.args[0])

    async def test_reset_preserves_concurrent_presets_and_original_season_snapshot(self) -> None:
        for original_elo, expected_elo in ((1400, 1340), (900, 950)):
            with self.subTest(original_elo=original_elo):
                seed = Player.model_construct(
                    id="100", user_id="100", tag="#P", elo=original_elo, highest_elo=1400,
                )
                seed._save_state()
                row = deepcopy(seed.get_saved_state())
                row["district_presets"] = ["Barbarian Camp", None, None]
                row["blocked_users"] = ["200"]
                updates = []

                async def write(_query, update, **_kwargs):
                    updates.append(deepcopy(update))
                    apply_set(row, update)
                    return deepcopy(row)

                collection = MagicMock(find_one_and_update=AsyncMock(side_effect=write))
                players = MagicMock(to_list=AsyncMock(return_value=[seed]))
                seasons = MagicMock()
                seasons.sort.return_value.first_or_none = AsyncMock(return_value=None)
                ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
                inserted = []

                async def insert_season(document):
                    inserted.append(document)
                    return document

                with (
                    patch.object(Player, "find_all", return_value=players),
                    patch.object(Player, "get_pymongo_collection", return_value=collection),
                    patch.object(Season, "find_all", return_value=seasons),
                    patch.object(Season, "insert", new=insert_season),
                ):
                    await self.cog.season_reset.callback.func(self.cog, ctx, start_date="1", end_date="2")

                self.assertEqual(row["district_presets"], ["Barbarian Camp", None, None])
                self.assertEqual(row["blocked_users"], ["200"])
                self.assertEqual(row["elo"], expected_elo)
                self.assertEqual(updates, [{"$set": {"elo": expected_elo}}])
                self.assertEqual(len(inserted), 1)
                self.assertEqual(inserted[0].players[0].elo, original_elo)
                self.assertIn("season 1 is recorded", ctx.send.await_args.args[0])

    async def test_district_write_failure_never_reports_success(self) -> None:
        seed = Season.model_construct(
            id=PydanticObjectId(), season_number=1, start=1, end=2, players=[], districts={},
        )
        seed._save_state()
        query = MagicMock()
        query.sort.return_value.first_or_none = AsyncMock(return_value=seed)
        collection = MagicMock(find_one_and_update=AsyncMock(side_effect=OperationFailure("write failed")))
        ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        with (
            patch.object(Season, "find_all", return_value=query),
            patch.object(Season, "get_pymongo_collection", return_value=collection),
            patch.object(commands, "BaseLayout"),
            patch.object(commands, "sort_bases", new=AsyncMock(return_value=[])),
        ):
            with self.assertRaises(OperationFailure):
                await self.cog.season_base_record.callback.func(self.cog, ctx, "Barbarian Camp")

        ctx.send.assert_not_awaited()

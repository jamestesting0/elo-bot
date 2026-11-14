"""Issue 25: newly created bases expire fifteen days after creation."""

from contextlib import chdir
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Final
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
import interactions as ipy
from pymongo.results import InsertOneResult, UpdateResult

import elobot.extensions.base as commands
import elobot.extensions.preset as presets
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import BaseLayout, PendingBaseSubmission
from tests._patch_sites import patch_sites
import elobot.extensions.base as base_module


BASE_LINK: Final = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
NOW: Final = datetime(2026, 11, 11, 12, 34, 56, tzinfo=timezone.utc)
EXPECTED_EXPIRATION: Final = (NOW + timedelta(days=15)).timestamp()


class TestIssue25BaseCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Each test owns its screenshot scratch directory, outside the repository.
        root = self.enterContext(TemporaryDirectory(prefix="issue25-creation-"))
        self.enterContext(chdir(root))
        Path("temp").mkdir()
        clock = self.enterContext(patch.object(commands, "datetime"))
        clock.now.return_value = NOW
        preset_clock = self.enterContext(patch.object(presets, "datetime"))
        preset_clock.now.return_value = NOW

        self.rows = {}
        self.pending_row = {
            "_id": "9001", "link": BASE_LINK, "image_url": "https://cdn.example/base.webp",
            "submitter_id": "123", "submitter_display": "Submitter",
            "builder_id": "456", "builder_display": "Builder",
            "district": "Barbarian Camp", "proposed_quality": "Good",
        }
        for model in (BaseLayout, PendingBaseSubmission):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
        for field in ("link", "builder_id", "quality"):
            self.enterContext(patch.object(BaseLayout, field, ExpressionField(field), create=True))

        async def load_base(*_args, **kwargs):
            query = kwargs["filter"]
            return next((deepcopy(row) for row in self.rows.values()
                         if all(row.get(key) == value for key, value in query.items())), None)

        async def insert_base(row, **_kwargs):
            row.setdefault("_id", PydanticObjectId())
            self.rows[row["_id"]] = deepcopy(row)
            return InsertOneResult(row["_id"], acknowledged=True)

        self.collection = MagicMock(
            find_one=AsyncMock(side_effect=load_base),
            insert_one=AsyncMock(side_effect=insert_base),
        )
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=self.collection))

        async def load_pending(**_kwargs):
            return deepcopy(self.pending_row)

        async def update_pending(query, update):
            for key, expected in query.items():
                current = self.pending_row
                for part in key.split("."):
                    current = current.get(part) if current is not None else None
                if current != expected:
                    return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            for key, value in update["$set"].items():
                target = self.pending_row
                *parts, last = key.split(".")
                for part in parts:
                    target = target[part]
                target[last] = deepcopy(value)
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        pending_collection = MagicMock(
            find_one=AsyncMock(side_effect=load_pending),
            update_one=AsyncMock(side_effect=update_pending),
        )
        self.enterContext(patch.object(
            PendingBaseSubmission, "get_pymongo_collection", return_value=pending_collection,
        ))

        self.bot = MagicMock(spec=ipy.Client, ext={})
        self.bot.fetch_user = AsyncMock(return_value=SimpleNamespace(send=AsyncMock()))
        self.bot.owner.send = AsyncMock(return_value=SimpleNamespace(
            id=100, channel=SimpleNamespace(id=200),
            attachments=[SimpleNamespace(id=300, url="https://cdn.example/base.webp")],
        ))
        self.cog = base_module.BaseCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.ModalContext, defer=AsyncMock(), send=AsyncMock())
        self.ctx.author = SimpleNamespace(id=789)
        self.ctx.guild.id = 456
        self.ctx.message = MagicMock(id=9001, edit=AsyncMock())
        self.ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )

        async def download(_bot, _ctx, _url, path):
            Path(path).write_bytes(b"offline screenshot")
            return True

        self.enterContext(patch.object(commands, "download_base_screenshot", new=AsyncMock(side_effect=download)))
        self.enterContext(patch.object(
            commands, "capture_base_screenshot", new=AsyncMock(side_effect=AssertionError("Unexpected capture")),
        ))
        self.enterContext(patch.object(presets, "download_base_screenshot", new=AsyncMock(side_effect=download)))
        self.enterContext(patch.object(
            presets, "capture_base_screenshot", new=AsyncMock(side_effect=AssertionError("Unexpected capture")),
        ))
        self.screenshot = SimpleNamespace(url="https://cdn.example/submitted.webp")

    def assert_created_base(self, quality: str, builder_id: str | None) -> None:
        self.collection.insert_one.assert_awaited_once()
        self.assertEqual(len(self.rows), 1)
        row = next(iter(self.rows.values()))
        self.assertEqual(row["link"], BASE_LINK)
        self.assertEqual(row["quality"], quality)
        self.assertEqual(row["builder_id"], builder_id)
        self.ctx.send.assert_awaited_once()
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])
        self.assertEqual(row["expiration_date"], EXPECTED_EXPIRATION)

    async def test_submission_acceptance_persists_fifteen_day_expiration(self) -> None:
        await self.cog.accept_submission_modal.callback.func(
            self.cog, self.ctx, quality=["Great"], note="",
        )

        self.assertEqual(self.pending_row["resolution"]["phase"], "accepted")
        self.ctx.message.edit.assert_awaited_once()
        self.assert_created_base("Great", "456")
        self.assertEqual(self.pending_row["resolution"]["expiration_date"], EXPECTED_EXPIRATION)

    async def test_public_add_persists_fifteen_day_expiration(self) -> None:
        await self.cog.base_add.callback.func(
            self.cog, self.ctx, base_link=BASE_LINK, base_ss=self.screenshot,
        )

        self.assert_created_base("Average", None)

    async def test_private_add_persists_fifteen_day_expiration(self) -> None:
        player = MagicMock(
            user_id="user_id", find_one=AsyncMock(return_value=SimpleNamespace(elo=1000)),
        )
        query = MagicMock(count=AsyncMock(return_value=0))
        with (
            patch_sites(('elobot.extensions.base.Player', 'elobot.features.bases.layouts.private.Player'), player),
            patch.object(BaseLayout, "find", return_value=query),
        ):
            await self.cog.base_private_add.callback.func(
                self.cog, self.ctx, base_link=BASE_LINK, base_ss=self.screenshot,
            )

        self.assert_created_base("Private", "789")

    async def test_preset_add_persists_fifteen_day_expiration(self) -> None:
        player = SimpleNamespace(elo=1000, base_presets={}, save_changes=AsyncMock())
        player_model = MagicMock(user_id="user_id", find_one=AsyncMock(return_value=player))
        query = MagicMock(count=AsyncMock(return_value=0))
        cog = presets.PresetCommands(self.bot)
        with (
            patch.object(presets, "Player", player_model),
            patch.object(BaseLayout, "find", return_value=query),
        ):
            await cog.preset_base_add.callback.func(
                cog, self.ctx, base_link=BASE_LINK, base_ss=self.screenshot,
            )

        player.save_changes.assert_awaited_once()
        self.assertEqual(player.base_presets["Barbarian Camp"].id, next(iter(self.rows)))
        self.assert_created_base("Private", "789")

import io
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from PIL import Image

import elo_bot.extensions.preset as presets
from elo_bot.features.bases.fingerprints import images
import elo_bot.extensions.base as base_module
from tests._patch_sites import patch_sites
import elo_bot.extensions.base.submissions as base_submissions_module
from tests._patch_sites import BASE_MODULE_FAMILY, patch_family


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseScreenshotValidation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Path("temp").mkdir(exist_ok=True)
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="temp")))
        self.enterContext(patch.object(base_submissions_module.secrets, "token_hex", return_value=f"{self.directory.name}/image"))
        self.bot = MagicMock(spec=ipy.Client, ext={})
        self.bot.owner.send = AsyncMock(return_value=SimpleNamespace(
            id=100, channel=SimpleNamespace(id=200),
            attachments=[SimpleNamespace(id=300, url="https://cdn.example/stored.webp")],
        ))
        self.cog = base_module.BaseCommands(self.bot)
        self.preset = presets.PresetCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock())
        self.ctx.author = SimpleNamespace(id=123, display_name="Builder")
        self.screenshot = SimpleNamespace(url="https://cdn.example/renamed.webp")
        self.base = MagicMock(image_revision=0, district="Barbarian Camp")
        self.base.get_pymongo_collection.return_value.update_one = AsyncMock(
            return_value=SimpleNamespace(matched_count=1))
        self.player = SimpleNamespace(elo=1000, base_presets={}, save_changes=AsyncMock())
        self.bases = MagicMock(find_one=AsyncMock(return_value=None))
        self.bases.return_value.insert = AsyncMock()
        self.bases.find.return_value.count = AsyncMock(return_value=0)
        self.pending = MagicMock()
        self.pending.return_value.insert = AsyncMock()
        self.enterContext(patch_family(BASE_MODULE_FAMILY, "BaseLayout", self.bases))
        self.enterContext(patch.object(presets, "BaseLayout", self.bases))
        self.enterContext(patch_sites(('elo_bot.extensions.base.search.PendingBaseSubmission', 'elo_bot.extensions.base.submissions.PendingBaseSubmission'), self.pending))
        self.enterContext(patch_family(BASE_MODULE_FAMILY, "Player", SimpleNamespace(
            user_id="user_id", find_one=AsyncMock(return_value=self.player),
        )))
        self.enterContext(patch.object(presets, "Player", SimpleNamespace(
            user_id="user_id", find_one=AsyncMock(return_value=self.player),
        )))
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.get.side_effect = lambda *_args, **_kwargs: self.response
        self.enterContext(patch.object(images.aiohttp, "ClientSession", return_value=self.session))
        self.enterContext(patch.object(images.aiohttp, "TCPConnector"))

    def serve(self, content):
        async def chunks(_size):
            yield content

        self.response = MagicMock(status=200, content_length=len(content))
        self.response.content.iter_chunked = chunks
        self.response.__aenter__ = AsyncMock(return_value=self.response)

    async def invoke(self, action):
        if action == "staff":
            await self.cog.base_add.callback.func(self.cog, self.ctx, BASE_LINK, self.screenshot, "Good")
        elif action == "private":
            await self.cog.base_private_add.callback.func(self.cog, self.ctx, BASE_LINK, self.screenshot)
        elif action == "preset":
            await self.preset.preset_base_add.callback.func(self.preset, self.ctx, BASE_LINK, self.screenshot)
        elif action == "submit":
            await self.cog.base_submit.callback.func(self.cog, self.ctx, BASE_LINK, self.screenshot, "Good")
        elif action == "replace":
            self.bases.find_one.return_value = self.base
            await self.cog.base_set_image.callback.func(self.cog, self.ctx, BASE_LINK, self.screenshot.url)

    async def test_renamed_animated_gif_is_rejected_before_upload_or_persistence(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (220, 175), "red") as first, Image.new("RGB", (220, 175), "blue") as second:
            first.save(buffer, format="GIF", save_all=True, append_images=[second])
        self.serve(buffer.getvalue())
        for action in ("staff", "private", "preset", "submit", "replace"):
            with self.subTest(action=action):
                self.bases.find_one.return_value = None
                self.ctx.send.reset_mock()
                self.bot.owner.send.reset_mock()
                self.bases.return_value.insert.reset_mock()
                self.base.get_pymongo_collection.return_value.update_one.reset_mock()
                self.pending.return_value.insert.reset_mock()
                self.player.save_changes.reset_mock()
                await self.invoke(action)
                self.bot.owner.send.assert_not_awaited()
                self.bases.return_value.insert.assert_not_awaited()
                self.base.get_pymongo_collection.return_value.update_one.assert_not_awaited()
                self.pending.return_value.insert.assert_not_awaited()
                self.player.save_changes.assert_not_awaited()
                self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])
                self.assertIn("static PNG, JPEG or WebP", self.ctx.send.await_args.args[0])
                self.assertNotIn("successfully", self.ctx.send.await_args.args[0])
                self.assertEqual(list(self.directory.iterdir()), [])

    async def test_failed_private_replacement_preserves_previous_screenshot_and_request(self):
        self.serve(b"not an image")
        self.bases.find_one.side_effect = [None, self.base]
        await self.invoke("private")
        self.bot.owner.send.assert_not_awaited()
        self.base.get_pymongo_collection.return_value.update_one.assert_not_awaited()
        self.assertEqual(self.base.image_revision, 0)
        self.assertIn("static PNG, JPEG or WebP", self.ctx.send.await_args.args[0])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_supported_static_formats_are_accepted_by_the_same_upload_path(self):
        for format_name in ("PNG", "JPEG", "WEBP"):
            with self.subTest(format_name=format_name):
                buffer = io.BytesIO()
                with Image.new("RGB", (100, 100), "red") as screenshot:
                    screenshot.save(buffer, format=format_name)
                self.serve(buffer.getvalue())
                self.bot.owner.send.reset_mock()
                self.bases.return_value.insert.reset_mock()
                await self.invoke("staff")
                self.bot.owner.send.assert_awaited_once()
                self.bases.return_value.insert.assert_awaited_once()
                self.assertIn("Fingerprint extraction pending", self.ctx.send.await_args.args[0])
                self.assertEqual(list(self.directory.iterdir()), [])

    async def test_failed_discord_upload_cleans_screenshot_without_writing_or_announcing_success(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (100, 100), "red") as screenshot:
            screenshot.save(buffer, format="PNG")
        self.serve(buffer.getvalue())
        self.bot.owner.send.side_effect = ipy.errors.HTTPException(
            SimpleNamespace(status=500, reason="Server Error"), "upload failed")
        for action in ("staff", "private", "preset", "submit"):
            with self.subTest(action=action):
                with self.assertRaises(ipy.errors.HTTPException):
                    await self.invoke(action)
                self.bases.return_value.insert.assert_not_awaited()
                self.pending.return_value.insert.assert_not_awaited()
                self.player.save_changes.assert_not_awaited()
                self.ctx.send.assert_not_awaited()
                self.assertEqual(list(self.directory.iterdir()), [])

    async def test_cancelled_partial_download_is_cleaned_without_success(self):
        async def download(_bot, _url, _attachment, path):
            path.write_bytes(b"partial image")
            raise asyncio.CancelledError

        with patch.object(images, "download_image", side_effect=download):
            with self.assertRaises(asyncio.CancelledError):
                await self.invoke("staff")
        self.bot.owner.send.assert_not_awaited()
        self.bases.return_value.insert.assert_not_awaited()
        self.ctx.send.assert_not_awaited()
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_large_supported_screenshot_retains_webp_compression(self):
        buffer = io.BytesIO()
        with Image.new("RGB", (100, 100), "red") as screenshot:
            screenshot.save(buffer, format="PNG")
        self.serve(buffer.getvalue())
        path = self.directory / "image.webp"
        with patch.object(images, "MAX_SCREENSHOT_BYTES", 200):
            accepted = await images.download_base_screenshot(self.bot, self.ctx, self.screenshot.url, str(path))
        self.assertTrue(accepted)
        with Image.open(path) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(image.size, (100, 100))
        self.assertLessEqual(path.stat().st_size, 200)
        self.ctx.send.assert_not_awaited()

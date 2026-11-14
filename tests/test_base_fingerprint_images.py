import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import aiohttp
from bson import ObjectId
from PIL import Image

from elobot.features.bases.fingerprints import images, service
from elobot.features.bases.fingerprints.types import ExtractionResult, FingerprintError
from elobot.db.fingerprint_types import ImageAttachment
from elobot.db import BaseFingerprintTask, BaseLayout
from tests._base_fingerprint_fixture import fingerprint


from tests._base_image_fixture import OLD_URL, FRESH_URL, image_response

class TestFingerprintDownloads(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "image"
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.bot = SimpleNamespace(http=SimpleNamespace(request=AsyncMock(return_value={
            "refreshed_urls": [{"original": OLD_URL, "refreshed": FRESH_URL}],
        })), fetch_channel=AsyncMock())
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.get.side_effect = lambda url, **kwargs: image_response(
            404 if url == OLD_URL else 200, self.png)
        self.session_factory = self.enterContext(patch.object(images.aiohttp, "ClientSession",
                                                              return_value=self.session))
        self.enterContext(patch.object(images.aiohttp, "TCPConnector"))

    async def test_expired_legacy_discord_url_downloads_refreshed_bytes(self):
        url = await images.download_image(self.bot, OLD_URL, None, self.path)
        self.assertEqual(url, FRESH_URL)
        self.assertEqual(self.path.read_bytes(), self.png)
        request = self.bot.http.request.await_args
        self.assertEqual(request.args[0].method, "POST")
        self.assertEqual(request.args[0].path, "/attachments/refresh-urls")
        self.assertEqual(request.kwargs, {"payload": {"attachment_urls": [OLD_URL]}})
        self.assertEqual([call.args[0] for call in self.session.get.call_args_list], [OLD_URL, FRESH_URL])
        self.assertTrue(all(call.kwargs == {"allow_redirects": False}
                            for call in self.session.get.call_args_list))

    async def test_download_uses_browser_headers_without_bot_credentials(self):
        await images.download_image(self.bot, FRESH_URL, None, self.path)
        headers = self.session_factory.call_args.kwargs["headers"]
        self.assertTrue(headers["User-Agent"].startswith("Mozilla/5.0"))
        self.assertNotIn("Authorization", headers)
        self.assertFalse(self.session_factory.call_args.kwargs["trust_env"])
        self.bot.http.request.assert_not_awaited()

    async def test_message_refresh_bypasses_cache_and_selects_exact_attachment(self):
        async def fetch(_message_id, *, force=False):
            return SimpleNamespace(attachments=[SimpleNamespace(id=999, url="https://other.example/image"),
                                                SimpleNamespace(id=200, url=FRESH_URL if force else OLD_URL)])

        channel = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch))
        self.bot.fetch_channel.return_value = channel
        attachment = ImageAttachment(channel_id="100", message_id="300", attachment_id="200")
        self.assertEqual(await images.download_image(self.bot, OLD_URL, attachment, self.path), FRESH_URL)
        channel.fetch_message.assert_awaited_once_with("300", force=True)
        self.bot.http.request.assert_not_awaited()

    async def test_missing_message_uses_legacy_refresh(self):
        self.bot.fetch_channel.return_value = SimpleNamespace(fetch_message=AsyncMock(return_value=None))
        attachment = ImageAttachment(channel_id="100", message_id="300", attachment_id="200")
        self.assertEqual(await images.download_image(self.bot, OLD_URL, attachment, self.path), FRESH_URL)
        self.bot.fetch_channel.return_value = None
        self.assertEqual(await images.download_image(self.bot, OLD_URL, attachment, self.path), FRESH_URL)

    async def test_refresh_failure_and_still_unavailable_source_fail_without_file(self):
        for result in ({"refreshed_urls": []}, {"refreshed_urls": [{"original": OLD_URL, "refreshed": FRESH_URL}]}):
            with self.subTest(result=result):
                self.bot.http.request.return_value = result
                self.session.get.side_effect = lambda *_args, **_kwargs: image_response(404)
                self.bot.http.request.reset_mock()
                with self.assertRaisesRegex(FingerprintError, "^source_unavailable$"):
                    await images.download_image(self.bot, OLD_URL, None, self.path)
                self.bot.http.request.assert_awaited_once()
                self.assertFalse(self.path.exists())
        self.bot.http.request.side_effect = aiohttp.ClientConnectionError("connection lost")
        with self.assertRaisesRegex(FingerprintError, "^source_unavailable$"):
            await images.download_image(self.bot, OLD_URL, None, self.path)

    async def test_refresh_cannot_substitute_another_image_or_private_host(self):
        for replacement in ("http://127.0.0.1/image", FRESH_URL.replace("/200/", "/201/"),
                            "https://other.example/attachments/100/200/base.png"):
            with self.subTest(replacement=replacement):
                self.bot.http.request.return_value = {"refreshed_urls": [
                    {"original": OLD_URL, "refreshed": replacement}]}
                self.session.get.reset_mock()
                with self.assertRaisesRegex(FingerprintError, "^source_unavailable$"):
                    await images.download_image(self.bot, OLD_URL, None, self.path)
                self.assertEqual(self.session.get.call_count, 1)
                self.assertFalse(self.path.exists())

    async def test_non_discord_errors_do_not_send_urls_to_discord(self):
        self.session.get.side_effect = lambda *_args, **_kwargs: image_response(404)
        for url in ("https://example.com/private.png", "https://cdn.discordapp.com.evil.example/attachments/100/200/x"):
            with self.subTest(url=url), self.assertRaisesRegex(FingerprintError, "^source_unavailable$"):
                await images.download_image(self.bot, url, None, self.path)
        self.bot.http.request.assert_not_awaited()

    async def test_download_guards_remain_after_refresh(self):
        responses = [image_response(302, headers={"Location": "https://127.0.0.1/image"}),
                     image_response(content=self.png, length=images.MAX_IMAGE_BYTES + 1),
                     image_response(content=self.png)]
        for response, error in zip(responses, ("invalid_image_url", "image_too_large", "image_too_large")):
            with self.subTest(error=error):
                self.session.get.side_effect = [image_response(404), response]
                with patch.object(images, "MAX_IMAGE_BYTES", 1):
                    with self.assertRaisesRegex(FingerprintError, f"^{error}$"):
                        await images.download_image(self.bot, OLD_URL, None, self.path)
                self.assertFalse(self.path.exists())

    async def test_single_and_partial_gpu_batch_keep_association_and_cleanup(self):
        for device, count in (("cpu", 1), ("cuda:0", 3)):
            with self.subTest(device=device):
                tasks = [BaseFingerprintTask.model_construct(id=str(uuid4()), base_id=ObjectId(),
                         source_revision=index, source_image_url=OLD_URL, district="Barbarian Camp",
                         claim_token=str(uuid4())) for index in range(count)]
                bot_service = service.FingerprintService(self.bot, "test")
                worker = bot_service.cpu if device == "cpu" else bot_service.gpu

                async def extract(inputs):
                    self.assertEqual(worker.device, device)
                    for source in inputs:
                        self.assertEqual(Path(source.path).read_bytes(), self.png)
                        self.assertEqual(source.source_image_url, FRESH_URL)
                    return [ExtractionResult(fingerprint=fingerprint(source.source_revision)) for source in inputs]

                worker.extract = AsyncMock(side_effect=extract)
                commit = AsyncMock()
                with patch.object(service, "SCRATCH", self.directory), \
                        patch.object(service.BaseLayout, "get", new=AsyncMock(return_value=BaseLayout.model_construct())), \
                        patch.object(service, "commit_result", new=commit):
                    await bot_service.process(tasks, worker)
                worker.extract.assert_awaited_once()
                self.assertEqual({call.args[0].id for call in commit.await_args_list}, {task.id for task in tasks})
                for call in commit.await_args_list:
                    self.assertEqual(call.args[0].source_revision, call.args[1].fingerprint.source_revision)
                self.assertEqual(list((self.directory / "test").iterdir()), [])

    async def test_inference_failure_and_cancellation_cleanup_downloads(self):
        bot_service = service.FingerprintService(self.bot, "test")
        task = BaseFingerprintTask.model_construct(id=str(uuid4()), base_id=ObjectId(), source_revision=0,
                source_image_url=OLD_URL, district="Barbarian Camp", claim_token=str(uuid4()))
        with patch.object(service, "SCRATCH", self.directory), \
                patch.object(service.BaseLayout, "get", new=AsyncMock(return_value=BaseLayout.model_construct())), \
                patch.object(service, "commit_result", new=AsyncMock()) as commit:
            bot_service.cpu.extract = AsyncMock(side_effect=FingerprintError("inference_failed"))
            await bot_service.process([task], bot_service.cpu)
            self.assertEqual(commit.await_args.args[1].error, "inference_failed")
            self.assertEqual(list((self.directory / "test").iterdir()), [])
            commit.reset_mock()
            bot_service.cpu.extract.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await bot_service.process([task], bot_service.cpu)
            commit.assert_not_awaited()
            self.assertEqual(list((self.directory / "test").iterdir()), [])

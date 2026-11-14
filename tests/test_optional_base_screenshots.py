import asyncio
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import interactions as ipy
from PIL import Image
from pymongo.errors import OperationFailure

import elobot.features.bases.screenshots as screenshots
import elobot.extensions.base as commands
import elobot.extensions.preset as presets
from elobot.integrations import helper_api
from elobot.features.bases.fingerprints import images
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.config.presentation import UNAVAILABLE_IMAGE


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
JOB_ID = "0123456789abcdef0123456789abcdef"
ACTIONS = ("staff", "private", "preset", "submit", "replace")


def http_response(status=200, document=None, content=b""):
    response = MagicMock(status=status, content_length=len(content))
    response.__aenter__ = AsyncMock(return_value=response)
    response.text = AsyncMock(return_value=json.dumps(document))

    async def chunks(_size):
        yield content

    response.content.iter_chunked = chunks
    return response


class TestOptionalBaseScreenshots(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Path("temp").mkdir(exist_ok=True)
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory(dir="temp")))
        self.enterContext(patch.object(commands.secrets, "token_hex", return_value=f"{self.directory.name}/image"))
        buffer = io.BytesIO()
        with Image.new("RGB", (100, 100), "red") as image:
            image.save(buffer, "WEBP")
        self.image = buffer.getvalue()
        self.bot = MagicMock(spec=ipy.Client, ext={})
        self.uploaded = SimpleNamespace(id=100, channel=SimpleNamespace(id=200), attachments=[
            SimpleNamespace(id=300, url="https://cdn.example/durable.webp"),
        ])
        self.bot.owner.send = AsyncMock(side_effect=self.upload)
        self.review = MagicMock(id=9001)
        self.review.edit = AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(components=[kwargs["components"]]))
        self.review_channel = MagicMock(send=AsyncMock(return_value=self.review))
        self.bot.fetch_channel = AsyncMock(return_value=self.review_channel)
        self.cog = commands.BaseCommands(self.bot)
        self.preset = presets.PresetCommands(self.bot)
        self.ctx = MagicMock(
            spec=ipy.SlashContext, defer=AsyncMock(), send=AsyncMock(side_effect=self.respond),
            edit=AsyncMock(side_effect=self.edit_progress),
        )
        self.progress_message = SimpleNamespace(id=700)
        self.ctx.author = SimpleNamespace(id=123, display_name="Builder")
        self.ctx.guild = SimpleNamespace(id=456)
        self.attachment = SimpleNamespace(url="https://cdn.example/provided.webp")
        self.bases = MagicMock(side_effect=self.construct_base, find_one=AsyncMock(side_effect=self.find_base))
        self.bases.find.return_value.count = AsyncMock(return_value=0)
        self.pending = MagicMock(side_effect=self.construct_review)
        self.player = SimpleNamespace(elo=1000, base_presets={}, save_changes=AsyncMock(side_effect=self.save_presets))
        for module in (commands, presets):
            self.enterContext(patch.object(module, "BaseLayout", self.bases))
            self.enterContext(patch.object(module, "Player", SimpleNamespace(
                user_id="user_id", find_one=AsyncMock(return_value=self.player),
            )))
        self.enterContext(patch.object(commands, "PendingBaseSubmission", self.pending))
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.post.side_effect = self.post
        self.session.get.side_effect = self.get
        self.enterContext(patch.object(screenshots.aiohttp, "ClientSession", return_value=self.session))
        self.enterContext(patch.object(images.aiohttp, "TCPConnector"))
        self.enterContext(patch.object(screenshots, "HELPER_API_BASE_URL", "https://helper.example"))
        self.enterContext(patch.object(screenshots, "HELPER_API_KEY", "test-key"))
        self.poll_sleep = self.enterContext(patch.object(helper_api.anyio, "sleep", new=AsyncMock()))
        self.reset_case("staff")

    def reset_case(self, action):
        self.action = action
        self.stage = "success"
        self.existing = None
        self.lookups = 0
        self.rows = {}
        self.reviews = {}
        self.saved_presets = {}
        self.events = []
        self.progress_updates = []
        self.player.base_presets = {}
        self.uploaded.attachments = [SimpleNamespace(id=300, url="https://cdn.example/durable.webp")]
        self.upload_gate = None
        self.insert_gate = None
        for mock in (self.ctx.defer, self.ctx.send, self.ctx.edit, self.bot.owner.send, self.bot.fetch_channel,
                     self.player.save_changes, self.bases, self.pending, self.session, self.review_channel.send):
            mock.reset_mock()

    def construct_base(self, **fields):
        base = SimpleNamespace(id="base", image_revision=0, **fields)

        async def insert():
            if self.insert_gate:
                entered, release = self.insert_gate
                entered.set()
                await asyncio.wait_for(release.wait(), timeout=5)
            if self.stage == "insert_failure":
                raise OperationFailure("base insert failed")
            self.rows[base.id] = deepcopy(fields)
            self.events.append("base_saved")

        async def update_one(query, update):
            self.assertEqual(query["_id"], base.id)
            self.assertEqual(query["image_url"], self.rows[base.id]["image_url"])
            self.rows[base.id].update(deepcopy(update["$set"]))
            self.events.append("base_saved")
            return SimpleNamespace(matched_count=1)

        base.insert = AsyncMock(side_effect=insert)
        base.get_pymongo_collection = lambda: SimpleNamespace(update_one=AsyncMock(side_effect=update_one))
        return base

    def store_existing(self):
        fields = dict(district="Barbarian Camp", link=BASE_LINK, image_url="https://cdn.example/previous.webp",
                      expiration_date=100, builder_id="123", quality="Private", records=[],
                      image_attachment=None, fingerprint_extraction=None)
        self.existing = self.construct_base(**fields)
        self.rows[self.existing.id] = deepcopy(fields)
        self.player.base_presets = {"Barbarian Camp": self.existing}
        self.saved_presets = {"Barbarian Camp": self.existing.id}

    async def find_base(self, *_queries):
        self.lookups += 1
        if self.action in ("private", "preset") and self.lookups == 1:
            return None
        return self.existing

    def construct_review(self, **fields):
        async def insert():
            if self.stage == "insert_failure":
                raise OperationFailure("review insert failed")
            self.reviews[fields["id"]] = deepcopy(fields)
            self.events.append("review_saved")

        return SimpleNamespace(insert=AsyncMock(side_effect=insert))

    async def save_presets(self):
        self.saved_presets = {district: base.id for district, base in self.player.base_presets.items()}
        self.events.append("preset_saved")

    async def respond(self, message, **_kwargs):
        if str(LOADING_EMOJI) in message:
            self.events.append("requesting")
            self.progress_updates.append(message)
            return self.progress_message
        self.events.append("success" if str(SUCCESS_EMOJI) in message else "failure")
        if str(SUCCESS_EMOJI) in message:
            if self.action == "submit":
                self.assertTrue(self.reviews)
            else:
                self.assertTrue(self.rows)
            if self.action == "preset":
                self.assertTrue(self.saved_presets)

    async def edit_progress(self, message, *, content):
        self.assertIs(message, self.progress_message)
        self.progress_updates.append(content)
        if str(LOADING_EMOJI) in content:
            self.assertNotIn("base_saved", self.events)
            self.assertEqual(self.reviews, {})
        return message

    async def upload(self, **kwargs):
        self.events.append("upload")
        if self.upload_gate:
            entered, release = self.upload_gate
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5)
        if self.stage == "upload_cancel":
            raise asyncio.CancelledError
        if self.stage == "upload_failure":
            raise ipy.errors.HTTPException(SimpleNamespace(status=500, reason="failed"), "upload failed")
        if self.stage == "empty_upload":
            return SimpleNamespace(attachments=[])
        with Image.open(kwargs["file"].file) as image:
            self.assertEqual(image.size, (100, 100))
        return self.uploaded

    def post(self, url, **kwargs):
        self.assertEqual(url, "https://helper.example/v1/jobs")
        self.assertEqual(kwargs["headers"], {"X-Api-Key": "test-key"})
        self.events.append("create")
        if self.stage == "create_network":
            raise aiohttp.ClientConnectionError("offline")
        if self.stage == "create_unavailable":
            return http_response(503)
        if self.stage == "create_rejected":
            return http_response(400, {"error": "capture rejected"})
        if self.stage == "create_invalid":
            return http_response(201, {})
        if self.stage == "conflict":
            return http_response(409, {
                "error": "capture already running", "existing_job_id": JOB_ID, "existing_job_status": "running",
            })
        return http_response(201, {"id": JOB_ID})

    def get(self, url, **kwargs):
        if url == self.attachment.url:
            self.events.append("provided")
            return http_response(content=self.image)
        self.assertEqual(kwargs["headers"], {"X-Api-Key": "test-key"})
        if url.endswith("/image"):
            self.events.append("artifact")
            if self.stage == "artifact_network":
                raise aiohttp.ClientConnectionError("artifact unavailable")
            if self.stage == "artifact_missing":
                return http_response(404)
            if self.stage == "artifact_invalid":
                return http_response(content=b"not an image")
            if self.stage == "artifact_oversize":
                response = http_response(content=self.image)
                response.content_length = screenshots.MAX_IMAGE_BYTES + 1
                return response
            if self.stage == "artifact_cancel":
                response = http_response()

                async def chunks(_size):
                    yield self.image[:10]
                    raise asyncio.CancelledError

                response.content.iter_chunked = chunks
                return response
            return http_response(content=self.image)
        self.assertEqual(url, f"https://helper.example/v1/jobs/{JOB_ID}")
        self.events.append("poll")
        if self.stage == "poll_missing":
            return http_response(404)
        if self.stage == "poll_network":
            raise aiohttp.ClientConnectionError("status unavailable")
        status = {"capture_failed": "failed", "capture_cancelled": "cancelled"}.get(self.stage, "succeeded")
        if self.stage == "queued" and self.events.count("poll") == 1:
            status = "queued"
        if self.stage == "progress":
            status = ["queued", "queued", "running", "running", "succeeded"][self.events.count("poll") - 1]
        document = {
            "id": JOB_ID, "type": "get_base_image", "status": status,
            "created_at": 100, "started_at": 101, "finished_at": 102, "events": [], "error": None,
            "result": {"image": f"{JOB_ID}.webp"} if status == "succeeded" else None,
        }
        if self.stage == "poll_invalid":
            document["result"] = {"image": "../not-a-helper-artifact.webp"}
        if self.stage == "wrong_job":
            document["type"], document["result"] = "preserve_base_link", None
        if self.stage == "wrong_id":
            document["id"] = "f" * 32
        if self.stage == "progress" and status == "running":
            document["events"] = [{"t": 101, "message": "Opening layout 123"}]
        if status == "failed":
            document["error"] = "Device could not open layout 123"
        return http_response(document=document)

    async def invoke(self, provided=False):
        if self.action == "replace" and self.existing is None:
            self.store_existing()
        screenshot = self.attachment if provided else None
        if self.action == "staff":
            await self.cog.base_add.callback.func(self.cog, self.ctx, BASE_LINK, screenshot, "Good")
        elif self.action == "private":
            await self.cog.base_private_add.callback.func(self.cog, self.ctx, BASE_LINK, screenshot)
        elif self.action == "preset":
            await self.preset.preset_base_add.callback.func(self.preset, self.ctx, BASE_LINK, screenshot)
        elif self.action == "submit":
            await self.cog.base_submit.callback.func(self.cog, self.ctx, BASE_LINK, screenshot, "Good")
        else:
            await self.cog.base_set_image.callback.func(
                self.cog, self.ctx, BASE_LINK, self.attachment.url if provided else None,
            )

    def assert_clean(self):
        self.assertEqual(list(self.directory.iterdir()), [])
        for call in self.bot.owner.send.await_args_list:
            self.assertFalse(Path(call.kwargs["file"].file).exists())

    def assert_failure(self):
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])
        self.assertNotIn("success", self.events)
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])
        self.assert_clean()

    async def test_all_image_command_replies_are_ephemeral(self):
        for action in ACTIONS:
            for provided in (False, True):
                stages = ("success", "empty_upload") if provided else (
                    "success", "create_rejected", "capture_failed", "artifact_missing", "empty_upload",
                )
                for stage in stages:
                    with self.subTest(action=action, provided=provided, stage=stage):
                        self.reset_case(action)
                        self.stage = stage
                        await self.invoke(provided=provided)
                        self.ctx.defer.assert_awaited_once_with(ephemeral=True)
                        self.assertTrue(self.ctx.send.await_args_list)
                        for reply in self.ctx.send.await_args_list:
                            self.assertIs(reply.kwargs.get("ephemeral"), True)
                        self.assert_clean()

    async def test_missing_base_image_reply_is_ephemeral_without_requesting_helper(self):
        self.reset_case("replace")
        await self.cog.base_set_image.callback.func(self.cog, self.ctx, BASE_LINK)
        self.ctx.send.assert_awaited_once()
        self.assertIs(self.ctx.send.await_args.kwargs.get("ephemeral"), True)
        self.session.post.assert_not_called()

    async def test_supplied_screenshot_rehosts_without_creating_helper_job(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                self.reset_case(action)
                await self.invoke(provided=True)
                self.session.post.assert_not_called()
                self.ctx.edit.assert_not_awaited()
                self.assertEqual(self.progress_updates, [])
                self.assertEqual(self.events.count("provided"), 1)
                self.assertNotIn("poll", self.events)
                saved = next(iter(self.reviews.values() if action == "submit" else self.rows.values()))
                self.assertEqual(saved["image_url"], self.uploaded.attachments[0].url)
                self.assertIsNotNone(saved["image_attachment"])
                self.assertEqual(self.events[-1], "success")
                self.assert_clean()

    async def test_omitted_screenshot_waits_for_capture_download_and_durable_upload(self):
        for action in ACTIONS:
            with self.subTest(action=action):
                self.reset_case(action)
                await self.invoke()
                self.assertEqual(self.session.post.call_args.kwargs["json"], {
                    "type": "get_base_image", "requested_by": "123",
                    "payload": {"base_link": BASE_LINK, "scout_view": False, "filetype": "webp",
                                "keep_secret": action in ("private", "preset", "replace")},
                })
                saved = next(iter(self.reviews.values() if action == "submit" else self.rows.values()))
                self.assertEqual(saved["image_url"], self.uploaded.attachments[0].url)
                self.assertIsNotNone(saved["image_attachment"])
                self.assertLess(self.events.index("artifact"), self.events.index("upload"))
                self.assertLess(self.events.index("upload"), self.events.index(
                    "review_saved" if action == "submit" else "base_saved"))
                self.assertEqual(self.events[-1], "success")
                self.assert_clean()

    async def test_public_image_replacement_uses_public_helper_capture(self):
        self.reset_case("replace")
        self.store_existing()
        self.existing.quality = "Good"
        await self.invoke()
        self.assertFalse(self.session.post.call_args.kwargs["json"]["payload"]["keep_secret"])
        self.assertEqual(self.rows["base"]["image_url"], self.uploaded.attachments[0].url)

    async def test_strict_capture_failures_never_change_base_or_create_review(self):
        stages = ("create_unavailable", "create_network", "create_rejected", "create_invalid",
                  "poll_missing", "poll_network", "poll_invalid", "wrong_job", "wrong_id",
                  "capture_failed", "capture_cancelled", "artifact_missing", "artifact_network",
                  "artifact_invalid", "artifact_oversize")
        for action in ("staff", "submit", "replace"):
            for stage in stages:
                with self.subTest(action=action, stage=stage):
                    self.reset_case(action)
                    if action != "submit":
                        self.store_existing()
                    before = deepcopy(self.rows)
                    self.stage = stage
                    await self.invoke()
                    self.assertEqual(self.rows, before)
                    self.assertEqual(self.reviews, {})
                    self.bot.owner.send.assert_not_awaited()
                    self.review_channel.send.assert_not_awaited()
                    self.player.save_changes.assert_not_awaited()
                    self.assert_failure()

    async def test_private_capture_failure_saves_placeholder_without_extraction(self):
        for action in ("private", "preset"):
            for stage in ("create_unavailable", "capture_failed", "artifact_missing", "artifact_invalid"):
                with self.subTest(action=action, stage=stage):
                    self.reset_case(action)
                    self.stage = stage
                    await self.invoke()
                    saved = self.rows["base"]
                    self.assertEqual(saved["image_url"], UNAVAILABLE_IMAGE)
                    self.assertIsNone(saved["image_attachment"])
                    self.assertIsNone(saved["fingerprint_extraction"])
                    self.bot.owner.send.assert_not_awaited()
                    if action == "preset":
                        self.assertEqual(self.saved_presets, {"Barbarian Camp": "base"})
                    self.assertNotIn("extraction pending", self.ctx.send.await_args.args[0].lower())
                    self.assertEqual(self.events[-1], "success")
                    self.assert_clean()

    async def test_private_capture_failure_preserves_existing_image_and_preset(self):
        for action in ("private", "preset"):
            with self.subTest(action=action):
                self.reset_case(action)
                self.store_existing()
                before = deepcopy(self.rows)
                previous = dict(self.player.base_presets)
                self.stage = "capture_failed"
                await self.invoke()
                self.assertEqual(self.rows, before)
                self.assertEqual(self.player.base_presets, previous)
                self.assertEqual(self.saved_presets, {"Barbarian Camp": "base"})
                self.player.save_changes.assert_not_awaited()
                self.assert_failure()

    async def test_upload_failure_or_missing_attachment_preserves_state_for_all_commands(self):
        for action in ACTIONS:
            for stage in ("upload_failure", "empty_upload"):
                with self.subTest(action=action, stage=stage):
                    self.reset_case(action)
                    if action != "submit":
                        self.store_existing()
                    before = deepcopy(self.rows)
                    previous = dict(self.player.base_presets)
                    self.stage = stage
                    await self.invoke()
                    self.assertEqual(self.rows, before)
                    self.assertEqual(self.player.base_presets, previous)
                    self.assertEqual(self.reviews, {})
                    self.review_channel.send.assert_not_awaited()
                    self.assert_failure()

    async def test_conflict_adopts_existing_capture_and_queued_job_is_polled_to_success(self):
        for stage in ("conflict", "queued"):
            with self.subTest(stage=stage):
                self.reset_case("staff")
                self.stage = stage
                await self.invoke()
                self.session.post.assert_called_once()
                self.assertEqual(self.rows["base"]["image_url"], self.uploaded.attachments[0].url)
                self.assertEqual(self.events.count("poll"), 2 if stage == "queued" else 1)
                self.assertEqual(self.events[-1], "success")
                self.assert_clean()

    async def test_queue_and_running_progress_are_visible_before_download_and_save(self):
        self.reset_case("staff")
        self.stage = "progress"
        await self.invoke()
        self.assertLess(self.events.index("requesting"), self.events.index("create"))
        queued = [text for text in self.progress_updates if JOB_ID in text and "queued" in text]
        running = [text for text in self.progress_updates if JOB_ID in text and "running" in text]
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(running), 1)
        self.assertIn("Opening layout 123", running[0])
        self.assertLess(self.progress_updates.index(queued[0]), self.progress_updates.index(running[0]))
        self.assertTrue(self.progress_updates[-1].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(self.events[-2:], ["base_saved", "success"])
        self.assertEqual(self.rows["base"]["image_url"], self.uploaded.attachments[0].url)

    async def test_helper_failures_keep_the_actual_reason_visible_before_command_outcome(self):
        for action in ACTIONS:
            for stage, detail in (
                ("create_rejected", "capture rejected"),
                ("capture_failed", "Device could not open layout 123"),
                ("capture_cancelled", "cancelled"),
                ("poll_missing", "the job was not found by the status API."),
                ("artifact_missing", "404"),
            ):
                with self.subTest(action=action, stage=stage):
                    self.reset_case(action)
                    self.stage = stage
                    await self.invoke()
                    self.assertTrue(self.progress_updates[-1].startswith(str(ERROR_EMOJI)))
                    self.assertIn(detail, self.progress_updates[-1])
                    if action in ("private", "preset"):
                        self.assertEqual(self.rows["base"]["image_url"], UNAVAILABLE_IMAGE)
                    else:
                        self.assertNotIn("success", self.events)
                        self.assertEqual(self.reviews, {})
                    self.assert_clean()

    async def test_existing_running_job_is_shown_without_claiming_a_new_queue_entry(self):
        self.reset_case("staff")
        original_post = self.post

        def existing_job(url, **kwargs):
            original_post(url, **kwargs)
            return http_response(409, {
                "error": "capture already running", "existing_job_id": JOB_ID, "existing_job_status": "running",
            })

        self.session.post.side_effect = existing_job
        await self.invoke()
        self.session.post.assert_called_once()
        self.assertIn(JOB_ID, self.progress_updates[1])
        self.assertIn("running", self.progress_updates[1])
        self.assertFalse(any("queued" in text for text in self.progress_updates))

    async def test_failed_progress_edit_does_not_undo_a_successful_capture(self):
        self.reset_case("staff")
        self.ctx.edit.side_effect = ipy.errors.HTTPException(SimpleNamespace(status=500, reason="failed"))
        with self.assertLogs(level="ERROR"):
            await self.invoke()
        self.assertEqual(self.rows["base"]["image_url"], self.uploaded.attachments[0].url)
        self.assertEqual(self.events[-1], "success")

    async def test_cancellation_during_artifact_or_upload_never_changes_state_and_cleans_files(self):
        for action in ACTIONS:
            for stage in ("artifact_cancel", "upload_cancel"):
                with self.subTest(action=action, stage=stage):
                    self.reset_case(action)
                    if action != "submit":
                        self.store_existing()
                    before = deepcopy(self.rows)
                    self.stage = stage
                    with self.assertRaises(asyncio.CancelledError):
                        await self.invoke()
                    self.assertEqual(self.rows, before)
                    self.assertEqual(self.reviews, {})
                    self.player.save_changes.assert_not_awaited()
                    self.assertNotIn("success", self.events)
                    self.assert_clean()

    async def test_upload_and_database_remain_pending_until_their_actual_completion(self):
        for stage in ("upload", "insert"):
            with self.subTest(stage=stage):
                self.reset_case("staff")
                entered, release = asyncio.Event(), asyncio.Event()
                if stage == "upload":
                    self.upload_gate = (entered, release)
                else:
                    self.insert_gate = (entered, release)
                task = asyncio.create_task(self.invoke())
                try:
                    await asyncio.wait_for(entered.wait(), timeout=2)
                    self.ctx.defer.assert_awaited_once_with(ephemeral=True)
                    self.assertTrue(self.progress_updates)
                    self.assertNotIn("success", self.events)
                    self.assertEqual(self.rows, {})
                    release.set()
                    await asyncio.wait_for(task, timeout=2)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=5)
                self.assertEqual(self.events[-2:], ["base_saved", "success"])
                self.assert_clean()

    async def test_insert_failure_never_announces_success_or_leaves_capture_files(self):
        for action in ("staff", "private", "preset", "submit"):
            with self.subTest(action=action):
                self.reset_case(action)
                self.stage = "insert_failure"
                if action == "submit":
                    await self.invoke()
                    self.assert_failure()
                else:
                    with self.assertRaises(OperationFailure):
                        await self.invoke()
                    self.assertNotIn("success", self.events)
                self.assertEqual(self.rows, {})
                self.assertEqual(self.reviews, {})
                self.assertEqual(self.saved_presets, {})
                self.assertNotIn("success", self.events)
                self.assert_clean()

    def test_slash_inputs_are_optional_but_quality_remains_required(self):
        for command, option_name in (
            (self.cog.base_add, "base_ss"), (self.cog.base_private_add, "base_ss"),
            (self.preset.preset_base_add, "base_ss"), (self.cog.base_submit, "base_ss"),
            (self.cog.base_set_image, "image_link"),
        ):
            with self.subTest(command=command.name):
                options = {str(option.name): option for option in command.options}
                self.assertFalse(options[option_name].required)
                if "base_quality" in options:
                    self.assertTrue(options["base_quality"].required)

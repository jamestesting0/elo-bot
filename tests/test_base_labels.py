import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from PIL import Image

from elo_bot.features.bases.fingerprints import labels, service
from elo_bot.features.bases.fingerprints.types import ExtractionResult, FingerprintError, ImageInput
from elo_bot.db.fingerprint_types import ImageAttachment, PIPELINE_ID
from tests import _base_search_fixture as fixture
from tests._relative_geometry_fixture import layout, points
from tests._base_extraction_fixture import detections


class TestLabelRendering(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.directory / "source.png"
        Image.new("RGB", (1000, 800), (200, 20, 80)).save(self.path)
        self.output = self.directory / "labels.png"

    def test_labels_names_confidence_and_hall_preserve_source_colors_without_font_downloads(self):
        from ultralytics.utils import plotting
        fingerprint = layout(hall=True)
        bounds = [(b.x - .01, b.y - .01, b.x + .01, b.y + .01) for b in fingerprint.buildings]
        original = plotting.Annotator.box_label
        with patch.object(plotting, "check_font", side_effect=AssertionError("font download attempted")), \
                patch.object(plotting.Annotator, "box_label", autospec=True, side_effect=original) as draw:
            labels.render_labels(self.path, ExtractionResult(fingerprint=fingerprint, bounds=bounds), self.output)
        self.assertEqual([call.args[2] for call in draw.call_args_list],
                         [f"{b.type} {b.conf:.2f}" for b in fingerprint.buildings])
        self.assertIn("district_hall 1.00", [call.args[2] for call in draw.call_args_list])
        with Image.open(self.output) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.getpixel((0, 0)), (200, 20, 80))
            self.assertEqual(image.size, (1000, 800))
            self.assertGreater(len(set(image.getdata())), 10)

    def test_uploaded_and_found_base_annotations_are_side_by_side(self):
        from ultralytics.utils import plotting
        found = self.directory / "found.png"
        Image.new("RGB", (1000, 1200), (20, 180, 60)).save(found)
        paths = (self.path, found)
        expected = tuple(layout(version=version).model_copy(update={
            "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }) for version, path in zip((2, 1), paths, strict=True))
        inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                             district_id=1, pipeline_id=PIPELINE_ID) for path in paths]
        detector = MagicMock()
        original = plotting.Annotator.box_label
        with patch.object(plotting, "check_font", side_effect=AssertionError("font download attempted")), \
                patch.object(plotting.Annotator, "box_label", autospec=True, side_effect=original) as draw, \
                patch("elo_bot.features.bases.fingerprints.extractor.sliced_detections",
                      side_effect=[detections(), detections(halls=(("district_hall", .1, .1),))]):
            labels.create_labels(detector, inputs, expected, "cpu")
        self.assertEqual(len(draw.call_args_list), 61)
        self.assertEqual(draw.call_args_list[-1].args[2], "district_hall 0.90")
        with Image.open(self.output) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(image.width, 2 * 1000)
            self.assertGreater(image.height, 1200)
            with image.crop((0, 0, image.width // 2, image.height)) as uploaded_panel:
                self.assertIn((200, 20, 80), uploaded_panel.getdata())
            with image.crop((image.width // 2, 0, image.width, image.height)) as found_panel:
                self.assertIn((20, 180, 60), found_panel.getdata())

    def test_changed_found_source_is_rejected_before_inference(self):
        found = self.directory / "found.png"
        Image.new("RGB", (1000, 800)).save(found)
        paths = (self.path, found)
        expected = tuple(layout().model_copy(update={
            "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }) for path in paths)
        inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                             district_id=1, pipeline_id=PIPELINE_ID) for path in paths]
        found.write_bytes(b"changed")
        detector = MagicMock()
        with self.assertRaisesRegex(FingerprintError, "source_changed"):
            labels.create_labels(detector, inputs, expected, "cpu")
        detector.predict.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_failed_found_extraction_does_not_publish_an_uploaded_only_image(self):
        source = ImageInput(path=str(self.path), source_revision=0, source_image_url="synthetic",
                            district_id=1, pipeline_id=PIPELINE_ID)
        fp = layout().model_copy(update={"source_image_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        detector = MagicMock()
        with patch("elo_bot.features.bases.fingerprints.extractor.sliced_detections",
                   side_effect=[detections(), detections(points(3))]), \
                self.assertRaisesRegex(FingerprintError, "labels_extraction_failed"):
            labels.create_labels(detector, [source, source], (fp, fp), "cpu")
        self.assertFalse(self.output.exists())

    def test_legacy_source_uses_current_detections_and_changed_source_is_rejected(self):
        source = ImageInput(path=str(self.path), source_revision=0, source_image_url="synthetic",
                            district_id=1, pipeline_id=PIPELINE_ID)
        expected = layout(version=1).model_copy(update={
            "source_image_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        detector = MagicMock()
        with patch("elo_bot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections()) as sliced:
            labels.create_labels(detector, [source, source], (expected, expected), "cpu")
        self.assertTrue(self.output.is_file())
        self.assertEqual(sliced.call_count, 2)
        detector.predict.assert_not_called()
        self.path.write_bytes(b"changed")
        detector.reset_mock()
        with self.assertRaisesRegex(FingerprintError, "source_changed"):
            labels.create_labels(detector, [source, source], (expected, expected), "cpu")
        detector.predict.assert_not_called()

    def test_bad_bounds_and_failed_extraction_do_not_create_an_image(self):
        fp = layout()
        with self.assertRaisesRegex(FingerprintError, "detections_invalid"):
            labels.render_labels(self.path, ExtractionResult(fingerprint=fp), self.output)
        self.assertFalse(self.output.exists())
        source = ImageInput(path=str(self.path), source_revision=0, source_image_url="synthetic",
                            district_id=1, pipeline_id=PIPELINE_ID)
        fp = fp.model_copy(update={"source_image_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()})
        detector = MagicMock()
        with patch("elo_bot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections(points(3))), \
                self.assertRaisesRegex(FingerprintError, "labels_extraction_failed"):
            labels.create_labels(detector, [source, source], (fp, fp), "cpu")
        self.assertFalse(self.output.exists())


class TestLabelService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(service, "SCRATCH", self.root))
        self.service = service.FingerprintService(MagicMock(), "labels-test")
        self.query = layout()
        self.base = SimpleNamespace(fingerprint=layout(version=1), image_url="source", image_attachment=None)

        async def download(_bot, url, _attachment, path):
            path.write_bytes(b"source")
            return url

        self.download = self.enterContext(patch.object(service, "download_image", new=AsyncMock(side_effect=download)))

        async def request(payload):
            Image.new("RGB", (64, 64)).save(Path(payload["images"][0]["path"]).parent / "labels.png")
            return {"labels": True}

        self.service.cpu = SimpleNamespace(request=AsyncMock(side_effect=request))

    async def test_query_and_selected_base_sources_and_attachment_are_sent_to_worker(self):
        for attachment in (None, ImageAttachment(channel_id="1", message_id="2", attachment_id="3")):
            with self.subTest(attachment=attachment):
                self.download.reset_mock()
                query = layout().model_copy(update={"source_image_url": "uploaded"})
                content = await self.service.labels(query, self.base, attachment=attachment)
                self.assertTrue(content.startswith(b"\x89PNG"))
                self.assertEqual([(call.args[1], call.args[2]) for call in self.download.await_args_list],
                                 [("uploaded", attachment), ("source", None)])
                payload = self.service.cpu.request.await_args.args[0]
                self.assertEqual(len(payload["images"]), 2)
                self.assertEqual([source["pipeline_id"] for source in payload["images"]], [PIPELINE_ID] * 2)
                self.assertEqual(payload["labels"], [fp.model_dump(mode="json") for fp in (query, self.base.fingerprint)])
                self.assertNotIn("compare", payload)
                self.assertFalse(list((self.root / "labels-test").iterdir()))

    async def test_found_source_download_failure_cleans_uploaded_source_and_skips_worker(self):
        downloaded = self.download.side_effect

        async def fail_found(bot, url, attachment, path):
            if url == "source":
                raise FingerprintError("source_unavailable")
            return await downloaded(bot, url, attachment, path)

        self.download.side_effect = fail_found
        with self.assertRaisesRegex(FingerprintError, "source_unavailable"):
            await self.service.labels(self.query, self.base)
        self.service.cpu.request.assert_not_awaited()
        self.assertFalse(list((self.root / "labels-test").iterdir()))

    async def test_failure_cancellation_bad_output_and_permissions_are_reportable_and_clean(self):
        self.download.side_effect = FingerprintError("source_unavailable")
        with self.assertRaisesRegex(FingerprintError, "source_unavailable"):
            await self.service.labels(self.query, self.base)
        self.download.side_effect = None
        self.download.return_value = "source"
        for failure in (FingerprintError("worker_busy"), asyncio.CancelledError()):
            self.service.cpu.request.side_effect = failure
            with self.assertRaises(type(failure)):
                await self.service.labels(self.query, self.base)
            self.assertFalse(list((self.root / "labels-test").iterdir()))
        self.service.cpu.request.side_effect = None
        for payload in ({}, {"labels": "true"}, {"labels": True}):
            self.service.cpu.request.return_value = payload
            with self.assertRaises(FingerprintError):
                await self.service.labels(self.query, self.base)
            self.assertFalse(list((self.root / "labels-test").iterdir()))
        with patch.object(Path, "mkdir", side_effect=PermissionError):
            with self.assertRaisesRegex(FingerprintError, "labels_image_unavailable"):
                await self.service.labels(self.query, self.base)


class TestLabelControls(unittest.IsolatedAsyncioTestCase):
    context = fixture.FingerprintSearchStateMixin.context
    search = fixture.FingerprintSearchStateMixin.search

    async def asyncSetUp(self):
        fixture.FingerprintSearchStateMixin.setUp(self)
        self.labels = AsyncMock(return_value=b"png")
        self.bot.base_fingerprints.labels = self.labels
        await self.search(self.context())
        self.session = next(iter(self.cog._similarity.sessions.values()))

    def click(self, owner=123, message=777):
        ctx = self.context(owner, message)
        ctx.custom_id = f"base_labels|{self.session.token}|{self.bases[0].id}"
        return ctx

    async def invoke(self, ctx):
        await self.cog.similar_labels.callback.func(self.cog, ctx)

    async def test_ephemeral_labeled_image_and_read_only_state(self):
        before = [base.model_dump() for base in self.bases]
        ctx = self.click()
        await self.invoke(ctx)
        payload = ctx.send.await_args.kwargs
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["file"].file_name, "base-labels.png")
        self.assertEqual(set(payload), {"file", "ephemeral"})
        self.labels.assert_awaited_once_with(self.session.query, self.bases[0], attachment=self.session.query_attachment)
        self.assertEqual([base.model_dump() for base in self.bases], before)
        self.assertIsNone(self.pending.resolution)
        ctx.message.edit.assert_not_awaited()

    async def test_labels_follow_selected_page_and_preserve_uploaded_attachment_identity(self):
        self.session.page = 1
        self.session.query_attachment = ImageAttachment(channel_id="1", message_id="2", attachment_id="3")
        ctx = self.click()
        ctx.custom_id = f"base_labels|{self.session.token}|{self.bases[1].id}"
        await self.invoke(ctx)
        self.labels.assert_awaited_once_with(self.session.query, self.bases[1],
                                             attachment=self.session.query_attachment)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("file", ctx.send.await_args.kwargs)

    async def test_page_change_during_label_rendering_blocks_delivery(self):
        async def change_page(*_args, **_kwargs):
            self.session.page = 1
            return b"png"

        self.labels.side_effect = change_page
        ctx = self.click()
        await self.invoke(ctx)
        self.assertNotIn("file", ctx.send.await_args.kwargs)
        self.assertFalse(self.cog._similarity.comparing)

    async def test_other_owner_message_stale_page_expiry_and_roles_block_labels(self):
        for ctx in (self.click(owner=456), self.click(message=9001)):
            await self.invoke(ctx)
            self.assertIn("belong", ctx.send.await_args.args[0])
        ctx = self.click()
        self.session.page = 1
        await self.invoke(ctx)
        self.assertIn("Use Show Labels on the current page", ctx.send.await_args.args[0])
        self.session.page = 0
        ctx.author.has_any_role = lambda _: False
        await self.invoke(ctx)
        self.assertIn("no longer have access", ctx.send.await_args.args[0])
        self.session.expires_at = 0
        await self.invoke(self.click())
        self.labels.assert_not_awaited()

    async def test_private_revised_deleted_and_role_revoked_during_work_block_delivery(self):
        for changed in ("private", "revised", "deleted", "role"):
            ctx = self.click()
            base = self.bases[0]

            async def render(_query, _base, **_kwargs):
                if changed == "private":
                    base.quality = "Private"
                elif changed == "revised":
                    base.image_revision = 1
                elif changed == "deleted":
                    self.bases.remove(base)
                else:
                    ctx.author.has_any_role = lambda _: False
                return b"png"

            self.labels.side_effect = render
            await self.invoke(ctx)
            self.assertIn("Labels unavailable", ctx.send.await_args.args[0])
            self.assertNotIn("file", ctx.send.await_args.kwargs)
            self.assertFalse(self.cog._similarity.comparing)
            base.quality, base.image_revision = "Good", 0
            if changed == "deleted":
                self.bases.insert(0, base)

    async def test_pending_guard_failures_and_closed_reviews_preserve_controls(self):
        ctx = self.click()
        self.cog._similarity.comparing.add(self.session.token)
        await self.invoke(ctx)
        self.assertIn("already pending", ctx.send.await_args.args[0])
        self.labels.assert_not_awaited()
        self.cog._similarity.comparing.clear()
        self.labels.side_effect = FingerprintError("source_unavailable")
        await self.invoke(ctx)
        self.assertIn("Labels unavailable", ctx.send.await_args.args[0])
        self.labels.side_effect = None
        ctx.send.side_effect = [ipy.errors.HTTPException(MagicMock(status=500)), None]
        await self.invoke(ctx)
        self.assertIn("could not be delivered", ctx.send.await_args.args[0])
        ctx.send.side_effect = None
        self.pending.resolution = SimpleNamespace(phase="rejected")
        await self.invoke(ctx)
        self.assertEqual(set(ctx.send.await_args.kwargs), {"file", "ephemeral"})
        self.assertEqual(self.pending.resolution.phase, "rejected")
        ctx.message.edit.assert_not_awaited()
        self.assertFalse(self.cog._similarity.comparing)

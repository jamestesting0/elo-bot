import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image, ImageChops, ImageDraw

from elobot.features.bases.fingerprints import comparison, presentation, service
from elobot.features.bases.fingerprints.extractor import extract_batch
from elobot.features.bases.fingerprints.matcher import align_layout, score_layout
from elobot.features.bases.fingerprints.types import (
    ComparisonSummary, ExtractionResult, FingerprintError, ImageAttachment, ImageInput, PIPELINE_ID, PREVIOUS_PIPELINE_ID,
)
from tests._relative_geometry_fixture import layout, points
from tests._relative_geometry_fixture import prediction
from tests._base_extraction_fixture import detections


from tests._relative_geometry_fixture import summary

class TestComparisonGeometry(unittest.TestCase):
    def test_headings_and_frame_preserve_both_complete_screenshot_panels(self):
        with tempfile.TemporaryDirectory() as directory, \
                Image.new("RGB", (400, 320), (10, 120, 210)) as uploaded, \
                Image.new("RGB", (500, 480), (150, 40, 90)) as found:
            output = Path(directory) / "panels.png"
            presentation.render_panels((uploaded, found), output)
            with Image.open(output) as image:
                for panel, box in ((uploaded, (78, 144, 478, 464)), (found, (568, 64, 1068, 544))):
                    with image.crop(box) as screenshot, ImageChops.difference(panel, screenshot) as difference:
                        self.assertIsNone(difference.getbbox())
                with image.crop((16, 16, 540, 64)) as heading:
                    self.assertGreater(len(set(heading.getdata())), 3)

    def test_correspondences_keep_original_indices_across_transform_shuffle_and_pair_reversal(self):
        query = layout(hall=True)
        candidate = layout([(name, x * 1.1 + .02, y * 1.1 + .01) for name, x, y in reversed(points()[2:])])
        expected = {(index, 29 - index) for index in range(2, 30)}
        result = align_layout(query, candidate, False)
        self.assertEqual(set(result.pairs), expected)
        self.assertEqual(result.score, score_layout(query, candidate, False))
        self.assertEqual(set(align_layout(candidate, query, False).pairs), {(c, q) for q, c in expected})
        self.assertEqual(align_layout(layout(points(19)), candidate, False).pairs, ())

    def test_fuzzy_correspondences_follow_search_class_grouping(self):
        candidate = layout([("spear" if name == "cannon" else name, x, y) for name, x, y in points()])
        self.assertEqual(len(align_layout(layout(), candidate, True).pairs), 30)
        self.assertEqual(len(align_layout(layout(), candidate, False).pairs), 24)

    def test_brightness_only_highlights_differences_and_dims_matches(self):
        fingerprint = layout()
        bounds = [(b.x - .012, b.y - .015, b.x + .012, b.y + .015) for b in fingerprint.buildings]
        result = ExtractionResult(fingerprint=fingerprint, bounds=bounds)
        with Image.new("RGB", (1000, 800), (25, 150, 60)) as original:
            draw = ImageDraw.Draw(original)
            for b in fingerprint.buildings:
                draw.rectangle((b.x * 1000 - 8, b.y * 800 - 8, b.x * 1000 + 8, b.y * 800 + 8),
                               fill=(30, 100, 220))
            panel = comparison.difference_panel(original, result, set(range(2, 30)))
        self.addCleanup(panel.close)
        for index in (0, 1):
            b = fingerprint.buildings[index]
            self.assertEqual(panel.getpixel((round(b.x * 800), round(b.y * 640))), (36, 120, 255))
        b = fingerprint.buildings[10]
        pixel = panel.getpixel((round(b.x * 800), round(b.y * 640)))
        self.assertEqual(pixel, (25, 84, 184))
        self.assertNotIn((247, 215, 120), panel.getdata())

    def test_extraction_bounds_follow_selected_attempt_and_do_not_change_fingerprint_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            Image.new("RGB", (1000, 800)).save(path)
            source = ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                                district_id=1, pipeline_id=PREVIOUS_PIPELINE_ID)
            detector = MagicMock()
            detector.predict.side_effect = [[prediction(points(30), confidence=.8)],
                                            [prediction(points(31), confidence=.95)],
                                            [prediction(points(30), confidence=.7)]]
            result = extract_batch(detector, [source], "cpu", include_bounds=True)[0]
            self.assertEqual(len(result.bounds), 31)
            self.assertEqual(result.fingerprint.total_detections, 31)
            self.assertNotIn("bounds", result.fingerprint.model_dump())
            for building, (x1, y1, x2, y2) in zip(result.fingerprint.buildings, result.bounds, strict=True):
                self.assertAlmostEqual(building.x, (x1 + x2) / 2)
                self.assertAlmostEqual(building.y, (y1 + y2) / 2)

    def test_real_extractor_comparison_accepts_legacy_source_without_a_hall_and_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(index) for index in range(2)]
            for path in paths:
                Image.new("RGB", (1000, 800), (25, 120, 50)).save(path, format="PNG")
            expected = tuple(layout(version=version).model_copy(update={
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }) for version, path in zip((2, 1), paths, strict=True))
            inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                                 district_id=1, pipeline_id=PIPELINE_ID) for path in paths]
            detector = MagicMock()
            with patch("elobot.features.bases.fingerprints.extractor.sliced_detections",
                       side_effect=[detections(), detections(points()[2:])]) as sliced:
                result = comparison.create_comparison(detector, inputs, expected, "cpu", False)
            self.assertEqual((result.query_unmatched, result.candidate_unmatched), (2, 0))
            self.assertEqual(result.score.matched, 28)
            self.assertEqual(sliced.call_count, 2)
            detector.predict.assert_not_called()
            with Image.open(Path(directory) / "comparison.png") as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (1696, 732))
            paths[0].write_bytes(b"changed source")
            detector.reset_mock()
            with self.assertRaisesRegex(FingerprintError, "source_changed"):
                comparison.create_comparison(detector, inputs, expected, "cpu", False)
            detector.predict.assert_not_called()

    def test_sparse_fresh_detections_still_render_without_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(index) for index in range(2)]
            for path in paths:
                Image.new("RGB", (1000, 800)).save(path, format="PNG")
            expected = tuple(layout().model_copy(update={
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }) for path in paths)
            inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                                 district_id=1, pipeline_id=PIPELINE_ID) for path in paths]
            detector = MagicMock()
            with patch("elobot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections(points(7))):
                result = comparison.create_comparison(detector, inputs, expected, "cpu", False)
            self.assertEqual(result.score.matched, 7)
            self.assertEqual(result.warnings, [])
            self.assertTrue((Path(directory) / "comparison.png").exists())

    def test_zero_overlap_highlights_all_detections_on_both_screenshots_without_warnings(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(index) for index in range(2)]
            for path in paths:
                with Image.new("RGB", (1000, 800), (25, 150, 60)) as image:
                    image.save(path, format="PNG")
            expected = tuple(layout().model_copy(update={
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }) for path in paths)
            inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                                 district_id=1, pipeline_id=PIPELINE_ID) for path in paths]
            different = [("inferno_tower", x, y) for _, x, y in points()]

            with patch("elobot.features.bases.fingerprints.extractor.sliced_detections",
                       side_effect=[detections(), detections(different)]):
                result = comparison.create_comparison(MagicMock(), inputs, expected, "cpu", False)

            self.assertEqual((result.score.matched, result.query_unmatched, result.candidate_unmatched), (0, 30, 30))
            self.assertEqual(result.warnings, [])
            with Image.open(Path(directory) / "comparison.png") as image:
                self.assertEqual(image.size, (1696, 732))
                bright = (30, 180, 72)
                for box in ((24, 64, 824, 704), (872, 64, 1672, 704)):
                    with image.crop(box) as panel:
                        self.assertGreater(list(panel.getdata()).count(bright), 1000)

    def test_empty_fresh_detections_still_produce_both_panels(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / str(index) for index in range(2)]
            for path in paths:
                with Image.new("RGB", (1000, 800), (25, 150, 60)) as image:
                    image.save(path, format="PNG")
            expected = tuple(layout().model_copy(update={
                "source_image_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }) for path in paths)
            inputs = [ImageInput(path=str(path), source_revision=0, source_image_url="synthetic",
                                 district_id=1, pipeline_id=PIPELINE_ID) for path in paths]

            with patch("elobot.features.bases.fingerprints.extractor.sliced_detections", return_value=()):
                result = comparison.create_comparison(MagicMock(), inputs, expected, "cpu", False)

            self.assertEqual((result.score.matched, result.score.total), (0, 0))
            self.assertEqual(result.warnings, [])
            self.assertEqual(ComparisonSummary.model_validate_json(result.model_dump_json()), result)
            with Image.open(Path(directory) / "comparison.png") as image:
                self.assertEqual(image.size, (1696, 732))


class TestComparisonService(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(service, "SCRATCH", self.root))
        self.service = service.FingerprintService(MagicMock(), "test")
        self.query = layout()
        self.attachment = ImageAttachment(channel_id="1", message_id="2", attachment_id="3")
        self.base = SimpleNamespace(fingerprint=layout(version=1), image_url="candidate", image_attachment=None)

        async def download(_bot, url, _attachment, path):
            path.write_bytes(b"downloaded")
            return url

        self.download = self.enterContext(patch.object(service, "download_image", new=AsyncMock(side_effect=download)))

        async def request(payload):
            path = Path(payload["images"][0]["path"]).parent / "comparison.png"
            Image.new("RGB", (64, 64)).save(path)
            return {"comparison": summary().model_dump()}

        self.service.cpu = SimpleNamespace(request=AsyncMock(side_effect=request))

    async def compare(self):
        return await self.service.compare(self.query, self.base, fuzzy=True, attachment=self.attachment)

    def assert_clean(self):
        self.assertFalse(list(self.root.rglob("compare-*")))

    async def test_success_refreshes_exact_sources_uses_v2_and_cleans_scratch(self):
        content, result = await self.compare()
        self.assertTrue(content.startswith(b"\x89PNG"))
        self.assertEqual(result.query_unmatched, 2)
        self.assertIs(self.download.await_args_list[0].args[2], self.attachment)
        payload = self.service.cpu.request.await_args.args[0]
        self.assertEqual([source["pipeline_id"] for source in payload["images"]], [PIPELINE_ID] * 2)
        self.assertEqual([fp["schema_version"] for fp in payload["compare"]], [2, 1])
        self.assertTrue(payload["fuzzy"])
        self.assert_clean()

    async def test_download_worker_cancellation_and_invalid_output_all_clean_scratch(self):
        self.download.side_effect = FingerprintError("source_unavailable")
        with self.assertRaisesRegex(FingerprintError, "source_unavailable"):
            await self.compare()
        self.assert_clean()
        self.download.side_effect = None
        self.download.return_value = "synthetic"
        for failure in (FingerprintError("worker_busy"), asyncio.CancelledError()):
            self.service.cpu.request.side_effect = failure
            with self.assertRaises(type(failure)):
                await self.compare()
            self.assert_clean()

        self.service.cpu.request.side_effect = None
        for payload in ({}, {"comparison": {}}, {"comparison": summary().model_dump()}):
            self.service.cpu.request.return_value = payload
            with self.assertRaises(FingerprintError):
                await self.compare()
            self.assert_clean()

    async def test_unwritable_scratch_returns_reportable_failure(self):
        with patch.object(Path, "mkdir", side_effect=PermissionError("not writable")):
            with self.assertRaisesRegex(FingerprintError, "comparison_image_unavailable"):
                await self.compare()
        self.assert_clean()

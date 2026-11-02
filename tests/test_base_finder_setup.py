import hashlib
import io
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from common.base_fingerprints import artifacts
from common.base_fingerprints.types import FingerprintError
from scripts import setup_base_finder as setup


class Response(io.BytesIO):
    url = "https://pinned.example/commit/best.pt"
    headers = {"Content-Type": "application/octet-stream"}


class TestBaseFinderSetup(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "commit" / "best.pt"
        self.content = b"verified test model"
        digest = hashlib.sha256(self.content).hexdigest()
        manifest = dict(artifacts.MANIFEST, bytes=len(self.content), sha256=digest, url=Response.url)
        self.enterContext(patch.object(artifacts, "MODEL_PATH", self.path))
        self.enterContext(patch.object(artifacts, "MODEL_SHA256", digest))
        self.enterContext(patch.object(artifacts, "MANIFEST", manifest))
        self.enterContext(patch.object(setup, "MODEL_PATH", self.path))
        self.enterContext(patch.object(setup, "MANIFEST", manifest))

    def test_concurrent_installers_verify_and_reuse_one_artifact(self):
        with patch.object(setup.urllib.request, "urlopen", side_effect=lambda *args, **kwargs: Response(self.content)) as get:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: setup.download(), range(2)))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.path.read_bytes(), self.content)
        self.assertEqual(list(self.path.parent.glob("*.partial")), [])
        self.assertEqual(artifacts.verify_model(), self.path)

    def test_checksum_interruption_html_and_oversize_never_install(self):
        for content in (b"wrong hash payload!", b"x" * 100):
            with self.subTest(content=content), patch.object(setup.urllib.request, "urlopen", return_value=Response(content)):
                with self.assertRaises(FingerprintError):
                    setup.download()
                self.assertFalse(self.path.exists())
                self.assertEqual(list(self.path.parent.glob("*.partial")), [])
        with patch.object(setup.urllib.request, "urlopen", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                setup.download()
        response = Response(self.content)
        response.headers = {"Content-Type": "text/html"}
        with patch.object(setup.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(FingerprintError):
                setup.download()
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob("*.partial")), [])

    def test_valid_installed_model_is_not_touched_when_network_is_down(self):
        self.path.parent.mkdir()
        self.path.write_bytes(self.content)
        with patch.object(setup.urllib.request, "urlopen", side_effect=OSError("offline")) as get:
            setup.download()
        get.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.content)

    def test_modified_manifest_is_rejected(self):
        self.path.parent.mkdir()
        self.path.write_bytes(self.content)
        for field in ("MANIFEST_SHA256", "LEGACY_MANIFEST_SHA256",
                      "PREVIOUS_MANIFEST_SHA256", "PREVIOUS_SAHI_MANIFEST_SHA256"):
            with self.subTest(field=field), patch.object(artifacts, field, "0" * 64), self.assertRaisesRegex(
                FingerprintError, "pipeline_manifest_mismatch"
            ):
                artifacts.verify_model()

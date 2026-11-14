import unittest
from unittest.mock import AsyncMock

from elo_bot.features.bases.fingerprints.runtime import InferenceWorker
from elo_bot.features.bases.fingerprints.types import FingerprintError, ImageInput
from elo_bot.db.fingerprint_types import PIPELINE_ID
from tests._relative_geometry_fixture import layout


class TestRelativeRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_or_misassociated_extraction_never_returns_success(self):
        worker = InferenceWorker("cpu", "isolated")
        source = ImageInput(path="synthetic", source_revision=0, source_image_url="synthetic", district_id=1,
                            pipeline_id=PIPELINE_ID)
        valid = layout().model_dump(mode="json")
        wrong = layout(version=1).model_dump(mode="json")
        for response in ({}, {"results": None}, {"results": [{}]}, {"results": []},
                         {"results": [{"fingerprint": valid, "error": "failure"}]},
                         {"results": [{"fingerprint": wrong}]},
                         {"results": [{"fingerprint": valid | {"source_revision": 1}}]}):
            with self.subTest(response=response):
                worker.request = AsyncMock(return_value=response)
                with self.assertRaises(FingerprintError):
                    await worker.extract([source])

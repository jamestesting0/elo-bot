from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import interactions as ipy
from bson import ObjectId
from elo_bot.features.bases.fingerprints.types import MatchScore
from elo_bot.config.ids import BASE_SUBMISSION_CHANNEL
from elo_bot.db import BaseLayout, PendingBaseSubmission
from tests._base_fingerprint_fixture import fingerprint
import elo_bot.extensions.base as base_module


class FingerprintSearchStateMixin:
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.user = SimpleNamespace(id=1234)
        self.bot.base_fingerprints = SimpleNamespace(query=AsyncMock(return_value=fingerprint()))
        self.cog = base_module.BaseCommands(self.bot)
        self.bases = [BaseLayout.model_construct(
            id=ObjectId(), district="Barbarian Camp", link=f"https://base/{index}",
            image_url=f"https://image/{index}", quality="Good", expiration_date=0,
            builder_id="123", fingerprint=fingerprint(), image_revision=0,
        ) for index in range(3)]
        self.scores = [MatchScore(base_id=str(base.id), source_revision=0, similarity=100-index,
                                  matched=2, total=2) for index, base in enumerate(self.bases)]
        self.pending = PendingBaseSubmission.model_construct(
            id="9001", review_channel_id=str(BASE_SUBMISSION_CHANNEL), district="Barbarian Camp",
            image_url="https://review/image", resolution=None,
        )
        self.enterContext(patch.object(PendingBaseSubmission, "get", new=AsyncMock(return_value=self.pending)))

        async def visible(query):
            return next((base for base in self.bases if base.id == query["_id"] and base.quality != "Private"), None)

        self.enterContext(patch.object(BaseLayout, "find_one", new=AsyncMock(side_effect=visible)))
        self.find = self.enterContext(patch('elo_bot.extensions.base.search.find_matches', new=AsyncMock(
            return_value=(self.scores, 3, 3))))

    def context(self, owner=123, message=9001):
        return SimpleNamespace(author=SimpleNamespace(id=owner, has_any_role=lambda roles: True),
                               guild=SimpleNamespace(fetch_member=AsyncMock(
                                   return_value=SimpleNamespace(id=123, display_name="Builder"))),
                               channel_id=BASE_SUBMISSION_CHANNEL, message=SimpleNamespace(id=message, edit=AsyncMock()),
                               defer=AsyncMock(), send=AsyncMock(return_value=SimpleNamespace(id=777)),
                               edit_origin=AsyncMock(), custom_id="")

    async def search(self, ctx):
        await self.cog.find_similar_submission.callback.func(self.cog, ctx)

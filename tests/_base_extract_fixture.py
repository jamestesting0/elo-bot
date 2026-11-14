from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import interactions as ipy
from beanie import PydanticObjectId
from elobot.features.bases.fingerprints.types import ExtractionState
from tests._patch_sites import patch_sites
import elobot.extensions.base as base_module


class BaseExtractStateMixin:
    def setUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.owner = SimpleNamespace(id=999)
        self.bot.builder_names = {"123": "@builder"}
        self.state = ExtractionState(source_revision=4)
        self.bot.base_fingerprints = SimpleNamespace(request_extraction=AsyncMock(return_value=(self.state, True)))
        self.cog = base_module.BaseCommands(self.bot)
        self.base = SimpleNamespace(id=PydanticObjectId(), district="Wizard Valley", builder_id="123",
                                    link="https://link.clashofclans.com/base", fingerprint_extraction=self.state)
        self.query = MagicMock()
        self.query.find.return_value.first_or_none = AsyncMock(return_value=self.base)
        self.query.sort.return_value.project.return_value.to_list = AsyncMock(return_value=[self.base])
        self.model = self.enterContext(patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout')))
        self.model.find.return_value = self.query
        self.model.find_all.return_value = self.query
        self.tasks = self.enterContext(patch('elobot.extensions.base.extraction.BaseFingerprintTask', MagicMock(get=AsyncMock(return_value=None))))
        self.runs = self.enterContext(patch('elobot.extensions.base.extraction.BaseExtractionRun', MagicMock(get=AsyncMock(return_value=None))))

    def context(self, *, staff=True, input_text=""):
        author = SimpleNamespace(id=123, has_any_role=lambda roles: staff, has_role=lambda role: staff)
        return SimpleNamespace(author=author, guild=MagicMock(), input_text=input_text,
                               defer=AsyncMock(), send=AsyncMock())

    async def invoke(self, ctx, selection=None):
        await self.cog.base_extract.callback.func(self.cog, ctx, selection or str(self.base.id))

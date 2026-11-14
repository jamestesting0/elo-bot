import asyncio

import anyio

import interactions as ipy
from elobot.config.ids import BOT_ID
from elobot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elobot.features.bases.fingerprints.search import SearchSessions
from elobot.extensions.base import groups
from elobot.extensions.base.extraction import BaseExtractionCommands
from elobot.extensions.base.search import BaseSearchCommands
from elobot.extensions.base.submissions import BaseSubmissionCommands
from elobot.extensions.base.manage import BaseManagementCommands
from elobot.extensions.base.listing import BaseListCommands
from elobot.extensions.base.private import PrivateBaseCommands
from elobot.extensions.base.leaderboard import BaseLeaderboardCommands



class BaseCommands(BaseExtractionCommands, BaseSearchCommands, BaseSubmissionCommands, BaseManagementCommands, BaseListCommands, PrivateBaseCommands, BaseLeaderboardCommands, ipy.Extension):
    # Class attributes so interactions.py registers the command and its groups with this extension.
    base = groups.base
    set_group = groups.set_group
    private_group = groups.private_group

    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self.helper_api_base_url = HELPER_API_BASE_URL
        self.helper_api_key = HELPER_API_KEY
        self._review_locks: dict[int, anyio.Lock] = {}
        self._similarity = SearchSessions()
        self._review_refresh_cursor = None
        self._review_refresh_retry_after: dict[str, float] = {}






























































    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        print("➤ Base commands loaded")
        asyncio.create_task(self.warm_builder_names())
        self.bot.base_fingerprints.start()
        self.refresh_similarity_buttons.start()

        if int(self.bot.user.id) == BOT_ID:
            await self.base_expiration_warning()

        self.base_expiration_warning.start()

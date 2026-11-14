import asyncio  # noqa: ANYIO_OK - interactions.py runtime and the requested registry use asyncio locks
import anyio

import interactions as ipy
from elobot.config.ids import BOT_ID
from elobot.config.settings import HELPER_API_BASE_URL, HELPER_API_KEY
from elobot.db import ServerState
import coc
from datetime import datetime
from elobot.features.base_store.match_adapters import sync_base_store_slots
from elobot.extensions.match import groups
from elobot.extensions.match.queue import QueueCommands
from elobot.extensions.match.lifecycle import MatchLifecycleCommands
from elobot.extensions.match.bases import MatchBaseCommands
from elobot.extensions.match.base_store import MatchBaseStoreCommands
from elobot.extensions.match.results import MatchResultCommands
from elobot.extensions.match.hosting import MatchHostingCommands
from elobot.extensions.match.dashboards import MatchDashboardTasks


class MatchCommands(QueueCommands, MatchLifecycleCommands, MatchBaseCommands, MatchBaseStoreCommands, MatchResultCommands, MatchHostingCommands, MatchDashboardTasks, ipy.Extension):
    # Class attributes so interactions.py registers the /match command and its groups with this extension.
    base = groups.base
    base_group = groups.base_group
    war_group = groups.war_group
    set_group = groups.set_group
    result_group = groups.result_group

    def __init__(self, bot):
        self.bot: ipy.Client = bot
        self.helper_api_base_url = HELPER_API_BASE_URL
        self.helper_api_key = HELPER_API_KEY
        self._pending_match_thread_renames: dict[str, asyncio.Task[None]] = {}
        self._hosting_queue_embed_update_lock = asyncio.Lock()
        self._match_publication_repair_lock = anyio.Lock()

    @property
    def coc_client(self) -> coc.Client:
        return self.bot.coc_client






































































































    @ipy.listen(ipy.events.Startup)
    async def on_start(self):
        await sync_base_store_slots(bot=self.bot)
        print("➤ Match commands loaded")

        is_main_bot = int(self.bot.user.id) == BOT_ID
        if is_main_bot:
            await self.inactive_threads_delete()
        await self.auto_match_confirm()
        await self.auto_match_cancel()
        await self.match_confirmation_remind()
        await self.base_notify()
        await self.host_notify()
        await self.match_data_clean_up()
        await self.repair_match_publications()
        await self.update_clan_availability_embed()
        await self.update_hosting_queue_embed()

        if is_main_bot:
            self.inactive_threads_delete.start()
        self.auto_match_confirm.start()
        self.auto_match_cancel.start()
        self.match_confirmation_remind.start()
        self.base_notify.start()
        self.host_notify.start()
        self.match_data_clean_up.start()
        self.repair_match_publications.start()
        self.raid_weekend_start_notify.start()
        self.raid_weekend_auto_store.start()
        self.update_clan_availability_embed.start()
        self.update_hosting_queue_embed.start()

        last_matching_timestamp = (await ServerState.current()).last_matching
        if not last_matching_timestamp:
            await self.create_matches()
        else:
            last_matching = datetime.fromtimestamp(last_matching_timestamp)
            now = datetime.now()
            diff = (now - last_matching).seconds
            if diff < 10800:
                await asyncio.sleep(10800 - diff)

            await self.create_matches()

        self.create_matches.start()

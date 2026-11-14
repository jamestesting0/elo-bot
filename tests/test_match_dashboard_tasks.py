import asyncio
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch
import anyio
import interactions as ipy
from elo_bot.db import BaseStore, ServerState
from tests._patch_sites import patch_sites
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    @patch_sites(('elo_bot.extensions.match.dashboards.HelperTask', 'elo_bot.features.matches.resources.HelperTask'))
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_hosting_queue_embed_shows_only_helper_tasks(
            self, mock_server_state_current, mock_helper_task):
        mock_server_state_current.return_value = MagicMock(
            hosting_queue_embed=MagicMock(channel=123, message=456)
        )
        embed = ipy.Embed(title="**Hosting Queue**", description="old")
        message = MagicMock(embeds=[embed])
        message.edit = AsyncMock()
        queue_channel = MagicMock()
        queue_channel.fetch_message = AsyncMock(return_value=message)
        queue_channel.guild.fetch_member = AsyncMock(return_value=MagicMock(id=111, display_name="Player"))
        self.bot.fetch_channel = AsyncMock(side_effect=[queue_channel, MagicMock()])

        task = MagicMock(thread=987, player_id="111", timestamp=3000)
        mock_helper_task.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[task])

        await self.cog.update_hosting_queue_embed.callback.func(self.cog)

        timestamp = ipy.Timestamp.fromtimestamp(3000).format(ipy.TimestampStyles.RelativeTime)
        message.edit.assert_awaited_once()
        self.assertEqual(message.edit.await_args.kwargs["embed"]["description"],
                         f"<#987> **[Player](<https://discordapp.com/users/111>)** {timestamp}\n")

    @patch_sites(('elo_bot.extensions.match.dashboards.HelperTask', 'elo_bot.features.matches.resources.HelperTask'))
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_hosting_queue_embed_avoids_concurrent_message_fetches(
            self, mock_server_state_current, mock_helper_task):
        # Given an update that is still waiting for the queue embed message
        mock_server_state_current.return_value = MagicMock(
            hosting_queue_embed=MagicMock(channel=123, message=456)
        )
        message = MagicMock(embeds=[MagicMock(description="")])
        message.edit = AsyncMock()
        first_fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()
        overlap_detected = asyncio.Event()
        active_fetches = 0

        async def fetch_message(_message_id: int) -> MagicMock:
            nonlocal active_fetches
            active_fetches += 1
            if active_fetches > 1:
                overlap_detected.set()
                release_fetch.set()
            first_fetch_started.set()
            with anyio.fail_after(5):
                await release_fetch.wait()
            active_fetches -= 1
            return message

        queue_channel = MagicMock()
        queue_channel.fetch_message = AsyncMock(side_effect=fetch_message)
        self.bot.fetch_channel = AsyncMock(return_value=queue_channel)
        mock_helper_task.find.return_value.sort.return_value.to_list = AsyncMock(return_value=[])

        # When the scheduler starts another update before the first one finishes
        async def run_scheduled_update() -> None:
            await self.cog.update_hosting_queue_embed.callback.func(self.cog)
            release_fetch.set()

        scheduled_update = ipy.Task(run_scheduled_update, ipy.IntervalTrigger(seconds=15))
        with anyio.fail_after(5):
            first_update = scheduled_update._fire(datetime.now())
            await first_fetch_started.wait()
            second_update = scheduled_update._fire(datetime.now())
            await asyncio.gather(first_update, second_update)

        # Then only the active update may fetch the Discord message
        self.assertFalse(overlap_detected.is_set())
        queue_channel.fetch_message.assert_awaited_once_with(456)

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'), new_callable=AsyncMock)
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch_sites(('elo_bot.extensions.match.dashboards.Clan', 'elo_bot.features.matches.resources.Clan'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch.object(ServerState, "current", new_callable=AsyncMock)
    async def test_update_clan_availability_embed_shows_slots_outside_raid_weekend(
            self, mock_server_state_current, mock_match, mock_clan, mock_is_raid_weekend, mock_sync_slots):
        mock_server_state_current.return_value = MagicMock(
            clan_availability_embed=MagicMock(channel=123, message=456)
        )

        field = ipy.EmbedField(name="**Test Clan (#CLAN)**", value="old")
        embed = ipy.Embed(title="**Clan Availability**", fields=[field])
        message = MagicMock(embeds=[embed])
        message.edit = AsyncMock()
        channel = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        channel.guild.fetch_member = AsyncMock(return_value=MagicMock(id=123, display_name="Player"))
        self.bot.fetch_channel = AsyncMock(return_value=channel)

        clan = MagicMock()
        clan.tag = "#CLAN"
        clan.availability_str = "Available"
        clan.available_slots = ["Slot 2"]
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        stored_match = MagicMock(thread="987")
        stored_match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        mock_match.find.return_value.to_list = AsyncMock(return_value=[stored_match])

        await self.cog.update_clan_availability_embed.callback.func(self.cog)

        mock_sync_slots.assert_awaited_once_with(bot=self.bot)
        self.assertEqual(
            message.edit.await_args.kwargs["embed"]["fields"][0]["value"],
            "Available\nSlot 1: <#987> **[Player](<https://discordapp.com/users/123>)**",
        )
        message.edit.assert_awaited_once()
        mock_is_raid_weekend.assert_not_called()

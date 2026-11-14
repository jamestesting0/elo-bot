from unittest.mock import MagicMock, AsyncMock, patch
import interactions as ipy
from elo_bot.db import Match, OverrideCode
from elo_bot.config.ids import BOT_ID
from tests._patch_sites import patch_sites
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_remove_success(self, mock_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # Mock match
        mock_match_doc = MagicMock(spec=Match)
        mock_base_obj = MagicMock()
        mock_base_obj.link = "http://link"
        mock_match_doc.participants = [
            MagicMock(user_id="123", bases=[mock_base_obj, None, None]),
            MagicMock(user_id="456", bases=[None, None, None])
        ]
        mock_match_doc.is_war = True
        mock_match_doc.hosts = [None, None]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        # Execute
        await self.cog.match_base_remove.callback(ctx, player=1, district="Barbarian Camp")

        # Verify
        mock_match_doc.save_changes.assert_called_once()
        self.assertIsNone(mock_match_doc.participants[0].bases[0])
        ctx.send.assert_called()
        self.assertIn("removed", ctx.send.call_args[0][0])

    @patch('elo_bot.extensions.match.bases.validators')
    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_set_image_success(self, mock_match, mock_base_layout, mock_validators):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999

        mock_validators.url.return_value = True

        # Mock Match
        mock_match_doc = MagicMock()
        mock_base = MagicMock()
        mock_match_doc.participants = [
            MagicMock(user_id="111", bases=[mock_base, None, None]),
            MagicMock(user_id="222")
        ]
        mock_match_doc.hosts = [str(ctx.author.id)]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match_doc.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        attachment = MagicMock(spec=ipy.Attachment)
        attachment.url = "http://image.url"
        attachment.content_type = "image/png"

        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        # Mock download_file (imported in elo_bot.extensions.match)
        self.bot.owner.send = AsyncMock()
        self.bot.owner.send.return_value.attachments = [MagicMock(url="http://new_image")]

        with patch('elo_bot.extensions.match.bases.download_file', new=AsyncMock(return_value=True)):
             with patch('elo_bot.extensions.match.bases.delete_file', new=AsyncMock()):
                 await self.cog.match_base_set_image.callback(ctx, player=0, district="Barbarian Camp", base_ss=attachment)

        # Verify
        mock_match_doc.save_changes.assert_called_once()
        ctx.send.assert_called()
        self.assertIn("set", ctx.send.call_args[0][0])

    @patch('elo_bot.extensions.match.bases.validators')
    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_set_success(self, mock_match, mock_base_layout, mock_validators):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999

        mock_validators.url.return_value = True
        mock_base_layout.find_one = AsyncMock(return_value=None) # No existing base

        # Mock Match
        mock_match_doc = MagicMock()
        mock_match_doc.participants = [MagicMock(user_id="111"), MagicMock(user_id="222")]
        mock_match_doc.hosts = [str(ctx.author.id)]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match_doc.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        # Execute
        # Link must be valid per code logic
        # 3A1 is Barbarian Camp, encoded as %3A1 in the ID part (because split('%')[2] gets 3A1)
        await self.cog.match_base_set.callback(ctx, player=123, base_link="https://link.clashofclans.com/?action=OpenLayout&id=TH15%3ACC%3A1%3AAAAAA", base_ss=None)

        # Verify
        mock_match_doc.save_changes.assert_called()
        ctx.send.assert_called()
        self.assertIn("set", ctx.send.call_args[0][0])

    @patch('elo_bot.extensions.match.bases.validators')
    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_submit_success(self, mock_match, mock_base_layout, mock_validators):
        for scheduled_time in (3000000000, None):
            with self.subTest(scheduled_time=scheduled_time):
                ctx = MagicMock(spec=ipy.SlashContext)
                ctx.author.id = 123
                ctx.author.has_any_role.return_value = True
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                ctx.channel.id = 999
                ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[])
                mock_msg = MagicMock(
                    pin=AsyncMock(), edit=AsyncMock(),
                    embeds=[MagicMock(fields=[MagicMock() for _ in range(4)] + [MagicMock(value="")])],
                )
                mock_msg.author.id = BOT_ID
                mock_msg.components = [ipy.ActionRow(ipy.Button(
                    style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN",
                ))]
                ctx.channel.history.return_value.__aiter__.return_value = [mock_msg]
                mock_validators.url.return_value = True
                mock_base_layout.find_one = AsyncMock(return_value=None)
                mock_match_doc = MagicMock(
                    token="TOKEN", thread_message_id=None,
                    participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
                    times=[scheduled_time, scheduled_time], is_war=False,
                    matched_time=3000000000, final_districts=["Barbarian Camp"],
                    save_changes=AsyncMock(),
                )
                mock_match.find_one = AsyncMock(return_value=mock_match_doc)

                await self.cog.match_base_submit.callback(
                    ctx,
                    base_link="https://link.clashofclans.com/?action=OpenLayout&id=TH15%3ACC%3A1%3AAAAAA",
                    base_ss=None,
                )

                mock_match_doc.save_changes.assert_awaited()
                self.assertIn("submitted", ctx.send.await_args.args[0])

    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_view_success(self, mock_match, mock_base_layout):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 999

        # Mock Match
        mock_match_doc = MagicMock()
        mock_match_doc.participants = [
            MagicMock(user_id="123", bases=[MagicMock(link="http://link", image="http://image")]),
            MagicMock(user_id="456")
        ]
        mock_match_doc.final_districts = ["Barbarian Camp"]
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        # Mock BaseLayout
        mock_base_layout.find.return_value.to_list = AsyncMock(return_value=[])

        # Execute
        await self.cog.match_base_view.callback(ctx)

        # Verify
        mock_base_layout.find.assert_called()
        ctx.send.assert_called()
        self.assertIn("Here are", ctx.send.call_args[0][0])
        self.assertIsInstance(ctx.send.call_args[1]['embeds'][0], ipy.Embed)

    @patch('elo_bot.extensions.match.bases.download_file', new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_set_image_checks_finished_opponent(self, mock_match, mock_download):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = True
        match.hosts = ["999", None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.participants = [
            MagicMock(user_id="111", result=[None, None, None], bases=[MagicMock(), None, None]),
            MagicMock(user_id="222", result=["3.100", "2.90", "2.80"], bases=[None, None, None]),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_base_set_image.callback(
            ctx,
            player=0,
            district="Barbarian Camp",
            base_ss=MagicMock(spec=ipy.Attachment),
        )

        mock_download.assert_not_awaited()
        match.save_changes.assert_not_awaited()
        self.assertIn("finished", ctx.send.call_args[0][0].lower())

    @patch.object(OverrideCode, "is_valid", new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_base_remove_all_checks_effective_player_opponent(
            self, mock_match, mock_base_layout, mock_is_valid):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 111
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        own_base = MagicMock(link="https://example.com/base")
        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = True
        match.hosts = [None, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.participants = [
            MagicMock(user_id="111", result=[None, None, None], bases=[own_base, None, None]),
            MagicMock(user_id="222", result=["3.100", "2.90", "2.80"], bases=[None, None, None]),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        mock_base_layout.find_one = AsyncMock(return_value=None)
        mock_is_valid.return_value = False

        await self.cog.match_base_remove_all.callback(ctx, player=1)

        self.assertIs(match.participants[0].bases[0], own_base)
        match.save_changes.assert_not_awaited()
        self.assertIn("finished", ctx.send.call_args[0][0].lower())

from unittest.mock import MagicMock, AsyncMock, patch, call
import interactions as ipy
from beanie import PydanticObjectId
from elobot.db import Match, Player
from tests._patch_sites import patch_sites
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    @patch('elobot.extensions.match.results.record_match')
    @patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))
    async def test_match_submit_success(self, mock_player, mock_record_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        player1 = MagicMock(spec=ipy.User)
        player1.id = 111
        player2 = MagicMock(spec=ipy.User)
        player2.id = 222
        
        # Mock players found
        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(elo=1300, capital_peak_enabled=False),
            MagicMock(elo=1300, capital_peak_enabled=False),
        ])

        # Execute
        await self.cog.match_submit.callback(
            ctx, 
            player1=player1, 
            player2=player2, 
            player1_result="3.100\n3.100\n3.100", 
            player2_result="2.90\n2.80\n1.50",
            district1="Barbarian Camp",
            district2="Balloon Lagoon",
            district3="Dragon Cliffs"
        )

        # Verify
        mock_record_match.assert_called_once()
        ctx.send.assert_called()
        self.assertIn("is submitted", ctx.send.call_args[0][0])

    @patch('elobot.extensions.match.results.record_match')
    @patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))
    async def test_match_submit_rejects_unavailable_conditional_districts(
            self, mock_player, mock_record_match):
        # Given: Wizard is at its Elo limit or either player has opted out of Peak
        cases = [
            (
                [MagicMock(elo=1200, capital_peak_enabled=True),
                 MagicMock(elo=1199, capital_peak_enabled=True)],
                ("Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"),
            ),
            (
                [MagicMock(elo=1200, capital_peak_enabled=True),
                 MagicMock(elo=1200, capital_peak_enabled=False)],
                ("Barbarian Camp", "Balloon Lagoon", "Capital Peak"),
            ),
        ]
        users = [MagicMock(id=111), MagicMock(id=222)]

        for player_docs, districts in cases:
            with self.subTest(districts=districts):
                ctx = MagicMock(spec=ipy.SlashContext, guild=MagicMock())
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                mock_player.find_one = AsyncMock(side_effect=player_docs)
                mock_record_match.reset_mock()

                # When: a forged manual submission requests the unavailable district
                await self.cog.match_submit.callback(
                    ctx,
                    player1=users[0],
                    player2=users[1],
                    player1_result="3.100\n3.100\n3.100",
                    player2_result="2.90\n2.80\n1.50",
                    district1=districts[0],
                    district2=districts[1],
                    district3=districts[2],
                )

                # Then: the forged result never reaches recording
                mock_record_match.assert_not_awaited()
                self.assertIn("not available", ctx.send.await_args.args[0].lower())

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_match_resubmit_success(self, mock_match):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 123
        ctx.channel.name = "🔴 Match Channel"
        ctx.send_modal = AsyncMock()
        ctx.send = AsyncMock()
        
        # Mock match found
        mock_match_doc = MagicMock()
        mock_match_doc.is_autowin = False
        # Configure __contains__ to return False (author not in match)
        mock_match_doc.__contains__.return_value = False
        
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)

        # Execute
        await self.cog.match_resubmit.callback(ctx)

        # Verify
        ctx.send_modal.assert_called_once()
        modal = ctx.send_modal.call_args[0][0]
        self.assertIsInstance(modal, ipy.Modal)
        self.assertEqual(modal.title, "Result Submission")

    async def test_match_autowin_success(self):
        from elobot.db import Helper
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0].update(is_war=False, hosts=["900", None], times=[1000, None])
        ctx = fixture.context()

        await fixture.invoke(ctx)

        self.assertTrue(fixture.rows[Match][0]["is_autowin"])
        self.assertTrue(fixture.rows[Match][0]["autowin_completed"])
        self.assertEqual(fixture.elos(), [1005, 965])
        self.assertEqual(fixture.rows[Helper][0]["points"], 3)
        self.assertIn("An auto-win has been given", ctx.send.call_args[0][0])
        action_rows = ctx.channel.send.await_args.kwargs["components"]
        self.assertEqual(
            [(button.label, button.custom_id) for button in action_rows[0].components],
            [("Delete Thread", "delete_thread|AUTOWIN")],
        )

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_match_result_store_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.id = PydanticObjectId("000000000000000000000001")
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.is_autowin = False
        match.is_war = True
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.times = [100, 200]
        match.hosts = [None, None]
        match.host_clans = ["#FIRST", "#SECOND"]
        match.old_msgs = [123, 456]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        match.set = AsyncMock()
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_result_store.callback(
            ctx, player=0, district="Barbarian Camp", stars=2, destruction=88
        )

        collection.update_one.assert_awaited_once_with(
            {"_id": match.id, "is_war": True, "participants.0.user_id": "111", "times.0": 100,
             "hosts.0": None, "host_clans.0": "#FIRST", "old_msgs.0": 123, "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             "participants.0.result.0": "3.100"},
            {"$set": {"participants.0.result.0": "2.88"}},
        )
        self.assertIn("recorded", ctx.send.call_args[0][0].lower())

        ctx.send.reset_mock()
        collection.update_one.reset_mock()
        await self.cog.match_result_store.callback(
            ctx, player=1, district="Barbarian Camp", stars=2, destruction=90
        )

        collection.update_one.assert_awaited_once_with(
            {"_id": match.id, "is_war": True, "participants.1.user_id": "222", "times.1": 200,
             "hosts.1": None, "host_clans.1": "#SECOND", "old_msgs.1": 456, "result_operation": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             "participants.1.result.0": None},
            {"$set": {"participants.1.result.0": "2.90"}},
        )
        match.set.assert_not_awaited()
        self.assertIn("recorded", ctx.send.call_args[0][0].lower())

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_result_store_button_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_result_store|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send_modal = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.old_msgs = [123, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.hosts = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.result_store_button.callback(ctx)

        ctx.send_modal.assert_awaited_once()
        self.assertEqual("store_modal|token|0", ctx.send_modal.call_args.args[0].custom_id)

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_result_store_button_modal_title_stays_within_discord_limits(self, mock_match):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_result_store|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send_modal = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.old_msgs = [123, None]
        match.final_districts = ["Barbarian Camp", "Skeleton Park", "Dragon Cliffs"]
        match.hosts = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", None, None]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        mock_user = MagicMock(spec=ipy.User)
        mock_user.__str__ = MagicMock(return_value="VeryLongDisplayNameExceedingLimits12345#1234")
        self.cog.bot.fetch_user = AsyncMock(return_value=mock_user)

        await self.cog.result_store_button.callback(ctx)

        ctx.send_modal.assert_awaited_once()
        modal = ctx.send_modal.call_args.args[0]
        self.assertLessEqual(len(modal.title), 45)

    async def test_match_autowin_allows_finished_war_player_correction(self):
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0]["participants"][0]["result"] = ["3.100", "2.90", "2.80"]
        fixture.rows[Player][0]["elo"] = 1200
        fixture.rows[Player][1].update(elo=1200, highest_elo=1250)
        ctx = fixture.context()

        await fixture.invoke(ctx, winner="222")

        self.assertTrue(fixture.rows[Match][0]["is_autowin"])
        self.assertTrue(fixture.rows[Match][0]["autowin_completed"])
        self.assertEqual(fixture.elos(), [1165, 1205])
        self.assertEqual(fixture.rows[Player][1]["highest_elo"], 1250)
        self.assertIn("auto-win has been given", ctx.send.await_args.args[0].lower())
        fixture.cleanup.assert_has_awaits([call("321", "111"), call("321", "222")])
        fixture.schedule.assert_called_once_with(ctx.channel, "🔴")
        fixture.autoqueue.assert_awaited_once_with(fixture.bot, fixture.match.token, ("111", "222"))
        ctx.channel.edit.assert_not_called()

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_result_store_modal_allows_finished_war_player_correction(self, mock_match):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "store_modal|token|0"
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Ended**")]

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.autowin_winner_id = None
        match.autowin_completed = False
        match.id = "MATCH"
        match.is_autowin = False
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = [None, None]
        match.host_clans = [None, None]
        match.submitters = [None, None]
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=[None, None, None]),
        ]
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.result_store_modal.callback.func(
            self.cog,
            ctx,
            result1="2.88",
            result2="2.90",
            result3="2.80",
        )

        collection.update_one.assert_awaited_once()
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 123)
        self.assertEqual(collection.update_one.await_args.args[1], {"$set": {
            "participants.0.result.0": "2.88",
            "participants.0.result.1": "2.90",
            "participants.0.result.2": "2.80",
            "submitters.0": "999",
        }})
        self.assertIn("stored", ctx.send.call_args[0][0].lower())

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_process_match_results_rechecks_finished_channel(self, mock_match):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "result_modal|token"
        ctx.channel.name = "🔴 Finished match"
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.result_operation = None
        match.thread = "123"
        match.participants = [
            MagicMock(user_id="111"),
            MagicMock(user_id="222"),
        ]
        match.save_changes = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        self.bot.fetch_channel = AsyncMock(return_value=ctx.channel)

        await self.cog._process_match_results(
            ctx,
            {
                "player1": "3.100\n2.90\n2.80",
                "player2": "2.90\n2.80\n1.70",
            },
        )

        match.save_changes.assert_not_awaited()
        self.assertIn("already ended", ctx.send.call_args[0][0].lower())

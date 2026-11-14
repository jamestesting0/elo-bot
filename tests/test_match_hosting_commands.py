import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock, ANY, call
from aiohttp import web
import anyio
import interactions as ipy
from beanie import PydanticObjectId
from elo_bot.db import Match, Participant
from tests._patch_sites import patch_sites
import elo_bot.features.matches.resources as resources_module
import elo_bot.extensions.match.base_store as match_base_store_module
import elo_bot.extensions.match.hosting as match_hosting_module
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    @patch(
        'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
        new_callable=AsyncMock,
        return_value=([], {"Slot 1": ("200", "2"), "Slot 2": ("300", "3")}),
    )
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new_callable=AsyncMock, return_value=True)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_war_start_blocks_when_both_foreign_slots_are_occupied(
            self, mock_match, _mock_is_clan_available, _mock_is_raid_weekend, mock_start_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 12345
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )

        match = MagicMock(
            thread="12345",
            is_war=True,
            times=[None, None],
            participants=[MagicMock(user_id="123", base_store=None, opponent_bases_stored=False),
                          MagicMock(user_id="456", base_store=None, opponent_bases_stored=False)],
        )
        mock_match.find_one = AsyncMock(return_value=match)
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock) as mock_send_bases,
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_war_start.callback(ctx, player=0, clan="#CLAN", scout=False)

        mock_send_bases.assert_not_awaited()
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        response = ctx.send.call_args[0][0]
        self.assertIn("both base slots", response.lower())
        self.assertIn("<#200> <@2>", response)
        self.assertIn("<#300> <@3>", response)

    async def test_start_war_uses_the_callers_preheld_reservations(self) -> None:
        from elo_bot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war

        match = MagicMock(
            token="TOKEN",
            thread="101",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            old_msgs=[None, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        match.set = AsyncMock()
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        clan_reservation = MagicMock(clan_tag="#AVAILABLE")
        side_reservation = MagicMock(key=("TOKEN", 0))
        message = MagicMock(id=777)
        start = WarStart(
            match=match,
            player=0,
            host=MagicMock(id=100),
            clan=MagicMock(tag="#AVAILABLE"),
            coc_client=MagicMock(),
            reply=SelfHostedWarStartReply(),
            clan_reservation=clan_reservation,
            side_reservation=side_reservation,
        )

        with (
                patch(
                    'elo_bot.features.matches.resources.current_resource_clan_reservation',
                    return_value=None,
                ),
                patch_sites(
                    ('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                    new_callable=AsyncMock,
                ) as reserve_clan,
                patch(
                    'elo_bot.features.matches.resources.current_resource_match_side_reservation',
                    return_value=None,
                ),
                patch_sites(
                    ('elo_bot.extensions.match.base_store.try_reserve_resource_match_side', 'elo_bot.extensions.match.hosting.try_reserve_resource_match_side', 'elo_bot.features.matches.resources.try_reserve_resource_match_side'),
                    new_callable=AsyncMock,
                ) as reserve_side,
                patch_sites(
                    ('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'),
                    new=AsyncMock(return_value=match),
                ),
                patch_sites(
                    ('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'),
                    new=AsyncMock(return_value=MagicMock(availability_str="Available")),
                ),
                patch(
                    'elo_bot.features.matches.resources.occupy_clan',
                    new_callable=AsyncMock,
                ) as occupy,
                patch_sites(
                    ('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'),
                    new_callable=AsyncMock,
                ),
                patch(
                    "elo_bot.features.hosting.war.war_start.complete_war_start",
                    new=AsyncMock(return_value=message),
                ),
        ):
            result = await start_war(MagicMock(), MagicMock(spec=ipy.SlashContext), start)

        self.assertIs(result, message)
        reserve_clan.assert_not_awaited()
        reserve_side.assert_not_awaited()
        clan_reservation.release.assert_called_once_with()
        side_reservation.release.assert_called_once_with()
        occupy.assert_awaited_once_with(match, 0)
        update = match.get_pymongo_collection.return_value.update_one.await_args.args[1]["$set"]
        self.assertEqual(update["hosts.0"], "100")
        self.assertEqual(update["host_clans.0"], "#AVAILABLE")
        self.assertEqual(update["persistent_host_clans.0"], "#AVAILABLE")
        self.assertEqual(match.persistent_host_clans[0], "#AVAILABLE")

    async def test_claim_resource_clan_initializes_persistent_array_for_legacy_match(self) -> None:
        legacy_match = Match.model_construct(
            token="TOKEN",
            matched_time=1,
            thread="101",
            host_clans=["#LEGACY", None],
            hosts=[None, None],
            participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
        )
        legacy_match.id = PydanticObjectId()
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        reservation = MagicMock(clan_tag="#AVAILABLE")
        side_reservation = MagicMock(key=("TOKEN", 1))

        with patch.object(Match, "find_one", new=AsyncMock(return_value=legacy_match)), \
                patch.object(Match, "get_pymongo_collection", return_value=collection), \
                patch.object(Match, "set", new_callable=AsyncMock), \
                patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"))), \
                patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock):
            claimed = await match_hosting_module.claim_resource_clan(
                reservation,
                legacy_match,
                1,
                side_reservation=side_reservation,
            )

        self.assertTrue(claimed)
        collection.update_one.assert_awaited_once_with(
            {"_id": legacy_match.id, "persistent_host_clans": {"$exists": False}},
            {"$set": {"persistent_host_clans": ["#LEGACY", None]}},
        )
        self.assertIn("persistent_host_clans", legacy_match.model_fields_set)
        self.assertEqual(legacy_match.persistent_host_clans, ["#LEGACY", "#AVAILABLE"])

    async def test_abort_started_war_side_preserves_persistent_host_clan(self) -> None:
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        match = Match.model_construct(
            token="TOKEN",
            matched_time=1,
            is_war=True,
            times=[1000, None],
            hosts=["100", None],
            host_clans=["#CURRENT", None],
            old_msgs=[777, None],
            participants=[],
        )
        match.id = PydanticObjectId()
        settings = MagicMock(use_state_management=True, state_management_save_previous=False, keep_nulls=True)

        with patch.object(Match, "get_pymongo_collection", return_value=collection), \
                patch.object(Match, "get_settings", return_value=settings):
            aborted = await match.abort_started_war_side(0, 1000, "100", "#CURRENT", 777)

        self.assertTrue(aborted)
        initialize, abort = collection.update_one.await_args_list
        self.assertEqual(
            initialize.args[1],
            {"$set": {"persistent_host_clans": ["#CURRENT", None]}},
        )
        update = abort.args[1]["$set"]
        self.assertNotIn("persistent_host_clans.0", update)
        self.assertEqual(match.persistent_host_clans, ["#CURRENT", None])

    async def test_abort_started_live_match_requires_exact_started_state(self) -> None:
        from elo_bot.features.hosting.live import live_start
        from elo_bot.db.match_models import ExpectedLiveStartState

        expected = ExpectedLiveStartState(
            times=(1000, None),
            hosts=("100", None),
            host_clans=("#CURRENT", None),
            old_messages=(777, None),
        )
        settings = MagicMock(use_state_management=True, state_management_save_previous=False, keep_nulls=True)

        for field, actual in (
                (None, None),
                ("times", (999, None)),
                ("hosts", ("999", None)),
                ("host_clans", ("#OTHER", None)),
                ("old_msgs", (999, None)),
                ("is_war", True),
        ):
            with self.subTest(field=field):
                match = Match.model_construct(
                    token="TOKEN",
                    matched_time=1,
                    is_war=False,
                    times=[1000, None],
                    hosts=["100", None],
                    host_clans=["#CURRENT", None],
                    persistent_host_clans=["#PREFERRED", None],
                    old_msgs=[777, None],
                    participants=[],
                )
                match.id = PydanticObjectId()
                if field is None:
                    match.model_fields_set.discard("persistent_host_clans")
                stored = {
                    "_id": match.id,
                    "is_war": False,
                    "times": [1000, None],
                    "hosts": ["100", None],
                    "host_clans": ["#CURRENT", None],
                    "old_msgs": [777, None],
                }
                if field:
                    stored[field] = list(actual) if isinstance(actual, tuple) else actual
                collection = MagicMock()

                async def update_one(query, _update):
                    matched = all(stored.get(key) == value for key, value in query.items())
                    return MagicMock(matched_count=int(matched))

                collection.update_one = AsyncMock(side_effect=update_one)
                before = (list(match.times), list(match.hosts), list(match.host_clans), list(match.old_msgs))
                with patch.object(Match, "get_pymongo_collection", return_value=collection), \
                        patch.object(Match, "get_settings", return_value=settings):
                    aborted = await match.abort_started_live_match(expected)

                self.assertEqual(aborted, field is None)
                query, update = collection.update_one.await_args.args
                self.assertEqual(query, {
                    "_id": match.id,
                    "is_war": False,
                    "times": [1000, None],
                    "hosts": ["100", None],
                    "host_clans": ["#CURRENT", None],
                    "old_msgs": [777, None],
                })
                self.assertEqual(update, {"$set": {
                    "times": [None, None], "hosts": [None, None],
                    "host_clans": [None, None],
                }})
                self.assertEqual(match.old_msgs, [777, None])
                if field is None:
                    self.assertEqual(match.persistent_host_clans, ["#CURRENT", None])
                else:
                    self.assertEqual(
                        (match.times, match.hosts, match.host_clans, match.old_msgs),
                        before,
                    )

        stale_match = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#CURRENT", None],
            old_msgs=[777, None], thread="12345", msg=None,
        )
        stale_match.abort_started_live_match = AsyncMock(return_value=False)
        message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'),
                      new_callable=AsyncMock) as clear_bases,
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'),
                      new_callable=AsyncMock) as free_clan,
                patch("elo_bot.features.hosting.live.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertFalse(await live_start.abort_live_start(MagicMock(), live_start.LiveAbort(stale_match, message)))
        clear_bases.assert_not_awaited()
        message.delete.assert_not_awaited()
        message.edit.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_not_called()

        missing_message_id = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#CURRENT", None],
            old_msgs=[None, None], thread="12345", msg=None,
        )
        missing_message_id.abort_started_live_match = AsyncMock(return_value=True)
        supplied_message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'),
                      new_callable=AsyncMock) as clear_bases,
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'),
                      new_callable=AsyncMock) as free_clan,
                patch("elo_bot.features.hosting.live.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertFalse(await live_start.abort_live_start(
                MagicMock(), live_start.LiveAbort(missing_message_id, supplied_message),
            ))
        missing_message_id.abort_started_live_match.assert_not_awaited()
        clear_bases.assert_not_awaited()
        supplied_message.delete.assert_not_awaited()
        supplied_message.edit.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_not_called()

        unassigned = MagicMock(
            times=[1000, None], hosts=[None, None], host_clans=[None, None],
            old_msgs=[None, None], thread="12345", msg=None,
        )
        unassigned.abort_started_live_match = AsyncMock(return_value=True)
        with (
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'),
                      new_callable=AsyncMock) as clear_bases,
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'),
                      new_callable=AsyncMock) as free_clan,
                patch("elo_bot.features.hosting.live.live_start.schedule_channel_name_edit") as rename,
        ):
            self.assertTrue(await live_start.abort_live_start(
                MagicMock(bot=self.bot), live_start.LiveAbort(unassigned, None),
            ))
        clear_bases.assert_not_awaited()
        free_clan.assert_not_awaited()
        rename.assert_called_once()

        two_clans = MagicMock(
            times=[1000, None], hosts=["100", None], host_clans=["#ONE", "#TWO"],
            old_msgs=[777, None], thread="12345", msg=None,
        )
        two_clans.abort_started_live_match = AsyncMock(return_value=True)
        message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with (
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'),
                      new_callable=AsyncMock),
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'),
                      new_callable=AsyncMock) as free_clan,
                patch("elo_bot.features.hosting.live.live_start.schedule_channel_name_edit"),
        ):
            self.assertTrue(await live_start.abort_live_start(MagicMock(), live_start.LiveAbort(two_clans, message)))
        self.assertCountEqual(
            free_clan.await_args_list,
            [
                call("#ONE", expected_thread="12345"),
                call("#TWO", expected_thread="12345"),
            ],
        )

    async def test_match_abort_preserves_persistent_host_clans(self) -> None:
        from elo_bot.db.match_models import ExpectedLiveStartState

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        old_message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        ctx.channel.fetch_message = AsyncMock(return_value=old_message)
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            thread="12345",
            times=[1000, None],
            hosts=["999", None],
            host_clans=["#CURRENT", None],
            persistent_host_clans=["#PREFERRED", None],
            old_msgs=[777, None],
            msg=None,
            participants=[MagicMock(result=[None] * 3, base_store=None, opponent_bases_stored=False),
                          MagicMock(result=[None] * 3, base_store=None, opponent_bases_stored=False)],
            model_fields_set={"persistent_host_clans"},
        )
        match.set = AsyncMock()
        match.abort_started_live_match = AsyncMock(return_value=True)

        with patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model, \
                patch_sites(('elo_bot.features.hosting.live.helper_live.is_match_finished', 'elo_bot.features.hosting.live.live_start.is_match_finished', 'elo_bot.features.hosting.war.helper_host.is_match_finished', 'elo_bot.extensions.match.bases.is_match_finished', 'elo_bot.extensions.match.base_store.is_match_finished', 'elo_bot.extensions.match.hosting.is_match_finished', 'elo_bot.extensions.match.settings.is_match_finished', 'elo_bot.features.matches.occupancy.is_match_finished'), return_value=False), \
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock), \
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock), \
                patch("elo_bot.features.hosting.live.live_start.schedule_channel_name_edit"):
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_abort.callback.func(self.cog, ctx)

        match.abort_started_live_match.assert_awaited_once_with(ExpectedLiveStartState(
            times=(1000, None),
            hosts=("999", None),
            host_clans=("#CURRENT", None),
            old_messages=(777, None),
        ))
        match.set.assert_not_awaited()
        self.assertEqual(match.persistent_host_clans, ["#PREFERRED", None])
        old_message.delete.assert_not_awaited()
        self.assertEqual(old_message.edit.await_args.kwargs["embeds"][0].title, "**Match Aborted**")
        self.assertEqual(old_message.edit.await_args.kwargs["components"], [])

    async def test_match_war_autostart_passes_preheld_reservations_to_helper(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel = MagicMock(
            id=12345,
            name="Match Thread",
            created_at=datetime.now(timezone.utc),
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            participants=[
                MagicMock(user_id="100", result=[None] * 3, base_store=None, opponent_bases_stored=False),
                MagicMock(user_id="200", result=[None] * 3, base_store=None, opponent_bases_stored=False),
            ],
        )
        clan = MagicMock(tag="#CLAN", name="Helper Clan")
        clan_reservation = MagicMock(clan_tag="#CLAN")
        side_reservation = MagicMock(key=("TOKEN", 1))
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)

        with (
                patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model,
                patch_sites(('elo_bot.features.hosting.live.helper_live.is_match_finished', 'elo_bot.features.hosting.live.live_start.is_match_finished', 'elo_bot.features.hosting.war.helper_host.is_match_finished', 'elo_bot.extensions.match.bases.is_match_finished', 'elo_bot.extensions.match.base_store.is_match_finished', 'elo_bot.extensions.match.hosting.is_match_finished', 'elo_bot.extensions.match.settings.is_match_finished', 'elo_bot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False),
                patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)),
                patch_sites(
                    ('elo_bot.extensions.match.base_store.confirm_pending_opponent_bases', 'elo_bot.extensions.match.hosting.confirm_pending_opponent_bases'),
                    new=AsyncMock(return_value=True),
                ),
                patch_sites(
                    ('elo_bot.extensions.match.base_store.try_reserve_resource_match_side', 'elo_bot.extensions.match.hosting.try_reserve_resource_match_side', 'elo_bot.features.matches.resources.try_reserve_resource_match_side'),
                    new=AsyncMock(return_value=side_reservation),
                ),
                patch_sites(
                    ('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                    new=AsyncMock(return_value=clan_reservation),
                ),
                patch(
                    'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
                    new_callable=AsyncMock,
                ) as available_slots,
                patch(
                    'elo_bot.extensions.match.hosting.helper_host.host_war_automatically',
                    new_callable=AsyncMock,
                ) as automatic_host,
        ):
            match_model.find_one = AsyncMock(return_value=match)

            await self.cog.match_war_autostart.callback(
                ctx,
                player=1,
                clan="#CLAN",
                scout=True,
            )

        available_slots.assert_not_awaited()
        automatic_host.assert_awaited_once()
        runtime, host_ctx, request = automatic_host.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(host_ctx, ctx)
        self.assertIs(request.match, match)
        self.assertEqual(request.player, 1)
        self.assertIs(request.clan, clan)
        self.assertIs(request.host, ctx.author)
        self.assertTrue(request.scout)
        self.assertIs(request.clan_reservation, clan_reservation)
        self.assertIs(request.side_reservation, side_reservation)
        clan_reservation.release.assert_called_once_with()
        side_reservation.release.assert_called_once_with()

    def test_match_autostart_metadata_and_options(self) -> None:
        command = self.cog.match_autostart

        self.assertEqual(command.resolved_name, "match autostart")
        self.assertEqual([str(option.name) for option in command.options], ["clan", "scout", "district"])
        self.assertTrue(command.options[0].required)
        self.assertFalse(command.options[1].required)
        self.assertFalse(command.options[2].required)
        self.assertEqual([str(option.name) for option in self.cog.match_start.options], ["clan", "scout"])
        self.assertEqual(len(command.checks), 1)
        self.assertIs(command.max_concurrency.bucket, match_base_store_module.CHANNEL_USER_CONCURRENCY_BUCKET)
        self.assertEqual(command.max_concurrency.concurrent, 1)

    async def test_match_autostart_passes_both_reservations_and_human_requester(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel = MagicMock(id=12345)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            old_msgs=[None, None],
            final_districts=["D1"],
            participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
        )
        match.participants[0].bases = [MagicMock(link="https://p1-d1")]
        match.participants[1].bases = [MagicMock(link="https://p2-d1")]
        clan = MagicMock(tag="#CLAN", name="Helper Clan")
        p1_doc = MagicMock(tag="#P1", elo=1200)
        p2_doc = MagicMock(tag="#P2", elo=1100)
        player1 = MagicMock(tag="#P1", town_hall=3)
        player2 = MagicMock(tag="#P2", town_hall=3)
        side0 = MagicMock()
        side1 = MagicMock()
        clan_reservation = MagicMock()
        started_message = MagicMock(id=777)
        events: list[str] = []

        async def confirm_eta(*_args, **_kwargs) -> bool:
            events.append("eta")
            return True

        async def reserve_match_side(_token: str, side: int):
            events.append(f"reserve-{side}")
            return (side0, side1)[side]

        async def reserve_clan(_clan_tag: str):
            events.append("reserve-clan")
            return clan_reservation

        async def choose_side(*_args, **_kwargs):
            events.append("choose")
            return match

        async def claim_clan(*_args, **_kwargs) -> bool:
            events.append("local-start")
            return True

        async def run_job(*_args, **_kwargs) -> None:
            events.append("runner")

        with (
                patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model,
                patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player')) as player_model,
                patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False),
                patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)),
                patch('elo_bot.extensions.match.hosting.is_resource_clan_locked', return_value=False),
                patch('elo_bot.extensions.match.hosting.get_start_base_store_slot_availability', new_callable=AsyncMock) as slots,
                patch("elo_bot.features.hosting.live.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch(
                    "elo_bot.features.hosting.live.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ) as live_slots,
                patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'),
                      new=AsyncMock(side_effect=choose_side)),
                patch("elo_bot.features.hosting.live.helper_live.confirm_helper_availability",
                      new=AsyncMock(side_effect=confirm_eta)),
                patch("elo_bot.features.hosting.live.helper_live.run_helper_host_job",
                      new=AsyncMock(side_effect=run_job)) as runner,
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_match_side', 'elo_bot.extensions.match.hosting.try_reserve_resource_match_side', 'elo_bot.features.matches.resources.try_reserve_resource_match_side'),
                      new=AsyncMock(side_effect=reserve_match_side)) as reserve_side,
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                      new=AsyncMock(side_effect=reserve_clan)),
                patch_sites(('elo_bot.extensions.match.hosting.claim_resource_clan', 'elo_bot.extensions.match.settings.claim_resource_clan', 'elo_bot.features.matches.resources.claim_resource_clan'),
                      new=AsyncMock(side_effect=claim_clan)),
                patch("elo_bot.features.hosting.live.live_start.complete_live_start",
                      new=AsyncMock(return_value=started_message)),
        ):
            match_model.find_one = AsyncMock(side_effect=[match, match, match])
            player_model.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])
            self.cog.coc_client.get_player = AsyncMock(side_effect=[player1, player2])
            self.cog.coc_client.get_clan = AsyncMock(return_value=clan)

            await self.cog.match_autostart.callback(ctx, clan="#CLAN", scout=True)

        slots.assert_not_awaited()
        live_slots.assert_awaited_once_with("#CLAN", bot=self.bot)
        self.assertEqual(
            reserve_side.await_args_list,
            [call("TOKEN", 0), call("TOKEN", 1)],
        )
        side0.release.assert_called_once_with()
        side1.release.assert_called_once_with()
        self.assertEqual(events, [
            "reserve-0", "reserve-1", "reserve-clan", "choose", "choose",
            "eta", "local-start", "runner",
        ])
        runner.assert_awaited_once()
        runtime, host_ctx, request = runner.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(host_ctx, ctx)
        self.assertEqual(request.requester_id, "999")
        self.assertEqual(request.create_request.job_type, "host_live_match")
        self.assertEqual(request.create_request.requested_by, "999")
        self.assertIs(request.started_message, started_message)
        self.assertIsNone(request.on_success)
        self.assertFalse(request.additional_cancel_user_ids)

    async def test_concurrent_automatic_live_loser_has_zero_side_effects(self) -> None:
        from elo_bot.features.hosting.live import live_start
        from elo_bot.features.hosting.live import live_reservations
        from elo_bot.features.hosting.live import helper_live

        resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
        resources_module.RESOURCE_CLAN_LOCKS.clear()
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            old_msgs=[None, None],
            final_districts=["D1"],
            participants=[
                MagicMock(user_id="100", result=[None], bases=[MagicMock(link="https://p1-d1")]),
                MagicMock(user_id="200", result=[None], bases=[MagicMock(link="https://p2-d1")]),
            ],
        )
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=lambda user_id: MagicMock(id=int(user_id)))
        runtime = MagicMock(bot=bot)
        winner_ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        loser_ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN", name="Helper Clan"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )
        choose_entered = anyio.Event()
        release_choose = anyio.Event()
        choose_calls = 0

        async def choose_side(*_args, **_kwargs):
            nonlocal choose_calls
            choose_calls += 1
            if choose_calls == 1:
                choose_entered.set()
                with anyio.fail_after(5):
                    await release_choose.wait()
            return match

        eta = AsyncMock(return_value=True)
        start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock(id=777)))
        runner = AsyncMock()
        abort = AsyncMock()
        with (
                patch("elo_bot.features.hosting.live.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch(
                    "elo_bot.features.hosting.live.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ),
                patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'),
                      new=AsyncMock(side_effect=choose_side)) as choose,
                patch_sites(('elo_bot.extensions.match.hosting.available_resource_clans_text', 'elo_bot.extensions.match.settings.available_resource_clans_text', 'elo_bot.features.matches.resources.available_resource_clans_text'),
                      new=AsyncMock(return_value="available clans")),
                patch("elo_bot.features.hosting.live.helper_live.confirm_helper_availability", new=eta),
                patch("elo_bot.features.hosting.live.helper_live.live_start.start_live_match", new=start),
                patch("elo_bot.features.hosting.live.helper_live.run_helper_host_job", new=runner),
                patch("elo_bot.features.hosting.live.helper_live.live_start.abort_live_start", new=abort),
        ):
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(helper_live.host_live_automatically(runtime, winner_ctx, request))
                    await choose_entered.wait()
                    await helper_live.host_live_automatically(runtime, loser_ctx, request)
                    loser_choose_count = choose.await_count
                    release_choose.set()

        self.assertEqual(loser_choose_count, 1)
        self.assertEqual(choose.await_count, 2)
        eta.assert_awaited_once()
        start.assert_awaited_once()
        self.assertIsInstance(start.await_args.args[2].reservations, live_reservations.LiveStartReservations)
        runner.assert_awaited_once()
        abort.assert_not_awaited()
        self.assertEqual(bot.fetch_user.await_count, 2)
        loser_ctx.send.assert_awaited_once()
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 0)].locked())
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 1)].locked())
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#CLAN"].locked())

    async def test_match_autostart_rejects_when_either_side_started_before_helper(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=999)
        ctx.channel = MagicMock(id=12345)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        for field, index in (("times", 0), ("times", 1), ("hosts", 0), ("hosts", 1)):
            with self.subTest(field=field, index=index):
                times = [None, None]
                hosts = [None, None]
                (times if field == "times" else hosts)[index] = 123 if field == "times" else "888"
                match = MagicMock(
                    token="TOKEN", thread="12345", is_war=False, times=times, hosts=hosts,
                    participants=[MagicMock(user_id="100"), MagicMock(user_id="200")],
                )
                with (
                        patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model,
                        patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player')) as player_model,
                        patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'), new_callable=AsyncMock) as choose,
                        patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_match_side', 'elo_bot.extensions.match.hosting.try_reserve_resource_match_side', 'elo_bot.features.matches.resources.try_reserve_resource_match_side'), new_callable=AsyncMock) as reserve,
                        patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                              new_callable=AsyncMock) as reserve_clan,
                        patch('elo_bot.extensions.match.hosting.live_start.start_live_match', new_callable=AsyncMock) as start,
                        patch('elo_bot.extensions.match.hosting.helper_live.confirm_helper_availability',
                              new_callable=AsyncMock) as eta,
                        patch('elo_bot.extensions.match.hosting.helper_live.run_helper_host_job', new_callable=AsyncMock) as post_poll,
                        patch('elo_bot.extensions.match.hosting.live_start.abort_live_start', new_callable=AsyncMock) as abort,
                        patch_sites(('elo_bot.extensions.match.hosting.available_resource_clans_text', 'elo_bot.extensions.match.settings.available_resource_clans_text', 'elo_bot.features.matches.resources.available_resource_clans_text'),
                              new=AsyncMock(return_value="available clans")),
                ):
                    match_model.find_one = AsyncMock(return_value=match)
                    await self.cog.match_autostart.callback(ctx, clan="#CLAN")

                player_model.find_one.assert_not_called()
                choose.assert_not_awaited()
                eta.assert_not_awaited()
                reserve.assert_not_awaited()
                reserve_clan.assert_not_awaited()
                start.assert_not_awaited()
                post_poll.assert_not_awaited()
                abort.assert_not_awaited()

    async def test_host_live_automatically_builds_paired_payload_and_polls_success(self) -> None:
        from elo_bot.features.hosting.live import live_start
        from elo_bot.features.hosting.live import helper_live

        job_id = "0123456789abcdef0123456789abcdef"
        events: list[str] = []
        request_payload = None
        request_api_key = None
        polls = iter(("running", "succeeded"))

        def job(status: str) -> dict[str, object]:
            return {
                "id": job_id, "type": "host_live_match", "status": status,
                "created_at": 100.0, "started_at": 101.0,
                "finished_at": 102.0 if status == "succeeded" else None,
                "events": [], "result": None, "error": None,
            }

        async def eta_handler(request: web.Request) -> web.Response:
            events.append("eta")
            self.assertEqual(request.query["type"], "host_live_match")
            self.assertEqual(request.headers["X-Api-Key"], "test-key")
            return web.json_response({"estimate_start_at": 0.0, "estimate_end_at": 180.0})

        async def post_handler(request: web.Request) -> web.Response:
            nonlocal request_payload, request_api_key
            events.append("post")
            request_payload = await request.json()
            request_api_key = request.headers.get("X-Api-Key")
            return web.json_response(job("queued"), status=201)

        async def poll_handler(request: web.Request) -> web.Response:
            status = next(polls)
            events.append(f"poll-{status}")
            self.assertEqual(request.headers["X-Api-Key"], "test-key")
            return web.json_response(job(status))

        app = web.Application()
        app.router.add_get("/v1/eta", eta_handler)
        app.router.add_post("/v1/jobs", post_handler)
        app.router.add_get(f"/v1/jobs/{job_id}", poll_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]

        async def wait_forever(*_args, **_kwargs) -> None:
            with anyio.fail_after(5):
                await asyncio.Event().wait()

        bot = MagicMock()
        bot.wait_for_component = AsyncMock(side_effect=wait_forever)
        player1_user = MagicMock(id=100)
        player2_user = MagicMock(id=200)
        bot.fetch_user = AsyncMock(side_effect=[player1_user, player2_user])
        runtime = MagicMock(
            bot=bot,
            helper_api_base_url=f"http://127.0.0.1:{port}",
            helper_api_key="test-key",
        )
        progress = MagicMock(id=888)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=555)
        ctx.send = AsyncMock(return_value=progress)
        ctx.edit = AsyncMock()
        match = MagicMock(
            token="TOKEN", thread="12345", final_districts=["D1", "D2"],
            participants=[
                MagicMock(user_id="100", bases=[MagicMock(link="https://p1-d1"), MagicMock(link="https://p1-d2")]),
                MagicMock(user_id="200", bases=[MagicMock(link="https://p2-d1"), MagicMock(link="https://p2-d2")]),
            ],
            times=[None, None], hosts=[None, None], host_clans=[None, None], old_msgs=[None, None],
        )
        request = helper_live.AutomaticLiveHost(
            match=match, clan=MagicMock(tag="#CLAN", name="Helper Clan"), host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"), player2=MagicMock(tag="#P2"),
            player1_elo=1200, player2_elo=1100, scout=False, requested_by="999",
        )

        async def choose_side(_token, _player, opponent, **_kwargs):
            events.append("choose-p2" if opponent is player2_user else "choose-p1")
            return match

        async def start_side(*_args, **_kwargs):
            events.append("live-start")
            match.times[0] = 100
            match.hosts[0] = "888"
            match.host_clans[0] = "#CLAN"
            match.old_msgs[0] = 777
            return live_start.LiveStartSucceeded(message=MagicMock(id=777))

        try:
            with (
                    patch(
                        "elo_bot.features.hosting.live.helper_live.live_eligibility.clan_has_all_base_store_slots",
                        new=AsyncMock(return_value=True),
                    ),
                    patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'),
                          new=AsyncMock(side_effect=choose_side)),
                    patch("elo_bot.features.hosting.live.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                    patch("elo_bot.features.hosting.live.helper_live.live_start.start_live_match",
                          new=AsyncMock(side_effect=start_side)),
                    patch("elo_bot.integrations.helper_api.anyio.sleep", new=AsyncMock()),
            ):
                await helper_live.host_live_automatically(runtime, ctx, request)
        finally:
            await runner.cleanup()

        self.assertEqual(events, [
            "choose-p2", "choose-p1", "eta", "live-start", "post", "poll-running", "poll-succeeded",
        ])
        self.assertEqual(request_api_key, "test-key")
        self.assertEqual(request_payload, {
            "type": "host_live_match",
            "requested_by": "999",
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_1_tag": "#P1",
                "player_2_tag": "#P2",
                "base_links_for_player_1": ["https://p2-d1", "https://p2-d2"],
                "base_links_for_player_2": ["https://p1-d1", "https://p1-d2"],
            },
        })

    async def test_host_live_automatically_rejects_invalid_pairs_before_eta(self) -> None:
        from elo_bot.features.hosting.live import helper_live

        resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
        resources_module.RESOURCE_CLAN_LOCKS.clear()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=[MagicMock(id=100), MagicMock(id=200)])
        runtime = MagicMock(bot=bot)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            final_districts=["D1", "D2"],
            participants=[
                MagicMock(user_id="100", bases=[MagicMock(link="duplicate"), MagicMock(link="duplicate")]),
                MagicMock(user_id="200", bases=[MagicMock(link="other-1"), MagicMock(link="other-2")]),
            ],
        )
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )

        with (
                patch(
                    "elo_bot.features.hosting.live.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ),
                patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'),
                      new=AsyncMock(return_value=match)),
                patch("elo_bot.features.hosting.live.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch("elo_bot.features.hosting.live.helper_live.confirm_helper_availability",
                      new_callable=AsyncMock) as eta,
                patch("elo_bot.features.hosting.live.helper_live.live_start.start_live_match",
                      new_callable=AsyncMock) as start,
                patch("elo_bot.features.hosting.live.helper_live.run_helper_host_job",
                      new_callable=AsyncMock) as post_poll,
        ):
            await helper_live.host_live_automatically(runtime, ctx, request)

        eta.assert_not_awaited()
        start.assert_not_awaited()
        post_poll.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 0)].locked())
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 1)].locked())
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#CLAN"].locked())

    async def test_host_live_automatically_rejects_clan_with_one_occupied_slot(self) -> None:
        from elo_bot.features.hosting.live import live_start
        from elo_bot.features.hosting.live import live_reservations
        from elo_bot.features.hosting.live import helper_live

        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=[MagicMock(id=100), MagicMock(id=200)])
        runtime = MagicMock(bot=bot)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()
        match = MagicMock(
            token="TOKEN",
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            final_districts=["D1"],
            participants=[
                MagicMock(user_id="100", result=[None], bases=[MagicMock(link="https://p1-d1")]),
                MagicMock(user_id="200", result=[None], bases=[MagicMock(link="https://p2-d1")]),
            ],
        )
        request = helper_live.AutomaticLiveHost(
            match=match,
            clan=MagicMock(tag="#CLAN", name="Helper Clan"),
            host=MagicMock(id=888),
            player1=MagicMock(tag="#P1"),
            player2=MagicMock(tag="#P2"),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            requested_by="999",
        )
        side0 = MagicMock()
        side1 = MagicMock()
        clan = MagicMock()
        reservations = live_reservations.LiveStartReservations(side0=side0, side1=side1, clan=clan)

        with (
                patch("elo_bot.features.hosting.live.helper_live.live_start.reserve_live_start",
                      new=AsyncMock(return_value=reservations)),
                patch("elo_bot.features.base_store.storage.sync_base_store_slots",
                      new=AsyncMock(return_value={"#CLAN": ["Slot 2"]})) as sync_slots,
                patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'),
                      new=AsyncMock(return_value=match)) as choose_bases,
                patch("elo_bot.features.hosting.live.helper_live.Match.find_one", new=AsyncMock(return_value=match)),
                patch("elo_bot.features.hosting.live.helper_live.confirm_helper_availability",
                      new=AsyncMock(return_value=True)) as confirm_eta,
                patch("elo_bot.features.hosting.live.helper_live.live_start.start_live_match",
                      new=AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock()))) as start,
                patch("elo_bot.features.hosting.live.helper_live.run_helper_host_job", new_callable=AsyncMock) as run_job,
        ):
            await helper_live.host_live_automatically(runtime, ctx, request)

        sync_slots.assert_awaited_once_with("#CLAN", bot=runtime.bot)
        choose_bases.assert_not_awaited()
        confirm_eta.assert_not_awaited()
        start.assert_not_awaited()
        run_job.assert_not_awaited()
        ctx.send.assert_awaited_once()
        self.assertIn("both base slots", ctx.send.await_args.args[0].lower())
        clan.release.assert_called_once_with()
        side1.release.assert_called_once_with()
        side0.release.assert_called_once_with()

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_host_clan_select_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a started war side whose replacement clan can be persisted.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "host_clan_select|TOKEN|0"
        ctx.values = ["#NEW"]
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**", fields=[MagicMock(), MagicMock(), MagicMock()])]
        events = []

        match = MagicMock()
        match.id = "MATCH"
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = ["999", None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [MagicMock(result=[]), MagicMock(result=[])]

        async def record_match_set(_query, _update):
            events.append(("persist", match.host_clans[0]))
            return MagicMock(matched_count=1)

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_edit(*_args, **_kwargs):
            events.append(("discord", match.host_clans[0]))

        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(side_effect=record_match_set)
        mock_match.find_one = AsyncMock(return_value=match)
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.message.edit = AsyncMock(side_effect=record_discord_edit)
        self.bot.coc_client.get_clan = AsyncMock(
            return_value=MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        )
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the side's host clan is changed.
        async def publish_existing(_ctx, _match, _expected, *, embed, existing_message):
            await existing_message.edit(embed=embed)

        with patch_sites(('elo_bot.extensions.match.hosting.publish_match_message', 'elo_bot.extensions.match.settings.publish_match_message'), new=AsyncMock(side_effect=publish_existing)) as publisher:
            await self.cog.host_clan_select.callback.func(self.cog, ctx)
        publisher.assert_awaited_once()
        self.assertIs(publisher.await_args.kwargs["existing_message"], ctx.message)

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#NEW"),
                ("free", "#NEW"),
                ("discord", "#NEW"),
            ],
        )
        collection.update_one.assert_awaited_once()
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 123)
        self.assertEqual(collection.update_one.await_args.args[1], {"$set": {
            "host_clans.0": "#NEW",
            "persistent_host_clans.0": "#NEW",
        }})
        self.assertEqual(match.persistent_host_clans[0], "#NEW")
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0)

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_host_clan_select_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a valid war-side clan selection whose durable write fails.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "host_clan_select|TOKEN|0"
        ctx.values = ["#NEW"]
        ctx.author.id = 999
        ctx.message.id = 123
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**", fields=[MagicMock(), MagicMock(), MagicMock()])]
        ctx.message.edit = AsyncMock()

        match = MagicMock()
        match.id = "MATCH"
        match.is_war = True
        match.old_msgs = [123, None]
        match.times = [100, None]
        match.hosts = ["999", None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [MagicMock(result=[]), MagicMock(result=[])]
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            side_effect=RuntimeError("match write failed"),
        )
        mock_match.find_one = AsyncMock(return_value=match)
        self.bot.coc_client.get_clan = AsyncMock(
            return_value=MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        )
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the durable match update fails, then no occupancy or Discord mutation follows.
        with self.assertRaisesRegex(RuntimeError, "match write failed"):
            await self.cog.host_clan_select.callback.func(self.cog, ctx)

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        ctx.send.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_set_clan_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a scheduled match whose selected host clan can be persisted.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 12345

        old_message = MagicMock()
        old_message.edit = AsyncMock()
        events = []

        match = MagicMock()
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [1234567890, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [777, None]
        match.msg = None
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]

        async def record_match_set(_update):
            events.append(("persist", match.host_clans[0]))

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player1_index, _player2_index):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_fetch(_message_id):
            events.append(("discord", match.host_clans[0]))
            return old_message

        match.set = AsyncMock(side_effect=record_match_set)
        mock_match.find_one = AsyncMock(return_value=match)
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.channel.fetch_message = AsyncMock(side_effect=record_discord_fetch)

        clan = MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        self.bot.fetch_channel = AsyncMock(return_value=MagicMock())

        # When the host clan is changed.
        async def publish_existing(_ctx, _match, _expected, *, embed, existing_message):
            await existing_message.edit(embed=embed)

        with patch_sites(('elo_bot.extensions.match.hosting.publish_match_message', 'elo_bot.extensions.match.settings.publish_match_message'), new=AsyncMock(side_effect=publish_existing)) as publisher:
            await self.cog.match_set_clan.callback.func(self.cog, ctx, clan="#NEW")
        publisher.assert_awaited_once()
        self.assertIs(publisher.await_args.kwargs["existing_message"], old_message)

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#NEW"),
                ("free", "#NEW"),
                ("discord", "#NEW"),
            ],
        )
        match.set.assert_awaited_once_with({
            "host_clans.0": "#NEW",
            "persistent_host_clans.0": "#NEW",
        })
        self.assertEqual(match.persistent_host_clans[0], "#NEW")
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0, 1)

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_set_clan_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_free_clan, mock_occupy_clan, mock_clan_find):
        # Given a valid scheduled match whose host-clan write fails.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.channel.id = 12345
        ctx.channel.fetch_message = AsyncMock()

        match = MagicMock()
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [1234567890, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [777, None]
        match.msg = None
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        mock_match.find_one = AsyncMock(return_value=match)

        clan = MagicMock(name="Test Clan", tag="#NEW", share_link="http://clan")
        self.bot.coc_client.get_clan = AsyncMock(return_value=clan)
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        # When the durable match update fails, then no occupancy or Discord mutation follows.
        with self.assertRaisesRegex(RuntimeError, "match write failed"):
            await self.cog.match_set_clan.callback.func(self.cog, ctx, clan="#NEW")

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.channel.fetch_message.assert_not_awaited()
        ctx.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.lifecycle.remove_helper_task', 'elo_bot.extensions.match.results.remove_helper_task', 'elo_bot.extensions.match.result_store.remove_helper_task', 'elo_bot.features.matches.resources.remove_helper_task', 'elo_bot.extensions.final.remove_helper_task'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.results.choose_tip', 'elo_bot.extensions.match.hosting.choose_tip', 'elo_bot.extensions.match.settings.choose_tip', 'elo_bot.features.matches.tips.choose_tip'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new_callable=AsyncMock)
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_start_updates_occupancy_before_discord_side_effects(
            self, mock_match, mock_player, _mock_is_raid_weekend, mock_is_clan_available, mock_choose_tip,
            mock_send_bases, mock_clear_base_stores, mock_free_clan, mock_occupy_clan,
            _mock_remove_helper_task, mock_clan_find):
        # Given a valid unstarted match and an available replacement clan.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.name = "🟡 Match Thread"
        start_message = MagicMock(id=777)
        start_message.pin = AsyncMock()
        ctx.channel.edit = AsyncMock()
        events = []

        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [None, None]
        match.msg = None
        match.final_districts = ["D1", "D2", "D3"]
        match.participants = [
            MagicMock(
                user_id="123",
                bases=[MagicMock(link="http://p1-1"), MagicMock(link="http://p1-2"), MagicMock(link="http://p1-3")],
                base_store=None, opponent_bases_stored=False,
            ),
            MagicMock(
                user_id="456",
                bases=[MagicMock(link="http://p2-1"), MagicMock(link="http://p2-2"), MagicMock(link="http://p2-3")],
                base_store=None, opponent_bases_stored=False,
            ),
        ]
        async def record_match_set(update):
            if "is_war" in update:
                events.append(("persist", match.host_clans[0]))

        async def record_free(_old_clan, **_kwargs):
            events.append(("free", match.host_clans[0]))

        async def record_occupy(_match, _player1_index, _player2_index):
            events.append(("occupy", match.host_clans[0]))

        async def record_discord_send(*_args, **kwargs):
            self.assertEqual(kwargs["components"], [])
            self.assertNotEqual(kwargs["embed"].title, "**Match Started**")
            events.append(("pending", match.host_clans[0]))
            return start_message

        async def record_binding(query, update):
            if "message_publications" in update["$set"]:
                self.assertEqual(query["message_publications"], {"$exists": False})
                self.assertEqual(update["$set"]["message_publications"], [None, None])
                return MagicMock(matched_count=1)
            if set(update["$set"]) == {"message_publications.0"}:
                self.assertTrue(query["message_publications.0"]["pending"])
                self.assertFalse(update["$set"]["message_publications.0"].get("pending", False))
                self.assertEqual(query["old_msgs.0"], 777)
                return MagicMock(matched_count=1)
            if "public_message_publication" in update["$set"]:
                self.assertEqual(update["$set"]["public_message_publication"]["message_id"], 888)
                if "msg" in update["$set"]:
                    self.assertIsNone(query["msg"])
                    self.assertEqual(update["$set"]["msg"], "888")
                    self.assertTrue(update["$set"]["public_message_publication"]["pending"])
                else:
                    self.assertEqual(query["msg"], "888")
                    self.assertTrue(query["public_message_publication"]["pending"])
                    self.assertFalse(update["$set"]["public_message_publication"].get("pending", False))
                return MagicMock(matched_count=1)
            self.assertEqual(query["host_clans.0"], "#CLAN")
            self.assertIsNone(query["host_clans.1"])
            self.assertEqual(set(update["$set"]), {"old_msgs.0", "message_publications.0"})
            self.assertEqual(update["$set"]["old_msgs.0"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["message_id"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["embeds"][0]["title"], "**Match Started**")
            events.append(("bind", match.host_clans[0]))
            return MagicMock(matched_count=1)

        async def record_activate(**kwargs):
            self.assertEqual(match.old_msgs[0], 777)
            self.assertEqual(kwargs["embed"]["title"], "**Match Started**")
            events.append(("activate", match.host_clans[0]))

        match.set = AsyncMock(side_effect=record_match_set)
        match.get_saved_state.return_value = None
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(side_effect=record_binding)
        collection.find_one = AsyncMock(return_value={"_id": match.id})
        start_message.edit = AsyncMock(side_effect=record_activate)
        mock_match.find_one = AsyncMock(side_effect=[match, match, match, match, match])
        mock_free_clan.side_effect = record_free
        mock_occupy_clan.side_effect = record_occupy
        ctx.channel.send = AsyncMock(side_effect=record_discord_send)

        p1_doc = MagicMock(tag="#P1", elo=1200)
        p2_doc = MagicMock(tag="#P2", elo=1100)
        mock_player.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])

        player1_acc = MagicMock(town_hall=3, tag="#P1", share_link="http://p1")
        player2_acc = MagicMock(town_hall=3, tag="#P2", share_link="http://p2")
        clan_obj = MagicMock()
        clan_obj.name = "Test Clan"
        clan_obj.tag = "#CLAN"
        clan_obj.share_link = "http://clan"
        public_message = MagicMock(id=888, edit=AsyncMock())
        public_channel = MagicMock()
        public_channel.send = AsyncMock(return_value=public_message)

        mock_is_clan_available.return_value = True
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        mock_choose_tip.return_value = "tip"

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
                    new_callable=AsyncMock,
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                ) as mock_start_slots,
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[player1_acc, player2_acc])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            self.bot.fetch_channel = AsyncMock(return_value=public_channel)

            # When the match is started.
            await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        # Then persistence and in-memory sync precede occupancy, which precedes Discord I/O.
        self.assertEqual(
            events,
            [
                ("persist", "#OLD"),
                ("occupy", "#CLAN"),
                ("free", "#CLAN"),
                ("pending", "#CLAN"),
                ("bind", "#CLAN"),
                ("activate", "#CLAN"),
            ],
        )
        mock_free_clan.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        mock_occupy_clan.assert_awaited_once_with(match, 0, 1)
        self.assertEqual(collection.update_one.await_args.args[0]["old_msgs.0"], 777)
        self.assertEqual(mock_send_bases.await_args_list[0].args[:4], (ANY, "TOKEN", 1, 0))
        self.assertEqual(mock_send_bases.await_args_list[1].args[:4], (ANY, "TOKEN", 0, 1))
        self.assertEqual(match.persistent_host_clans[0], "#CLAN")
        mock_clear_base_stores.assert_not_awaited()
        self.assertEqual(public_channel.send.await_args.args[0], "tip")
        self.assertEqual(public_channel.send.await_args.kwargs["components"], [])
        ctx.send.assert_called()
        self.assertIn("Successfully started the match", ctx.send.call_args[0][0])
        self.assertIn("non-occupied base slot `Slot 2`", ctx.send.call_args[0][0])
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)

    async def test_match_start_uses_shared_live_start_after_atomic_claim(self) -> None:
        from elo_bot.features.hosting.live import live_start

        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author = MagicMock(id=999, mention="<@999>")
        ctx.channel.id = 12345
        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
        )
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        shared_start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=MagicMock()))

        with (
                patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model,
                patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player')) as player_model,
                patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False),
                patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)),
                patch('elo_bot.extensions.match.hosting.is_resource_clan_locked', return_value=False),
                patch('elo_bot.extensions.match.hosting.get_start_base_store_slot_availability', new=AsyncMock(
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                )),
                patch('elo_bot.extensions.match.hosting.live_start.start_live_match', new=shared_start),
                patch_sites(('elo_bot.extensions.match.hosting.claim_resource_clan', 'elo_bot.extensions.match.settings.claim_resource_clan', 'elo_bot.features.matches.resources.claim_resource_clan'), new_callable=AsyncMock) as direct_claim,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(side_effect=[
                MagicMock(tag="#P1", elo=1200), MagicMock(tag="#P2", elo=1100),
            ])
            self.cog.coc_client.get_player = AsyncMock(side_effect=[
                MagicMock(town_hall=3), MagicMock(town_hall=3),
            ])
            self.cog.coc_client.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        shared_start.assert_awaited_once()
        direct_claim.assert_not_awaited()

    async def test_live_start_holds_both_side_reservations_until_claim(self) -> None:
        from elo_bot.features.hosting.live import live_start

        resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match = MagicMock(
            token="TOKEN", times=[None, None], hosts=[None, None], host_clans=[None, None],
            persistent_host_clans=[None, None], old_msgs=[None, None], is_war=False,
        )
        request = live_start.LiveStart(
            match=match, host=MagicMock(id=999), clan=MagicMock(tag="#CLAN"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=1200, player2_elo=1100,
            scout=False, reply=live_start.AutomaticLiveStartReply(),
        )
        claim_entered = asyncio.Event()
        allow_claim = asyncio.Event()

        async def pending_claim(*_args, **_kwargs):
            claim_entered.set()
            with anyio.fail_after(5):
                await allow_claim.wait()
            return False

        with (
                patch_sites(('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'),
                      new=AsyncMock(return_value=match)),
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                      new=AsyncMock(return_value=MagicMock())),
                patch_sites(('elo_bot.extensions.match.hosting.claim_resource_clan', 'elo_bot.extensions.match.settings.claim_resource_clan', 'elo_bot.features.matches.resources.claim_resource_clan'),
                      new=AsyncMock(side_effect=pending_claim)),
        ):
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    task = tasks.create_task(live_start.start_live_match(MagicMock(), MagicMock(), request))
                    await claim_entered.wait()
                    self.assertIsNone(await match_base_store_module.try_reserve_resource_match_side("TOKEN", 1))
                    allow_claim.set()
                self.assertIsInstance(task.result(), live_start.LiveStartClanUnavailable)

        for player in (0, 1):
            reservation = await match_base_store_module.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        with (
                patch_sites(('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'),
                      new=AsyncMock(return_value=match)),
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                      new=AsyncMock(return_value=MagicMock())),
                patch_sites(('elo_bot.extensions.match.hosting.claim_resource_clan', 'elo_bot.extensions.match.settings.claim_resource_clan', 'elo_bot.features.matches.resources.claim_resource_clan'),
                      new=AsyncMock(return_value=True)),
                patch("elo_bot.features.hosting.live.live_start.complete_live_start",
                      new=AsyncMock(return_value=MagicMock())),
        ):
            self.assertIsInstance(
                await live_start.start_live_match(MagicMock(), MagicMock(), request),
                live_start.LiveStartSucceeded,
            )
        for player in (0, 1):
            reservation = await match_base_store_module.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        busy = MagicMock(times=[None, 1], hosts=[None, None])
        with patch_sites(('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'),
                   new=AsyncMock(return_value=busy)):
            self.assertIsInstance(
                await live_start.start_live_match(MagicMock(), MagicMock(), request),
                live_start.LiveStartBusy,
            )
        for player in (0, 1):
            reservation = await match_base_store_module.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

        with (
                patch_sites(('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'),
                      new=AsyncMock(return_value=match)),
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                      new=AsyncMock(return_value=MagicMock())),
                patch_sites(('elo_bot.extensions.match.hosting.claim_resource_clan', 'elo_bot.extensions.match.settings.claim_resource_clan', 'elo_bot.features.matches.resources.claim_resource_clan'),
                      new=AsyncMock(side_effect=RuntimeError("claim failed"))),
        ):
            with self.assertRaisesRegex(RuntimeError, "claim failed"):
                await live_start.start_live_match(MagicMock(), MagicMock(), request)
        for player in (0, 1):
            reservation = await match_base_store_module.try_reserve_resource_match_side("TOKEN", player)
            self.assertIsNotNone(reservation)
            reservation.release()

    async def test_live_start_persistence_failure_has_no_occupancy_or_discord_effects(self) -> None:
        from elo_bot.features.hosting.live import live_start

        match = MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            old_msgs=[None, None],
            model_fields_set={"persistent_host_clans"},
        )
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        side0 = MagicMock()
        side1 = MagicMock()
        clan_reservation = MagicMock(clan_tag="#CLAN")
        ctx = MagicMock()
        ctx.channel.send = AsyncMock()
        request = live_start.LiveStart(
            match=match,
            host=MagicMock(id=999),
            clan=MagicMock(tag="#CLAN"),
            player1=MagicMock(),
            player2=MagicMock(),
            player1_elo=1200,
            player2_elo=1100,
            scout=False,
            reply=live_start.AutomaticLiveStartReply(),
        )

        with (
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_match_side', 'elo_bot.extensions.match.hosting.try_reserve_resource_match_side', 'elo_bot.features.matches.resources.try_reserve_resource_match_side'),
                      new=AsyncMock(side_effect=[side0, side1])),
                patch_sites(('elo_bot.extensions.match.base_store.try_reserve_resource_clan', 'elo_bot.extensions.match.hosting.try_reserve_resource_clan', 'elo_bot.extensions.match.settings.try_reserve_resource_clan', 'elo_bot.features.matches.resources.try_reserve_resource_clan'),
                      new=AsyncMock(return_value=clan_reservation)),
                patch_sites(('elo_bot.features.base_store.match_adapters.Match.find_one', 'elo_bot.extensions.match.lifecycle.Match.find_one', 'elo_bot.extensions.match.bases.Match.find_one', 'elo_bot.extensions.match.base_store.Match.find_one', 'elo_bot.extensions.match.results.Match.find_one', 'elo_bot.extensions.match.result_store.Match.find_one', 'elo_bot.extensions.match.hosting.Match.find_one', 'elo_bot.extensions.match.settings.Match.find_one', 'elo_bot.extensions.match.dashboards.Match.find_one', 'elo_bot.features.queue.state.Match.find_one', 'elo_bot.features.matches.occupancy.Match.find_one', 'elo_bot.features.matches.resources.Match.find_one', 'elo_bot.features.matches.lifecycle.Match.find_one', 'elo_bot.features.matches.attack_bases.Match.find_one', 'elo_bot.features.results.recording.Match.find_one'), new=AsyncMock(return_value=match)),
                patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"))),
                patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy,
        ):
            with self.assertRaisesRegex(RuntimeError, "match write failed"):
                await live_start.start_live_match(MagicMock(), ctx, request)

        occupy.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.lifecycle.remove_helper_task', 'elo_bot.extensions.match.results.remove_helper_task', 'elo_bot.extensions.match.result_store.remove_helper_task', 'elo_bot.features.matches.resources.remove_helper_task', 'elo_bot.extensions.final.remove_helper_task'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new_callable=AsyncMock, return_value=True)
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_start_persistence_failure_skips_occupancy_and_discord(
            self, mock_match, mock_player, _mock_is_raid_weekend, _mock_is_clan_available, mock_send_bases,
            mock_clear_base_stores, mock_free_clan, mock_occupy_clan, mock_remove_helper_task, mock_clan_find):
        # Given a valid unstarted match whose initial state write fails.
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()

        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_autowin = False
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = ["#OLD", None]
        match.persistent_host_clans = ["#PREFERRED", None]
        match.model_fields_set = {"persistent_host_clans"}
        match.participants = [
            MagicMock(user_id="123", result=[]),
            MagicMock(user_id="456", result=[]),
        ]
        match.set = AsyncMock(side_effect=RuntimeError("match write failed"))
        mock_match.find_one = AsyncMock(return_value=match)

        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(tag="#P1", elo=1200),
            MagicMock(tag="#P2", elo=1100),
        ])
        player1_acc = MagicMock(town_hall=3)
        player2_acc = MagicMock(town_hall=3)
        clan_obj = MagicMock(name="Test Clan", tag="#CLAN", share_link="http://clan")
        mock_clan_find.return_value = MagicMock(availability_str="Available")

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
                    new_callable=AsyncMock,
                    return_value=(["Slot 2"], {"Slot 1": ("987", "789")}),
                ),
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[player1_acc, player2_acc])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)

            # When the durable match update fails, then no occupancy or Discord mutation follows.
            with self.assertRaisesRegex(RuntimeError, "match write failed"):
                await self.cog.match_start.callback.func(self.cog, ctx, clan="#CLAN")

        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        mock_free_clan.assert_not_awaited()
        mock_occupy_clan.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        ctx.send.assert_not_awaited()
        self.bot.fetch_channel.assert_not_awaited()
        mock_send_bases.assert_not_awaited()
        mock_clear_base_stores.assert_not_awaited()
        mock_remove_helper_task.assert_not_awaited()

    @patch(
        'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
        new_callable=AsyncMock,
        return_value=([], {"Slot 1": ("200", "2"), "Slot 2": ("300", "3")}),
    )
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new_callable=AsyncMock, return_value=True)
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_start_blocks_when_both_foreign_slots_are_occupied(
            self, mock_match, mock_player, _mock_is_clan_available, _mock_is_raid_weekend, mock_start_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 12345

        match = MagicMock(
            thread="12345",
            is_war=False,
            times=[None, None],
            participants=[MagicMock(user_id="123"), MagicMock(user_id="456")],
        )
        match.set = AsyncMock()
        mock_match.find_one = AsyncMock(return_value=match)
        mock_player.find_one = AsyncMock(side_effect=[
            MagicMock(tag="#P1", elo=1200),
            MagicMock(tag="#P2", elo=1100),
        ])
        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock) as mock_send_bases,
        ):
            mock_coc_client.return_value.get_player = AsyncMock(side_effect=[
                MagicMock(town_hall=3),
                MagicMock(town_hall=3),
            ])
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_start.callback(ctx, clan="#CLAN")

        mock_send_bases.assert_not_awaited()
        match.set.assert_not_awaited()
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)
        response = ctx.send.call_args[0][0]
        self.assertIn("both base slots", response.lower())
        self.assertIn("<#200> <@2>", response)
        self.assertIn("<#300> <@3>", response)

    @patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'))
    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=True)
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_start_blocks_without_stored_bases_during_raid_weekend(
            self, mock_match, mock_player, _mock_is_raid_weekend, mock_clear_base_stores):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock()
        match.is_war = False
        match.times = [None, None]
        match.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match.find_one = AsyncMock(return_value=match)

        p1_doc = MagicMock(tag="#P1")
        p2_doc = MagicMock(tag="#P2")
        mock_player.find_one = AsyncMock(side_effect=[p1_doc, p2_doc])
        player_acc = MagicMock(town_hall=3)
        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_player = AsyncMock(return_value=player_acc)
            await self.cog.match_start.callback(ctx, clan="#CLAN")

        ctx.send.assert_called()
        self.assertIn("requires both players' bases", ctx.send.call_args[0][0])
        self.assertIn("separate slots in the same clan", ctx.send.call_args[0][0])
        mock_clear_base_stores.assert_not_called()

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_start_blocks_finished_regular_match(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        match = MagicMock(spec=Match)
        match.is_autowin = False
        match.is_war = False
        match.participants = [
            MagicMock(user_id="111", result=["3.100", "2.90", "2.80"]),
            MagicMock(user_id="222", result=["2.90", "2.80", "1.70"]),
        ]
        mock_match.find_one = AsyncMock(return_value=match)

        await self.cog.match_start.callback(ctx, clan="#CLAN")

        self.assertIn("finished", ctx.send.call_args[0][0].lower())

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_raid_weekend_start_notify_skips_ended_matches(self, mock_match):
        cases = [
            ("autowin", True, [None] * 3, "match-thread"),
            ("completed results", False, ["3.100"] * 3, "match-thread"),
            ("red thread", False, [None] * 3, "🔴 match-thread"),
        ]
        for reason, autowin, results, thread_name in cases:
            with self.subTest(reason=reason):
                thread = AsyncMock(spec=ipy.ThreadChannel)
                thread.name = thread_name
                thread.send = AsyncMock()
                self.bot.fetch_channel = AsyncMock(return_value=thread)
                match = MagicMock(spec=Match)
                match.thread = "123"
                match.is_autowin = autowin
                match.participants = [
                    MagicMock(user_id=user_id, opponent_bases_stored=False, result=list(results))
                    for user_id in ("111", "222")
                ]
                mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

                callback = getattr(self.cog.raid_weekend_start_notify, "callback", self.cog.raid_weekend_start_notify)
                await callback()

                thread.send.assert_not_called()

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_raid_weekend_start_notify_sends_to_active_match(self, mock_match):
        thread = AsyncMock(spec=ipy.ThreadChannel)
        thread.name = "match-thread"
        thread.send = AsyncMock()
        self.bot.fetch_channel = AsyncMock(return_value=thread)

        active_match = MagicMock(spec=Match)
        active_match.thread = "123"
        active_match.is_autowin = False
        active_match.participants = [
            MagicMock(user_id="111", opponent_bases_stored=False, result=[None, None, None]),
            MagicMock(user_id="222", opponent_bases_stored=True, result=[None, None, None]),
        ]
        find_result = MagicMock()
        find_result.to_list = AsyncMock(return_value=[active_match])
        mock_match.find.return_value = find_result

        callback = getattr(self.cog.raid_weekend_start_notify, "callback", self.cog.raid_weekend_start_notify)
        await callback()

        thread.send.assert_called_once()
        self.assertIn("<@111>", thread.send.call_args[0][0])
        self.assertIn("raid weekend is starting soon", thread.send.call_args[0][0])

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock, ANY, call
import anyio
import interactions as ipy
from elo_bot.features.base_store.match_adapters import (
    collect_occupied_base_store_slots, clear_participant_base_store, clear_match_base_stores,
    construct_start_slot_instruction, get_start_base_store_slot_availability,
)
from elo_bot.db import BaseStore, Participant
from elo_bot.config.game import BASE_STORE_SLOTS
from elo_bot.features.base_store.slots import (
    clear_all_base_store_slots, collect_base_store_slot_owners, format_base_store_slot_status,
)
from tests._patch_sites import patch_sites
from tests._match_commands_case import MatchCommandsCase


class TestMatchCommands(MatchCommandsCase):
    def test_collect_occupied_base_store_slots(self):
        match = MagicMock()
        match.thread = "987"
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            ),
            MagicMock(base_store=None),
        ]

        occupied = collect_occupied_base_store_slots([match])

        self.assertEqual(occupied, {"#CLAN": {"Slot 1"}})
        orphaned_match = MagicMock(
            thread=None,
            participants=[MagicMock(
                user_id="456",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 2", stored_at=10),
            )],
        )
        self.assertEqual(
            collect_occupied_base_store_slots([match, orphaned_match]),
            {"#CLAN": {"Slot 1"}},
        )

        slot_owners = collect_base_store_slot_owners([match])
        self.assertEqual(slot_owners, {"#CLAN": {"Slot 1": ("987", "123")}})
        self.assertEqual(
            format_base_store_slot_status("#CLAN", {}),
            "",
        )
        self.assertEqual(
            format_base_store_slot_status("#CLAN", slot_owners),
            "Slot 1: <#987> <@123>",
        )

    @patch("elo_bot.features.base_store.repository.Match")
    async def test_start_slot_availability_excludes_current_thread_and_tracks_foreign_slots(self, mock_match):
        current_match = MagicMock(thread="100")
        current_match.participants = [
            MagicMock(
                user_id="1",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        foreign_slot_1 = MagicMock(thread="200")
        foreign_slot_1.participants = [
            MagicMock(
                user_id="2",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                ),
            )
        ]
        foreign_slot_2 = MagicMock(thread="300")
        foreign_slot_2.participants = [
            MagicMock(
                user_id="3",
                base_store=BaseStore(
                    clan_tag="#CLAN", slot="Slot 2", stored_at=3000000000
                ),
            )
        ]
        mock_match.find.return_value.to_list = AsyncMock(side_effect=[
            [current_match],
            [current_match, foreign_slot_1],
            [current_match, foreign_slot_1, foreign_slot_2],
        ])

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, list(BASE_STORE_SLOTS))
        self.assertEqual(owners, {})
        self.assertEqual(construct_start_slot_instruction(available, "Test Clan"), "")

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, ["Slot 2"])
        self.assertEqual(owners, {"Slot 1": ("200", "2")})
        self.assertEqual(
            construct_start_slot_instruction(available, "Test Clan"),
            "\n\nPlease use the non-occupied base slot `Slot 2` in **Test Clan**.",
        )

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")
        self.assertEqual(available, [])
        self.assertEqual(owners, {
            "Slot 1": ("200", "2"),
            "Slot 2": ("300", "3"),
        })

    @patch("elo_bot.features.base_store.repository.Match")
    async def test_start_slot_availability_ignores_unrelated_reservations(self, mock_match):
        cases = [
            ("unmapped legacy", MagicMock(
                thread="200",
                participants=[MagicMock(user_id="2", base_store=None, opponent_bases_stored=True)],
                times=[None],
            )),
            ("no thread owner", MagicMock(
                thread=None,
                participants=[
                    MagicMock(
                        user_id=str(index),
                        base_store=BaseStore(clan_tag="#CLAN", slot=slot, stored_at=3000000000),
                    )
                    for index, slot in enumerate(BASE_STORE_SLOTS, start=2)
                ],
            )),
            ("other clan", MagicMock(
                thread="200",
                participants=[MagicMock(
                    user_id="2",
                    base_store=BaseStore(clan_tag="#OTHER", slot="Slot 1", stored_at=3000000000),
                )],
            )),
        ]
        for reason, match in cases:
            with self.subTest(reason=reason):
                mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

                available, owners = await get_start_base_store_slot_availability("#CLAN", "100")

                self.assertEqual(available, list(BASE_STORE_SLOTS))
                self.assertEqual(owners, {})

    @patch("elo_bot.features.base_store.repository.latest_completed_raid_weekend_end_timestamp", return_value=200)
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_start_slot_availability_clears_expired_reservations(
            self, mock_match, _mock_latest_raid_end):
        participant = MagicMock(
            user_id="2",
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100),
            opponent_bases_stored=True,
        )
        expired_match = MagicMock(thread="200", participants=[participant])
        expired_match.save_changes = AsyncMock()
        self.enable_base_store_clear(expired_match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[expired_match])

        available, owners = await get_start_base_store_slot_availability("#CLAN", "100")

        self.assertEqual(available, list(BASE_STORE_SLOTS))
        self.assertEqual(owners, {})
        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        expired_match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        expired_match.save_changes.assert_not_awaited()

    @patch("elo_bot.features.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_participant_base_store_releases_slot_and_stored_flag(self, mock_sync_slots):
        participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            opponent_bases_stored=True
        )
        match = MagicMock(participants=[participant])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_participant_base_store(match, 0, reset_stored_flag=True, reason="undone")

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        mock_sync_slots.assert_awaited_once_with("#CLAN", bot=None)

    @patch("elo_bot.features.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_participant_base_store_resynchronizes_unmapped_legacy_reservation(self, mock_sync_slots):
        participant = MagicMock(base_store=None, opponent_bases_stored=True)
        match = MagicMock(participants=[participant])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_participant_base_store(match, 0, reset_stored_flag=True, reason="undone")

        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        mock_sync_slots.assert_awaited_once_with(None, bot=None)

    @patch("elo_bot.features.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    async def test_clear_match_base_stores_releases_all_slots_and_flags(self, mock_sync_slots):
        participants = [
            MagicMock(
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
                opponent_bases_stored=True
            ),
            MagicMock(
                base_store=BaseStore(clan_tag="#CLAN2", slot="Slot 2", stored_at=20),
                opponent_bases_stored=True
            ),
        ]
        match = MagicMock(participants=participants)
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        await clear_match_base_stores(match, reset_stored_flags=True, reason="undone")

        self.assertTrue(all(participant.base_store is None for participant in participants))
        self.assertTrue(all(not participant.opponent_bases_stored for participant in participants))
        self.assertEqual(match.clear_base_store.await_args_list, [
            call(0, reset_stored_flag=True), call(1, reset_stored_flag=True),
        ])
        match.save_changes.assert_not_awaited()
        self.assertEqual(mock_sync_slots.await_args_list, [
            call("#CLAN", bot=None), call("#CLAN2", bot=None),
        ])

    @patch("elo_bot.features.base_store.slots.Clan")
    @patch("elo_bot.features.base_store.slots.Match")
    async def test_clear_all_base_store_slots_frees_match_metadata_and_clan_slots(self, mock_match, mock_clan):
        stored_participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=10),
            opponent_bases_stored=True
        )
        started_participant = MagicMock(base_store=None, opponent_bases_stored=True)
        match = MagicMock(participants=[stored_participant, started_participant], times=[None, 100])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)

        legacy_participant = MagicMock(base_store=None, opponent_bases_stored=True)
        legacy_match = MagicMock(participants=[legacy_participant], times=[None])
        legacy_match.save_changes = AsyncMock()
        self.enable_base_store_clear(legacy_match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match, legacy_match])

        clan = MagicMock(available_slots=["Slot 1"])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        await clear_all_base_store_slots()

        self.assertIsNone(stored_participant.base_store)
        self.assertFalse(stored_participant.opponent_bases_stored)
        self.assertTrue(started_participant.opponent_bases_stored)
        self.assertFalse(legacy_participant.opponent_bases_stored)
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        legacy_match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        legacy_match.save_changes.assert_not_awaited()
        clan.save_changes.assert_awaited_once()

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.bases.BaseLayout', 'elo_bot.features.matches.attack_bases.BaseLayout', 'elo_bot.features.results.recording.BaseLayout'))
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_success(
            self, mock_choose_bases, mock_base_layout, mock_preparation_match, mock_match, mock_sync_slots):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        clan_obj.share_link = "http://clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}
        
        # Mock match found
        mock_match_doc = MagicMock()
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_message_binding(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one
        
        # Mock existing base check (none found)
        mock_base_layout.find_one = AsyncMock(return_value=None)
        
        # Execute
        with (
            patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("elo_bot.features.base_store.publication.Match.find_one", new=AsyncMock(return_value=mock_match_doc)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        # Verify
        mock_choose_bases.assert_called()
        mock_match_doc.get_pymongo_collection.return_value.update_one.assert_awaited_once()
        self.assertEqual(mock_match_doc.participants[0].base_store.clan_tag, "#CLAN")
        self.assertEqual(mock_match_doc.participants[0].base_store.slot, "Slot 1")
        self.assertEqual(ctx.channel.send.await_args.kwargs["embed"].title, "**Base Storage**")
        self.assertEqual(ctx.channel.send.await_args.kwargs["components"], [])
        published = ctx.channel.send.return_value
        self.assertEqual(published.edit.await_args.kwargs["embed"]["title"], "**Base Slot Reserved**")
        self.assertTrue(published.edit.await_args.kwargs["components"])
        self.assertEqual(mock_match_doc.participants[0].base_store.message_id, int(published.id))
        self.assertTrue(mock_match_doc.participants[0].base_store.ready)
        ctx.send.assert_called()
        self.assertIn("Please store the bases", ctx.send.call_args[0][0])

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_chooses_bases_before_reserving_slot(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.id = 12345
        ctx.channel.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}

        mock_match_doc = MagicMock()
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_message_binding(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        async def choose_bases_side_effect(*_args, **_kwargs):
            self.assertFalse(mock_match_doc.participants[0].opponent_bases_stored)
            self.assertIsNone(mock_match_doc.participants[0].base_store)

        mock_choose_bases.side_effect = choose_bases_side_effect

        with (
            patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("elo_bot.features.base_store.publication.Match.find_one", new=AsyncMock(return_value=mock_match_doc)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_awaited_once()
        self.assertTrue(mock_match_doc.participants[0].opponent_bases_stored)
        self.assertEqual(mock_match_doc.participants[0].base_store.slot, "Slot 1")
        ctx.send.assert_called()
        self.assertIn("Please store the bases", ctx.send.call_args[0][0])

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_serializes_reservations_for_the_same_participant(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        state = {"stored": False, "base_store": None}
        claim_entered = asyncio.Event()
        release_claim = asyncio.Event()
        second_preparation_entered = asyncio.Event()
        prepare = self.cog._prepare_base_store
        preparation_calls = 0

        async def prepare_store(request):
            nonlocal preparation_calls
            preparation_calls += 1
            if preparation_calls == 2:
                second_preparation_entered.set()
            return await prepare(request)

        def make_match():
            participant = MagicMock(
                user_id="123",
                opponent_bases_stored=state["stored"],
                base_store=state["base_store"],
                result=[None, None, None],
            )
            opponent = MagicMock(
                user_id="456",
                bases=[],
                result=[None, None, None],
            )
            match = MagicMock(
                token="TOKEN",
                is_war=False,
                is_autowin=False,
                participants=[participant, opponent],
                final_districts=["D1", "D2", "D3"],
            )

            async def save_changes():
                state["stored"] = participant.opponent_bases_stored
                state["base_store"] = participant.base_store

            match.save_changes = AsyncMock(side_effect=save_changes)
            self.enable_base_store_message_binding(match)
            async def claim(_query, update):
                claim_entered.set()
                with anyio.fail_after(5):
                    await release_claim.wait()
                if state["stored"]:
                    return MagicMock(matched_count=0)
                state["stored"] = True
                state["base_store"] = BaseStore.model_validate(update["$set"]["participants.0.base_store"])
                return MagicMock(matched_count=1)
            match.get_pymongo_collection.return_value.update_one.side_effect = claim
            return match

        mock_match.find_one = AsyncMock(side_effect=lambda *_args, **_kwargs: make_match())
        mock_preparation_match.find_one = mock_match.find_one
        mock_sync_slots.return_value = {"#CLAN": list(BASE_STORE_SLOTS)}

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan", share_link="http://clan")

        def make_ctx():
            ctx = MagicMock(spec=ipy.SlashContext)
            ctx.channel.guild.fetch_member = AsyncMock(
                side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Player {user_id}"),
            )
            ctx.channel.id = 12345
            ctx.channel.send = AsyncMock()
            ctx.defer = AsyncMock()
            ctx.send = AsyncMock()
            ctx.author.id = 999
            return ctx

        contexts = [make_ctx(), make_ctx()]
        with (
            patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
            patch("elo_bot.features.base_store.publication.Match.find_one", new=AsyncMock(side_effect=lambda *_args: make_match())),
            patch.object(self.cog, "_prepare_base_store", new=AsyncMock(side_effect=prepare_store)),
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with anyio.fail_after(5):
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(
                        self.cog.match_base_store.callback(contexts[0], player=0, clan="#CLAN", slot="Slot 1"),
                    )
                    await claim_entered.wait()
                    tasks.create_task(
                        self.cog.match_base_store.callback(contexts[1], player=0, clan="#CLAN", slot="Slot 2"),
                    )
                    await second_preparation_entered.wait()
                    self.assertEqual(mock_choose_bases.await_count, 1)
                    self.assertFalse(state["stored"])
                    release_claim.set()

        self.assertEqual(
            mock_choose_bases.await_count,
            1,
            [ctx.send.await_args_list for ctx in contexts],
        )
        self.assertTrue(state["stored"])
        self.assertEqual(state["base_store"].slot, "Slot 1")
        self.assertTrue(state["base_store"].ready)
        contexts[0].channel.send.assert_awaited_once()
        contexts[1].channel.send.assert_not_awaited()
        contexts[1].send.assert_awaited_once()

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch("elo_bot.features.base_store.storage.sync_base_store_slots", new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_does_not_claim_slot_when_choose_bases_fails(
            self, mock_choose_bases, _mock_storage_sync, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 1", "Slot 2"]}

        mock_match_doc = MagicMock(result_operation=None, autowin_winner_id=None)
        mock_match_doc.token = "TOKEN"
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match_doc.final_districts = ["D1", "D2", "D3"]
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_clear(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one
        mock_choose_bases.side_effect = RuntimeError("base selection failed")

        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with self.assertRaises(RuntimeError):
                await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        self.assertIsNone(mock_match_doc.participants[0].base_store)
        self.assertFalse(mock_match_doc.participants[0].opponent_bases_stored)
        mock_match_doc.save_changes.assert_not_awaited()
        mock_match_doc.clear_base_store.assert_not_awaited()

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_does_not_claim_slot_when_preclaim_resync_fails(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        mock_sync_slots.side_effect = [
            {"#CLAN": ["Slot 1", "Slot 2"]},
            RuntimeError("slot synchronization failed"),
        ]

        participant = MagicMock(user_id="123", opponent_bases_stored=False, base_store=None)
        mock_match_doc = MagicMock(
            token="TOKEN", result_operation=None, autowin_winner_id=None,
            participants=[participant, MagicMock(user_id="456", opponent_bases_stored=False, base_store=None)],
        )
        mock_match_doc.save_changes = AsyncMock()
        self.enable_base_store_clear(mock_match_doc)
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            with self.assertRaisesRegex(RuntimeError, "slot synchronization failed"):
                await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        mock_match_doc.save_changes.assert_not_awaited()
        mock_match_doc.clear_base_store.assert_not_awaited()
        mock_choose_bases.assert_awaited_once()

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_blocks_unavailable_slot(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock()
        clan_obj.tag = "#CLAN"
        clan_obj.name = "Test Clan"
        mock_sync_slots.return_value = {"#CLAN": ["Slot 2"]}

        mock_match_doc = MagicMock(result_operation=None, autowin_winner_id=None)
        mock_match_doc.participants = [MagicMock(user_id="123"), MagicMock(user_id="456")]
        mock_match_doc.participants[0].opponent_bases_stored = False
        mock_match_doc.participants[1].opponent_bases_stored = False
        mock_match_doc.participants[0].base_store = None
        mock_match_doc.participants[1].base_store = None
        mock_match.find_one = AsyncMock(return_value=mock_match_doc)
        mock_preparation_match.find_one = mock_match.find_one

        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_not_called()
        ctx.send.assert_called()
        self.assertIn("already being used", ctx.send.call_args[0][0])

    @patch_sites(('elo_bot.extensions.match.sync_base_store_slots', 'elo_bot.extensions.match.base_store.sync_base_store_slots', 'elo_bot.extensions.match.dashboards.sync_base_store_slots'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    @patch("elo_bot.features.base_store.preparation.Match")
    @patch_sites(('elo_bot.extensions.match.base_store.choose_bases', 'elo_bot.features.matches.attack_bases.choose_bases'))
    async def test_match_base_store_blocks_when_clan_slots_cannot_be_verified(
            self, mock_choose_bases, mock_preparation_match, mock_match, mock_sync_slots):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999

        clan_obj = MagicMock(tag="#CLAN", name="Test Clan")
        mock_sync_slots.return_value = {}

        participant = MagicMock(user_id="123", opponent_bases_stored=False, base_store=None)
        mock_match.find_one = AsyncMock(return_value=MagicMock(
            participants=[participant, MagicMock(user_id="456")], result_operation=None, autowin_winner_id=None,
        ))
        mock_preparation_match.find_one = mock_match.find_one

        with patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client:
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        mock_choose_bases.assert_not_awaited()
        self.assertFalse(participant.opponent_bases_stored)
        self.assertIn("Unable to verify", ctx.send.call_args[0][0])

    @patch('elo_bot.extensions.match.hosting.is_raid_weekend', return_value=False)
    @patch("elo_bot.features.base_store.repository.Clan")
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_sync_base_store_slots_tracks_occupancy_outside_raid_weekend(
            self, mock_match, mock_clan, mock_is_raid_weekend):
        from elo_bot.features.base_store.match_adapters import sync_base_store_slots

        match = MagicMock(participants=[
            MagicMock(base_store=BaseStore(
                clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
            ))
        ])
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        clan = MagicMock()
        clan.tag = "#CLAN"
        clan.available_slots = []
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": ["Slot 2"]})
        self.assertEqual(clan.available_slots, ["Slot 2"])
        clan.save_changes.assert_awaited_once()
        mock_is_raid_weekend.assert_not_called()

    @patch("elo_bot.features.base_store.repository.Clan")
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_sync_base_store_slots_blocks_all_clans_for_pending_legacy_reservation(
            self, mock_match, mock_clan):
        from elo_bot.features.base_store.match_adapters import sync_base_store_slots

        legacy_match = MagicMock(
            thread="987",
            participants=[MagicMock(base_store=None, opponent_bases_stored=True)],
            times=[None],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[legacy_match])

        clans = [
            MagicMock(tag="#CLAN1", available_slots=list(BASE_STORE_SLOTS)),
            MagicMock(tag="#CLAN2", available_slots=list(BASE_STORE_SLOTS)),
        ]
        for clan in clans:
            clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=clans)

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN1": [], "#CLAN2": []})
        for clan in clans:
            self.assertEqual(clan.available_slots, [])
            clan.save_changes.assert_awaited_once()

    @patch("elo_bot.features.base_store.repository.Clan")
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_sync_base_store_slots_ignores_orphaned_reservations(self, mock_match, mock_clan):
        from elo_bot.features.base_store.match_adapters import sync_base_store_slots

        orphaned_match = MagicMock(
            thread=None,
            participants=[
                MagicMock(
                    user_id="1",
                    base_store=BaseStore(
                        clan_tag="#CLAN", slot="Slot 1", stored_at=3000000000
                    ),
                ),
                MagicMock(
                    user_id="2",
                    base_store=None,
                    opponent_bases_stored=True,
                ),
            ],
            times=[None, None],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[orphaned_match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        clan.save_changes.assert_awaited_once()

    @patch("elo_bot.features.base_store.repository.Clan")
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_sync_base_store_slots_does_not_block_for_started_legacy_match(
            self, mock_match, mock_clan):
        from elo_bot.features.base_store.match_adapters import sync_base_store_slots

        started_match = MagicMock(
            participants=[MagicMock(base_store=None, opponent_bases_stored=True)],
            times=[100],
        )
        mock_match.find.return_value.to_list = AsyncMock(return_value=[started_match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})
        self.assertEqual(clan.available_slots, list(BASE_STORE_SLOTS))
        clan.save_changes.assert_awaited_once()

    @patch("elo_bot.features.base_store.repository.latest_completed_raid_weekend_end_timestamp", return_value=200)
    @patch("elo_bot.features.base_store.repository.Clan")
    @patch("elo_bot.features.base_store.repository.Match")
    async def test_sync_base_store_slots_expires_reservations_from_completed_weekend(
            self, mock_match, mock_clan, _mock_latest_raid_end):
        from elo_bot.features.base_store.match_adapters import sync_base_store_slots

        participant = MagicMock(
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100),
            opponent_bases_stored=True,
        )
        match = MagicMock(participants=[participant], times=[None])
        match.save_changes = AsyncMock()
        self.enable_base_store_clear(match)
        mock_match.find.return_value.to_list = AsyncMock(return_value=[match])

        clan = MagicMock(tag="#CLAN", available_slots=[])
        clan.save_changes = AsyncMock()
        mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[clan])

        available = await sync_base_store_slots()

        self.assertIsNone(participant.base_store)
        self.assertFalse(participant.opponent_bases_stored)
        match.clear_base_store.assert_awaited_once_with(0, reset_stored_flag=True)
        match.save_changes.assert_not_awaited()
        self.assertEqual(available, {"#CLAN": list(BASE_STORE_SLOTS)})

    @patch('elo_bot.extensions.match.base_store.clear_participant_base_store', new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_undo_base_storage_button_releases_stored_slot(self, mock_match, mock_clear_base_store):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "undo_base_storage|TOKEN|0"
        ctx.message.delete = AsyncMock()
        ctx.message.edit = AsyncMock()
        ctx.message.components = []
        ctx.message.id = 777
        ctx.message.embeds = [ipy.Embed(title="**Base Slot Reserved**")]
        ctx.send = AsyncMock()

        match = MagicMock(is_war=True, participants=[Participant(
            user_id="123", opponent_bases_stored=True,
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", message_id=777),
        )])
        mock_match.find_one = AsyncMock(return_value=match)
        mock_clear_base_store.return_value = True

        await self.cog.undo_base_storage_button.callback(ctx)

        mock_clear_base_store.assert_awaited_once_with(
            match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
        )
        ctx.message.delete.assert_not_awaited()
        self.assertEqual(ctx.message.edit.await_args.kwargs["components"], [])
        ctx.send.assert_called()
        self.assertIn("undone", ctx.send.call_args[0][0])

    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_failed_manual_store_recovery_does_not_claim_undo_was_restored(self, mock_match):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.channel.id = 12345
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = MagicMock(participants=[
            Participant(
                user_id="123",
                opponent_bases_stored=True,
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="HELD"),
            ),
            Participant(user_id="456"),
        ])
        mock_match.find_one = AsyncMock(return_value=match)

        async def fail_recovery(interaction_ctx, _presentation) -> bool:
            await interaction_ctx.send("Stored-slot controls could not be confirmed.", ephemeral=True)
            return False

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as coc_client,
                patch_sites(
                    ('elo_bot.extensions.match.base_store.base_store_publication.present_retained_base_store', 'elo_bot.extensions.match.results.base_store_publication.present_retained_base_store', 'elo_bot.extensions.match.hosting.base_store_publication.present_retained_base_store'),
                    new=AsyncMock(side_effect=fail_recovery),
                ),
        ):
            coc_client.return_value.get_clan = AsyncMock(return_value=MagicMock())
            await self.cog.match_base_store.callback(ctx, player=0, clan="#CLAN", slot="Slot 1")

        messages = [call.args[0] for call in ctx.send.await_args_list]
        self.assertEqual(messages, ["Stored-slot controls could not be confirmed."])
        self.assertTrue(all("has been restored" not in message for message in messages))

    @patch_sites(('elo_bot.extensions.match.lifecycle.remove_helper_task', 'elo_bot.extensions.match.results.remove_helper_task', 'elo_bot.extensions.match.result_store.remove_helper_task', 'elo_bot.features.matches.resources.remove_helper_task', 'elo_bot.extensions.final.remove_helper_task'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.dashboards.Clan.find_one', 'elo_bot.features.matches.resources.Clan.find_one'), new_callable=AsyncMock)
    @patch('elo_bot.features.matches.resources.occupy_clan', new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock)
    @patch("elo_bot.features.base_store.storage.clear_participant_base_store", new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.bases.send_bases', 'elo_bot.features.matches.attack_bases.send_bases'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.extensions.match.base_store.is_clan_available', 'elo_bot.extensions.match.hosting.is_clan_available'), new_callable=AsyncMock)
    @patch_sites(('elo_bot.features.hosting.war.war_start.Player', 'elo_bot.extensions.match.queue.Player', 'elo_bot.extensions.match.lifecycle.Player', 'elo_bot.extensions.match.results.Player', 'elo_bot.extensions.match.hosting.Player', 'elo_bot.features.queue.state.Player', 'elo_bot.features.queue.matching.Player', 'elo_bot.features.matches.lifecycle.Player', 'elo_bot.features.matches.attack_bases.Player', 'elo_bot.features.results.recording.Player'))
    @patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match'))
    async def test_match_war_start_from_stored_button_retains_slot_after_sending_bases(
            self, mock_match, mock_player, mock_is_clan_available, mock_send_bases,
            mock_clear_base_store, _mock_free_clan, _mock_occupy_clan, mock_clan_find,
            _mock_remove_helper_task):
        events = []
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "start_match|0|#CLAN|Slot 1"
        ctx.message.id = 888
        ctx.message.embeds = [ipy.Embed(title="**Bases Stored**")]
        ctx.message.components = [ipy.ActionRow(ipy.Button(
            style=ipy.ButtonStyle.DANGER, label="Undo", custom_id="undo_base_storage|TOKEN|0",
        ))]
        ctx.message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("slot_reserved"))
        self.bot.fetch_channel = AsyncMock(return_value=ctx.channel)
        self.bot.cache.fetch_message = AsyncMock(return_value=ctx.message)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 12345
        ctx.channel.name = "🟡 Match Thread"
        ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[MagicMock(embeds=[MagicMock(fields=[])])])
        start_message = MagicMock(id=777)
        start_message.pin = AsyncMock()
        start_message.edit = AsyncMock(side_effect=lambda **_kwargs: events.append("activate"))

        async def record_pending(*_args, **kwargs):
            self.assertEqual(kwargs["components"], [])
            self.assertNotEqual(kwargs["embed"].title, "**Match Started**")
            events.append("pending")
            return start_message

        ctx.channel.send = AsyncMock(side_effect=record_pending)

        host = MagicMock(spec=ipy.User)
        host.id = 999
        host.mention = "<@999>"

        opponent_bases = [MagicMock(link="http://base1"), MagicMock(link="http://base2"), MagicMock(link="http://base3")]
        match = MagicMock()
        match.token = "TOKEN"
        match.thread = "12345"
        match.is_war = False
        match.times = [None, None]
        match.hosts = [None, None]
        match.host_clans = [None, None]
        match.persistent_host_clans = [None, None]
        match.model_fields_set = {"persistent_host_clans"}
        match.old_msgs = [None, None]
        match.final_districts = ["D1", "D2", "D3"]
        match.participants = [
            MagicMock(
                user_id="123",
                base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=None)
            ),
            MagicMock(user_id="456", bases=opponent_bases, base_store=None),
        ]
        match.save_changes = AsyncMock()
        match.set = AsyncMock()
        match.get_saved_state.return_value = None
        self.enable_base_store_message_binding(match)
        collection = match.get_pymongo_collection.return_value

        async def record_binding(query, update):
            if "times.0" in update["$set"]:
                self.assertIsNone(query["times.0"])
                self.assertEqual(update["$set"]["hosts.0"], "999")
                self.assertEqual(update["$set"]["host_clans.0"], "#CLAN")
                return MagicMock(matched_count=1)
            if "message_publications" in update["$set"]:
                self.assertEqual(query["message_publications"], {"$exists": False})
                self.assertEqual(update["$set"]["message_publications"], [None, None])
                return MagicMock(matched_count=1)
            self.assertEqual(query["hosts.0"], "999")
            if set(update["$set"]) == {"message_publications.0"}:
                self.assertTrue(query["message_publications.0"]["pending"])
                self.assertFalse(update["$set"]["message_publications.0"].get("pending", False))
                self.assertEqual(query["old_msgs.0"], 777)
                return MagicMock(matched_count=1)
            self.assertEqual(query["host_clans.0"], "#CLAN")
            self.assertEqual(set(update["$set"]), {"old_msgs.0", "message_publications.0"})
            self.assertEqual(update["$set"]["old_msgs.0"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["message_id"], 777)
            self.assertEqual(update["$set"]["message_publications.0"]["embeds"][0]["title"], "**Match Started**")
            events.append("bind")
            return MagicMock(matched_count=1)

        collection.update_one = AsyncMock(side_effect=record_binding)
        collection.find_one = AsyncMock(return_value={"_id": match.id})
        _mock_occupy_clan.side_effect = lambda *_args: events.append("occupy")
        mock_send_bases.side_effect = lambda *_args, **_kwargs: events.append("send_bases")

        mock_match.find_one = AsyncMock(return_value=match)

        mock_is_clan_available.return_value = True
        mock_clan_find.return_value = MagicMock(availability_str="Available")
        mock_player.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
        clan_obj = MagicMock()
        clan_obj.name = "Test Clan"
        clan_obj.tag = "#CLAN"
        clan_obj.share_link = "http://clan"
        player_user = MagicMock(mention="<@123>")
        player_acc = MagicMock(tag="#PLAYER", share_link="http://player")

        with (
                patch("elo_bot.extensions.match.MatchCommands.coc_client", new_callable=PropertyMock) as mock_coc_client,
                patch(
                    'elo_bot.extensions.match.hosting.get_start_base_store_slot_availability',
                    new_callable=AsyncMock,
                    return_value=(["Slot 1"], {"Slot 2": ("987", "789")}),
                ) as mock_start_slots,
        ):
            mock_coc_client.return_value.get_clan = AsyncMock(return_value=clan_obj)
            mock_coc_client.return_value.get_player = AsyncMock(return_value=player_acc)
            self.bot.fetch_user = AsyncMock(return_value=player_user)

            await self.cog.start_match_button.callback.func(self.cog, ctx)

        self.assertEqual(events, ["occupy", "pending", "bind", "activate", "send_bases", "slot_reserved"])
        self.assertEqual(start_message.edit.await_args.kwargs["embed"]["title"], "**Match Started**")
        self.assertTrue(start_message.edit.await_args.kwargs["components"])
        completion_writes = [item for item in collection.update_one.await_args_list
                             if set(item.args[1]["$set"]) == {"message_publications.0"}]
        self.assertEqual(len(completion_writes), 1)
        self.assertEqual(completion_writes[0].args[0]["old_msgs.0"], 777)
        self.assertEqual(match.old_msgs[0], 777)
        mock_send_bases.assert_awaited_once_with(ANY, "TOKEN", 0, 1, scout=False)
        self.assertEqual(match.attach_base_store_message.await_args_list,
                         [call(0, 888, ready=True), call(0, 888, ready=False)])
        self.assertEqual(match.participants[0].base_store.message_id, 888)
        self.assertFalse(match.participants[0].base_store.ready)
        self.assertEqual(ctx.message.edit.await_args.kwargs["embeds"][0]["title"], "**Bases Stored**")
        mock_clear_base_store.assert_not_awaited()
        self.assertEqual(match.host_clans[0], "#CLAN")
        self.assertEqual(match.persistent_host_clans[0], "#CLAN")
        ctx.send.assert_called()
        self.assertIn("stored in **slot 1** in **Test Clan**", ctx.send.call_args[0][0])
        self.assertNotIn("non-occupied base slot", ctx.send.call_args[0][0])
        mock_start_slots.assert_awaited_once_with("#CLAN", "12345", bot=self.bot)

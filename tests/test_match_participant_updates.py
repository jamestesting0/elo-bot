import unittest
from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue

from elobot.extensions.match import MatchCommands
from elobot.db import BaseLink, BaseStore, Match, Participant
from tests._patch_sites import patch_sites


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3ASUBMITTED"


class TestMatchParticipantUpdates(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(),
            token="TOKEN",
            matched_time=1,
            thread="123",
            final_districts=["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"],
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        )
        self.match._save_state()
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=3_000_000_000)

    async def _save_updates(self, match: Match) -> Mapping[str, JsonValue]:
        database_set = AsyncMock(return_value=match)
        with patch.object(Match, "set", new=database_set):
            await match.save_changes()
        database_set.assert_awaited_once()
        return database_set.await_args.args[0]

    async def test_base_submission_preserves_storage_created_during_validation(self) -> None:
        for modal in (False, True):
            with self.subTest(modal=modal):
                # Given: submission has loaded a match before the opponent reserves a slot.
                snapshot = self.match.model_copy(deep=True)
                latest = self.match.model_copy(deep=True)
                message = MagicMock(embeds=[ipy.Embed()], components=[ipy.ActionRow(
                    ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN"),
                )])
                message.author.id = 999
                message.edit = AsyncMock()
                ctx = MagicMock(spec=ipy.ModalContext if modal else ipy.SlashContext)
                ctx.author.id = 111
                ctx.author.mention = "<@111>"
                ctx.channel.id = 123
                ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[message])
                ctx.custom_id = "submit_bases_modal|TOKEN"
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                bot = MagicMock()
                bot.user.id = 999
                bot.owner.id = 999999
                bot.fetch_channel = AsyncMock(return_value=ctx.channel)
                cog = MatchCommands(bot)

                def reserve_during_validation(_link: MagicMock, _quality: MagicMock) -> None:
                    latest.participants[1].base_store = self.store
                    latest.participants[1].opponent_bases_stored = True

                database_set = AsyncMock(return_value=snapshot)
                with (
                    patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                    patch_sites(('elobot.extensions.match.bases.BaseLayout', 'elobot.features.matches.attack_bases.BaseLayout', 'elobot.features.results.recording.BaseLayout')) as base_model,
                    patch.object(Match, "set", new=database_set),
                ):
                    match_model.find_one = AsyncMock(return_value=snapshot)
                    base_model.find_one = AsyncMock(side_effect=reserve_during_validation)
                    # When: the real callback saves after the concurrent reservation.
                    if modal:
                        await cog.submit_bases_modal.callback.func(
                            cog, ctx, **{"Barbarian Camp": BASE_LINK, "Balloon Lagoon": "", "Dragon Cliffs": ""},
                        )
                    else:
                        await cog.match_base_submit.callback.func(cog, ctx, base_link=BASE_LINK)

                # Then: the Mongo update cannot overwrite either participant's storage fields.
                self.assertIsNotNone(latest.participants[1].base_store)
                database_set.assert_awaited_once()
                updates = database_set.await_args.args[0]
                self.assertEqual(set(updates), {"participants.0.bases.0"})
                self.assertEqual(updates["participants.0.bases.0"]["link"], BASE_LINK)
                self.assertIn("submitted", ctx.send.await_args.args[0])

    async def test_stale_base_edit_does_not_restore_a_cleared_reservation(self) -> None:
        # Given: this snapshot still contains a reservation that another action has cleared.
        self.match.participants[1].base_store = self.store
        self.match.participants[1].opponent_bases_stored = True
        self.match._save_state()

        # When: the stale snapshot saves only a base edit.
        self.match.participants[1].bases[0] = BaseLink(link=BASE_LINK, image="image")
        updates = await self._save_updates(self.match)

        # Then: the old reservation and stored flag are not written back.
        self.assertEqual(set(updates), {"participants.1.bases.0"})

    async def test_reserving_a_slot_does_not_overwrite_concurrent_base_edits(self) -> None:
        # Given: bases may change elsewhere after storage loads the match.
        self.match.participants[1].base_store = self.store
        self.match.participants[1].opponent_bases_stored = True

        # When: storage persists its reservation.
        updates = await self._save_updates(self.match)

        # Then: only the reservation fields are written.
        self.assertEqual(updates, {
            "participants.1.base_store": self.store.model_dump(),
            "participants.1.opponent_bases_stored": True,
        })

    async def test_clearing_one_side_preserves_the_other_sides_reservation(self) -> None:
        # Given: both participants have reservations in the loaded snapshot.
        for participant in self.match.participants:
            participant.base_store = self.store.model_copy()
            participant.opponent_bases_stored = True
        self.match._save_state()

        # When: one side is undone while the other side can change independently.
        self.match.participants[0].base_store = None
        self.match.participants[0].opponent_bases_stored = False
        updates = await self._save_updates(self.match)

        # Then: neither the other participant nor base-link/result fields are overwritten.
        self.assertEqual(updates, {
            "participants.0.base_store": None,
            "participants.0.opponent_bases_stored": False,
        })

    async def test_reservation_replacement_remains_one_complete_value(self) -> None:
        # Given: an existing reservation changes clan and slot together.
        self.match.participants[0].base_store = self.store
        self.match._save_state()
        replacement = BaseStore(clan_tag="#OTHER", slot="Slot 2", stored_at=3_000_000_100)

        # When: the reservation is replaced.
        self.match.participants[0].base_store = replacement
        updates = await self._save_updates(self.match)

        # Then: the complete reservation is written without partial nested fields.
        self.assertEqual(updates, {"participants.0.base_store": replacement.model_dump()})

    async def test_match_and_participant_slots_are_saved_independently(self) -> None:
        # Given: a participant result and the match timestamp both change.
        self.match.participants[0].result[0] = "3.100"
        self.match.times[0] = 100

        # When: those changes are persisted together.
        updates = await self._save_updates(self.match)

        # Then: neither the result nor timestamp can overwrite an unmodified sibling slot.
        self.assertEqual(updates, {
            "participants.0.result.0": "3.100",
            "times.0": 100,
        })

    async def test_participant_replacement_reordering_and_resize_are_preserved(self) -> None:
        for participants in (
            [Participant(user_id="333"), Participant(user_id="222")],
            list(reversed(self.match.participants)),
            self.match.participants[:1],
            [],
        ):
            with self.subTest(user_ids=[participant.user_id for participant in participants]):
                # Given: a structural participant change, rather than a field edit.
                snapshot = self.match.model_copy(deep=True)
                snapshot.participants = participants

                # When: the participant structure is saved.
                updates = await self._save_updates(snapshot)

                # Then: whole-list replacement keeps the existing structural behavior.
                self.assertEqual(updates, {
                    "participants": [participant.model_dump() for participant in participants],
                })

    async def test_unchanged_match_does_not_issue_an_update(self) -> None:
        # Given: no fields have changed since loading.
        database_set = AsyncMock()

        # When: callers save the unchanged match.
        with patch.object(Match, "set", new=database_set):
            await self.match.save_changes()

        # Then: state management retains its no-op behavior.
        database_set.assert_not_awaited()

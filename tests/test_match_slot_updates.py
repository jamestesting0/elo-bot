import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue

from elobot.extensions.match import MatchCommands
from elobot.db import BaseLink, Match, Participant
from tests._patch_sites import patch_sites


class TestMatchSlotUpdates(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Match, "_document_settings", DocumentSettings(
            name="matches", use_state_management=True,
        ))
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1,
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        )
        self.match._save_state()

    async def _updates(self, match: Match) -> dict[str, JsonValue]:
        database = AsyncMock(return_value=match)
        with patch.object(Match, "set", new=database):
            await match.save_changes()
        database.assert_awaited_once()
        return database.await_args.args[0]

    async def test_district_selection_preserves_another_successful_slot_selection(self) -> None:
        # Given: two controls for the same participant each load before either write completes.
        first_waiting = anyio.Event()
        second_saved = anyio.Event()
        persisted = self.match.model_dump(mode="json")
        writes = []
        contexts = []
        for index, district in enumerate(("Barbarian Camp", "Balloon Lagoon")):
            ctx = MagicMock()
            ctx.custom_id = f"district_select|TOKEN|{index}"
            ctx.values = [district]
            ctx.author.id = 111
            ctx.channel.id = 222
            ctx.send = AsyncMock()
            contexts.append(ctx)
        cog = MatchCommands(MagicMock())

        async def persist(match: Match, updates: dict[str, JsonValue], **_kwargs) -> Match:
            writes.append(updates)
            if len(writes) == 1:
                first_waiting.set()
                await second_saved.wait()
            for path, value in updates.items():
                parts = path.split(".")
                districts = persisted["participants"][int(parts[1])]
                if len(parts) == 3:
                    districts[parts[2]] = deepcopy(value)
                else:
                    districts[parts[2]][int(parts[3])] = deepcopy(value)
            return match

        async def first_selection() -> None:
            await cog.district_select.callback.func(cog, contexts[0])

        async def second_selection() -> None:
            await first_waiting.wait()
            await cog.district_select.callback.func(cog, contexts[1])
            second_saved.set()

        # When: the second save finishes before the first save reaches Mongo.
        with (
            patch_sites(('elobot.extensions.match.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
            patch.object(Match, "set", new=persist),
        ):
            match_model.find_one = AsyncMock(side_effect=lambda *_args: self.match.model_copy(deep=True))
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(first_selection)
                    tasks.start_soon(second_selection)

        # Then: both acknowledged selections remain persisted.
        self.assertEqual(persisted["participants"][0]["districts"], ["Barbarian Camp", "Balloon Lagoon", None])
        for ctx in contexts:
            ctx.send.assert_awaited_once()

    async def test_base_slot_update_keeps_the_complete_base_link_atomic(self) -> None:
        # Given: replacing one base changes its link, image and expiration together.
        self.match.participants[0].bases[1] = BaseLink(link="old", image="old", expiration_date=1)
        self.match._save_state()
        replacement = BaseLink(link="new", image="new", expiration_date=2)

        # When: only the second base is replaced.
        self.match.participants[0].bases[1] = replacement
        updates = await self._updates(self.match)

        # Then: sibling bases are omitted and no partial BaseLink fields are emitted.
        self.assertEqual(updates, {"participants.0.bases.1": replacement.model_dump()})

    async def test_result_update_omits_other_result_slots(self) -> None:
        # Given: a stale result snapshot can coexist with another district's new result.
        self.match.participants[1].result[2] = "3.100"

        # When: the selected district result is saved.
        updates = await self._updates(self.match)

        # Then: only that district is written.
        self.assertEqual(updates, {"participants.1.result.2": "3.100"})

    async def test_match_slot_updates_omit_unmodified_siblings(self) -> None:
        for field in (
                "submitters", "hosts", "host_clans", "persistent_host_clans", "times", "old_msgs", "final_districts",
        ):
            with self.subTest(field=field):
                # Given: one fixed slot changes on a stale match snapshot.
                match = self.match.model_copy(deep=True)
                value = 100 if field in {"times", "old_msgs"} else "value"
                getattr(match, field)[0] = value

                # When: state management persists this change.
                updates = await self._updates(match)

                # Then: another side's value cannot be replaced by the stale sibling.
                self.assertEqual(updates, {f"{field}.0": value})

    async def test_clearing_one_base_slot_does_not_restore_another_slot(self) -> None:
        # Given: two base slots exist in the saved snapshot.
        self.match.participants[0].bases = [BaseLink(link="a", image="a"), BaseLink(link="b", image="b"), None]
        self.match._save_state()

        # When: one slot is removed while the sibling may change elsewhere.
        self.match.participants[0].bases[0] = None
        updates = await self._updates(self.match)

        # Then: only the removal is persisted.
        self.assertEqual(updates, {"participants.0.bases.0": None})

    async def test_structural_list_replacement_keeps_its_existing_write_shape(self) -> None:
        # Given: legacy structural changes can resize a slot list or replace participants.
        resized = self.match.model_copy(deep=True)
        resized.times = [100]
        replaced = self.match.model_copy(deep=True)
        replaced.participants = list(reversed(replaced.participants))

        # When: structural changes are saved.
        resized_updates = await self._updates(resized)
        replaced_updates = await self._updates(replaced)

        # Then: whole-list writes preserve the original structural semantics.
        self.assertEqual(resized_updates, {"times": [100]})
        self.assertEqual(replaced_updates, {
            "participants": [participant.model_dump() for participant in replaced.participants],
        })

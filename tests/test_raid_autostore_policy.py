import unittest

from cogs.base_store.raid_policy import eligible_for_raid_storage, select_raid_storage_slot
from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elobot.db import BaseLink, BaseStore


from tests._raid_autostore_fixture import make_match

class TestRaidAutostorePolicy(unittest.TestCase):
    def test_unhosted_thread_with_declined_submission_is_eligible(self) -> None:
        match = make_match()

        eligible = [eligible_for_raid_storage(match, player) for player in (0, 1)]

        self.assertEqual(eligible, [True, True])

    def test_started_war_side_excludes_only_that_side(self) -> None:
        for field, value in (("times", 100), ("hosts", "999")):
            for side in (0, 1):
                with self.subTest(field=field, side=side):
                    match = make_match()
                    getattr(match, field)[side] = value

                    eligible = [eligible_for_raid_storage(match, player) for player in (0, 1)]

                    self.assertEqual(eligible, [player != side for player in (0, 1)])

    def test_opponent_partial_result_does_not_exclude_unstarted_side(self) -> None:
        match = make_match()
        match.times[1], match.hosts[1] = 100, "999"
        match.participants[1].result = ["3.100", None, None]

        eligible = [eligible_for_raid_storage(match, player) for player in (0, 1)]

        self.assertEqual(eligible, [True, False])

    def test_submission_must_be_declined_or_complete(self) -> None:
        bases = [BaseLink(link=f"base-{index}", image="image") for index in range(3)]
        for submission, selected, expected in (
                (None, bases, False), (False, [None] * 3, True),
                (True, [None] * 3, False), (True, bases[:2] + [None], False), (True, bases, True),
        ):
            with self.subTest(submission=submission, selected=selected):
                match = make_match()
                match.participants[1].submit_bases = submission
                match.participants[1].bases = selected

                eligible = eligible_for_raid_storage(match, 0)

                self.assertEqual(eligible, expected)

    def test_stored_or_finished_players_are_excluded(self) -> None:
        for field, value in (
                ("opponent_bases_stored", True),
                ("base_store", BaseStore(clan_tag="#HELD", slot="Slot 1")),
                ("result", ["3.100"] * 3),
        ):
            with self.subTest(field=field):
                match = make_match()
                setattr(match.participants[0], field, value)

                eligible = eligible_for_raid_storage(match, 0)

                self.assertFalse(eligible)

    def test_missing_thread_terminal_and_live_matches_are_excluded(self) -> None:
        for field, value in (
                ("thread", None), ("is_autowin", True), ("recorded_match_id", "record"), ("is_war", False),
        ):
            with self.subTest(field=field):
                match = make_match()
                setattr(match, field, value)

                eligible = eligible_for_raid_storage(match, 0)

                self.assertFalse(eligible)

    def test_opponents_are_placed_in_different_clans(self) -> None:
        match = make_match()
        first, second = [str(choice.value) for choice in HOST_CLAN_CHOICES[:2]]
        match.participants[1].base_store = BaseStore(clan_tag=first, slot="Slot 1")
        slots = {first: ["Slot 2"], second: list(BASE_STORE_SLOTS)}

        selected = select_raid_storage_slot(match, 0, slots)

        self.assertEqual(selected, (second, "Slot 1"))

    def test_reserve_requires_two_different_configured_clans(self) -> None:
        match = make_match()
        first, second = [str(choice.value) for choice in HOST_CLAN_CHOICES[:2]]
        for slots in (
                {first: list(BASE_STORE_SLOTS)},
                {first: ["Slot 1"], second: ["Slot 2"]},
                {first: list(BASE_STORE_SLOTS), "#UNCONFIGURED": list(BASE_STORE_SLOTS)},
        ):
            with self.subTest(slots=slots):
                selected = select_raid_storage_slot(match, 0, slots)

                self.assertIsNone(selected)

    def test_storage_fills_capacity_without_consuming_the_reserve(self) -> None:
        first, second, third = [str(choice.value) for choice in HOST_CLAN_CHOICES[:3]]
        slots = {tag: list(BASE_STORE_SLOTS) for tag in (first, second, third)}
        placements = []

        while selected := select_raid_storage_slot(make_match(), 0, slots):
            placements.append(selected)
            slots[selected[0]].remove(selected[1])

        self.assertEqual(len(placements), 4)
        self.assertEqual(sum(map(len, slots.values())), 2)
        self.assertEqual(sum(map(bool, slots.values())), 2)

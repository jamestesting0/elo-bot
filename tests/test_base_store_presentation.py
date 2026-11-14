import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from elo_bot.features.base_store.models import BaseStorePresentationRequest
from elo_bot.features.base_store.presentation import (
    build_helper_store_components,
    build_manual_store_components,
    build_store_embed,
    construct_occupied_slots_error,
    format_opponent_bases,
)
from elo_bot.db import BaseLink, Participant


class TestBaseStorePresentation(unittest.TestCase):
    @staticmethod
    def make_request() -> BaseStorePresentationRequest:
        opponent = Participant(
            user_id="200",
            bases=[BaseLink(link="https://base.example/one", image="https://image.example/one"), None, None],
        )
        match = MagicMock(
            token="TOKEN",
            participants=[Participant(user_id="100"), opponent],
            final_districts=["Barbarian Camp", None, None],
        )
        clan = SimpleNamespace(tag="#CLAN", name="Test Clan", share_link="https://clan.example")
        return BaseStorePresentationRequest(match=match, player=0, helper_id="300", clan=clan)

    def test_manual_and_helper_controls_keep_isolated_ids(self) -> None:
        request = self.make_request()

        manual = build_manual_store_components(request, "Slot 2")
        helper = build_helper_store_components(request)

        self.assertEqual(
            [button.custom_id for button in manual],
            ["start_match|0|#CLAN|Slot 2", "undo_base_storage|TOKEN|0"],
        )
        self.assertEqual(
            [button.custom_id for button in helper],
            ["helper_stored_start|TOKEN|0", "helper_stored_undo|TOKEN|0"],
        )

    def test_store_embed_and_opponent_summary_match_command_contract(self) -> None:
        request = self.make_request()

        names = {"100": "**[Player](<https://discordapp.com/users/100>)**",
                 "300": "**[Helper](<https://discordapp.com/users/300>)**"}
        embed = build_store_embed(request, names)

        self.assertEqual(embed.title, "**Bases Stored**")
        self.assertFalse(embed.description)
        self.assertEqual([field.value for field in embed.fields[:2]], [names["100"], names["300"]])
        self.assertEqual(format_opponent_bases(request), "[Barbarian Camp](https://base.example/one)")

        reserved = build_store_embed(BaseStorePresentationRequest(
            match=request.match, player=0, helper_id="300", clan=request.clan, bases_stored=False,
        ), names)
        self.assertEqual(reserved.title, "**Base Slot Reserved**")
        self.assertFalse(reserved.description)

        incomplete = build_store_embed(BaseStorePresentationRequest(
            match=request.match, player=0, helper_id="300", clan=request.clan,
            failure_reason="Copying did not complete.",
        ), names)
        self.assertEqual(incomplete.title, "**Base Storage Incomplete**")
        self.assertFalse(incomplete.description)


    def test_occupied_slots_error_includes_both_owners(self) -> None:
        message = construct_occupied_slots_error(
            "#CLAN",
            "Test Clan",
            {"Slot 1": ("10", "100"), "Slot 2": ("20", "200")},
        )

        self.assertIn("Slot 1: <#10> <@100>", message)
        self.assertIn("Slot 2: <#20> <@200>", message)


if __name__ == "__main__":
    unittest.main()

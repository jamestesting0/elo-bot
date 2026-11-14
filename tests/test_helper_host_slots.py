import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from elobot.features.hosting.war import helper_host
from tests._patch_sites import patch_sites


class TestHelperHostSlotSelection(unittest.IsolatedAsyncioTestCase):
    async def test_selects_first_available_slot_or_none(self) -> None:
        from elobot.features.hosting.war.helper_slot import get_available_helper_slot

        for available, expected in ((["Slot 1", "Slot 2"], 1), (["Slot 2"], 2), ([], None)):
            with self.subTest(available=available), patch(
                    "elobot.features.hosting.war.helper_slot.base_store_storage.sync_base_store_slots",
                    new=AsyncMock(return_value={"#CLAN": available}),
            ) as sync_slots:
                slot = await get_available_helper_slot("#CLAN")
                self.assertEqual(slot, expected)
                sync_slots.assert_awaited_once_with("#CLAN", bot=None)







class TestAutomaticWarHostSlots(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def request() -> helper_host.AutomaticWarHost:
        match = MagicMock(
            token="TOKEN",
            participants=[
                MagicMock(user_id="100"),
                MagicMock(user_id="200", bases=[MagicMock(link="https://base.example")]),
            ],
            host_clans=[None, None],
            times=[None, None],
        )
        return helper_host.AutomaticWarHost(
            match=match,
            player=0,
            clan=MagicMock(tag="#CLAN", name="Helper Clan"),
            host=MagicMock(id=300),
        )

    async def test_no_empty_slot_stops_before_host_side_effects(self) -> None:
        # Given
        runtime = MagicMock()
        ctx = MagicMock()
        ctx.send = AsyncMock()
        with (
                patch.object(helper_host, "get_available_helper_slot", new=AsyncMock(return_value=None)) as slot,
                patch.object(helper_host, "Player") as player_model,
                patch_sites(('elobot.extensions.match.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new_callable=AsyncMock) as choose_bases,
                patch.object(helper_host, "confirm_helper_availability", new_callable=AsyncMock) as confirm,
                patch.object(helper_host.war_start, "start_war", new_callable=AsyncMock) as start,
                patch.object(helper_host, "run_helper_host_job", new_callable=AsyncMock) as run_job,
        ):
            # When
            await helper_host.host_war_automatically(runtime, ctx, self.request())

        # Then
        slot.assert_awaited_once_with("#CLAN", bot=runtime.bot)
        player_model.find_one.assert_not_called()
        choose_bases.assert_not_awaited()
        confirm.assert_not_awaited()
        start.assert_not_awaited()
        run_job.assert_not_awaited()
        ctx.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

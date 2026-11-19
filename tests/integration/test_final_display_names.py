from types import SimpleNamespace

from elo_bot.features.finals.lifecycle import (
    add_entrant, final_for_view, refresh_display_names, remove_entrant, set_nickname,
)
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalDisplayNames(FinalScenarioCase):
    async def test_entrants_follow_display_names_unless_a_manager_set_their_nickname(self):
        final = await self.roster(3)
        await set_nickname(final, "2", "Captain")
        final = await add_entrant(await final_for_view(), "4", "Chosen", override=True)
        self.assertEqual(final.nickname_overrides, ["2", "4"])
        members = {user: SimpleNamespace(display_name=f"Display {user}") for user in ("1", "2", "4")}
        members["3"] = None
        final = await refresh_display_names(final, members)
        self.assertEqual(final.player_nicknames,
                         {"1": "Display 1", "2": "Captain", "3": "Player 3", "4": "Chosen"})
        self.assertIs(await refresh_display_names(final, members), final)
        await remove_entrant(final, "4")
        current = await final_for_view()
        self.assertEqual(current.nickname_overrides, ["2"])
        self.assertNotIn("4", current.player_nicknames)

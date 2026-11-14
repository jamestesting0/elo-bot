import unittest
from collections import Counter
from unittest.mock import patch

from elo_bot.extensions import match as match_commands
import elo_bot.features.matches.tips as tips_module
import elo_bot.extensions.match.results as match_results_module


class TestTipRotation(unittest.IsolatedAsyncioTestCase):
    async def test_each_item_is_shown_once_per_cycle_with_unequal_lengths(self):
        contents = {
            "tips.json": ["tip one", "tip two", "tip three"],
            "facts.json": ["fact one", "fact two"],
        }
        last_index = -1

        async def read_content(filename, _directory):
            return contents[filename]

        async def advance(count):
            nonlocal last_index
            last_index = (last_index + 1) % count
            return last_index

        with patch('elo_bot.features.matches.tips.read_json', side_effect=read_content), \
                patch.object(match_commands.ServerState, "advance_tip_index", side_effect=advance):
            messages = [await match_results_module.choose_tip() for _ in range(10)]

        expected = [f'💡 **TIP:** "{tip}"' for tip in contents["tips.json"]]
        expected += [f'💡 **ELO FACT:** "{fact}"' for fact in contents["facts.json"]]
        self.assertEqual(Counter(messages[:5]), Counter(expected))
        self.assertEqual(Counter(messages[5:]), Counter(expected))

    async def test_real_content_is_selected_by_the_shared_index(self):
        tips = await tips_module.read_json("tips.json", "assets/")
        facts = await tips_module.read_json("facts.json", "assets/")
        with patch.object(match_commands.ServerState, "advance_tip_index", side_effect=[0, len(tips) + len(facts) - 1]):
            tip = await match_results_module.choose_tip()
            fact = await match_results_module.choose_tip()

        self.assertTrue(tip.startswith('💡 **TIP:** "'))
        self.assertTrue(fact.startswith('💡 **ELO FACT:** "'))
        self.assertNotEqual(tip.split('"', 1)[1], fact.split('"', 1)[1])

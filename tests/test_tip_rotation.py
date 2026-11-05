import unittest
from unittest.mock import patch

from cogs import match_commands


class TestTipRotation(unittest.IsolatedAsyncioTestCase):
    async def test_alternates_and_visits_every_item_with_unequal_lengths(self):
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

        with patch.object(match_commands, "read_json", side_effect=read_content), \
                patch.object(match_commands.ServerState, "advance_tip_index", side_effect=advance):
            messages = [await match_commands.choose_tip() for _ in range(14)]

        self.assertEqual(messages, [
            '💡 **TIP:** "tip one"', '💡 **ELO FACT:** "fact one"',
            '💡 **TIP:** "tip two"', '💡 **ELO FACT:** "fact two"',
            '💡 **TIP:** "tip three"', '💡 **ELO FACT:** "fact one"',
            '💡 **TIP:** "tip one"', '💡 **ELO FACT:** "fact two"',
            '💡 **TIP:** "tip two"', '💡 **ELO FACT:** "fact one"',
            '💡 **TIP:** "tip three"', '💡 **ELO FACT:** "fact two"',
            '💡 **TIP:** "tip one"', '💡 **ELO FACT:** "fact one"',
        ])

    async def test_real_content_is_selected_by_the_shared_index(self):
        with patch.object(match_commands.ServerState, "advance_tip_index", side_effect=[0, 1]):
            tip = await match_commands.choose_tip()
            fact = await match_commands.choose_tip()

        self.assertTrue(tip.startswith('💡 **TIP:** "'))
        self.assertTrue(fact.startswith('💡 **ELO FACT:** "'))
        self.assertNotEqual(tip.split('"', 1)[1], fact.split('"', 1)[1])

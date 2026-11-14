import unittest

import interactions as ipy

from elobot.discord.pagination import (
    InvalidPageError,
    build_page_button,
    build_page_jump_modal,
    parse_page_number,
)


class TestPagination(unittest.TestCase):
    def test_button_and_modal_payloads(self):
        # Given: a page family with a visible current/total value
        custom_id = "helper_lb_page"

        # When: its shared button and modal are constructed
        button = build_page_button(2, 9, custom_id)
        modal = build_page_jump_modal("helper_lb_page_modal")

        # Then: the button is enabled and the modal collects one required page value
        self.assertEqual(button.style, ipy.ButtonStyle.SECONDARY)
        self.assertEqual(button.label, "2/9")
        self.assertEqual(button.custom_id, custom_id)
        self.assertFalse(button.disabled)
        self.assertEqual(modal.custom_id, "helper_lb_page_modal")
        self.assertEqual(len(modal.components), 1)
        self.assertEqual(modal.components[0].custom_id, "page")
        self.assertTrue(modal.components[0].required)

    def test_valid_page_boundaries(self):
        # Given: a current nine-page result set
        total_pages = 9

        # When/Then: the first and final visible page values are parsed unchanged
        self.assertEqual(parse_page_number(" 1 ", total_pages), 1)
        self.assertEqual(parse_page_number("9", total_pages), 9)

    def test_invalid_page_values(self):
        # Given: malformed, non-positive, and stale page submissions
        cases = {
            "": "Page must be a positive integer.",
            "   ": "Page must be a positive integer.",
            "abc": "Page must be a positive integer.",
            "1.5": "Page must be a positive integer.",
            "+1": "Page must be a positive integer.",
            "0": "Page must be a positive integer.",
            "-1": "Page must be a positive integer.",
            "10": "Page must be between 1 and 9.",
        }

        # When/Then: each value is rejected with its boundary-specific reason
        for value, expected_message in cases.items():
            with self.subTest(value=value), self.assertRaisesRegex(InvalidPageError, expected_message):
                parse_page_number(value, 9)


if __name__ == "__main__":
    unittest.main()

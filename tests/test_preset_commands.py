import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions.preset import PresetCommands
from elobot.config.emojis import ERROR_EMOJI


class TestPresetBaseView(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = PresetCommands(self.bot)

    def test_district_add_exposes_optional_secondary_fallbacks(self) -> None:
        # Given: the registered /preset district add command
        options = {
            str(option.name): option
            for option in self.cog.preset_district_add.options
        }
        self.assertEqual(
            list(options),
            ["district1", "district2", "district3", "secondary_district1", "secondary_district_3"],
        )

        # When: its secondary fallback options are inspected
        secondary_district1 = options["secondary_district1"]
        secondary_district_3 = options["secondary_district_3"]

        # Then: both are optional and restricted to unconditional districts in their slot
        self.assertFalse(secondary_district1.required)
        self.assertEqual(
            [choice.value for choice in secondary_district1.choices],
            ["Barbarian Camp", "Skeleton Park"],
        )
        self.assertFalse(secondary_district_3.required)
        self.assertEqual(
            [choice.value for choice in secondary_district_3.choices],
            ["Dragon Cliffs", "Goblin Mines"],
        )

    @patch("elobot.extensions.preset.Player")
    async def test_district_add_persists_secondary_fallbacks(self, player_model) -> None:
        # Given: a registered player and both optional fallback choices
        player = MagicMock(
            district_presets=[None, None, None],
            secondary_district_presets=[None, None, None],
            save_changes=AsyncMock(),
        )
        player_model.find_one = AsyncMock(return_value=player)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild.id = 456
        ctx.send = AsyncMock()

        # When: the player stores primary and secondary district presets
        await self.cog.preset_district_add.callback(
            ctx,
            "Wizard Valley",
            "Balloon Lagoon",
            "Capital Peak",
            "Skeleton Park",
            "Goblin Mines",
        )

        # Then: both fixed-slot lists are persisted in one save
        self.assertEqual(
            player.district_presets,
            ["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
        )
        self.assertEqual(
            player.secondary_district_presets,
            ["Skeleton Park", None, "Goblin Mines"],
        )
        player.save_changes.assert_awaited_once_with()

    @patch("elobot.extensions.preset.Player")
    async def test_district_add_omitted_secondaries_replace_existing_with_none(self, player_model) -> None:
        # Given: a registered player with stale secondary fallbacks
        player = MagicMock(
            district_presets=[None, None, None],
            secondary_district_presets=["Skeleton Park", None, "Goblin Mines"],
            save_changes=AsyncMock(),
        )
        player_model.find_one = AsyncMock(return_value=player)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild.id = 456
        ctx.send = AsyncMock()

        # When: the player replaces the preset without either optional field
        await self.cog.preset_district_add.callback(
            ctx,
            "Barbarian Camp",
            "Balloon Lagoon",
            "Dragon Cliffs",
        )

        # Then: omitted values clear both stale secondary slots
        self.assertEqual(player.secondary_district_presets, [None, None, None])
        player.save_changes.assert_awaited_once_with()

    @patch("elobot.extensions.preset.Player")
    async def test_district_remove_clears_secondary_fallbacks(self, player_model) -> None:
        # Given: a player with primary and secondary district presets
        player = MagicMock(
            district_presets=["Wizard Valley", "Balloon Lagoon", "Capital Peak"],
            secondary_district_presets=["Skeleton Park", None, "Goblin Mines"],
            save_changes=AsyncMock(),
        )
        player_model.find_one = AsyncMock(return_value=player)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.send = AsyncMock()

        # When: all district presets are removed
        await self.cog.preset_district_remove.callback(ctx)

        # Then: neither fallback can silently remain active
        self.assertEqual(player.district_presets, [None, None, None])
        self.assertEqual(player.secondary_district_presets, [None, None, None])
        player.save_changes.assert_awaited_once_with()

    @patch("elobot.extensions.preset.Player")
    async def test_view_skips_missing_link_and_sends_remaining_preset(self, player_model) -> None:
        # Given: one removed preset and one linked base remain in the stored mapping
        linked_base = MagicMock(
            expiration_date=1_800_000_000,
            link="https://link.clashofclans.com/example",
            image_url="https://cdn.example/base.webp",
        )
        player = MagicMock(base_presets={"Barbarian Camp": None, "Wizard Valley": linked_base})
        player_model.find_one = AsyncMock(return_value=player)
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild.id = 456
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the player views their base presets
        await self.cog.preset_base_view.callback(ctx)

        # Then: only the linked base is shown
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.send.assert_awaited_once()
        embeds = ctx.send.await_args.kwargs["embeds"]
        self.assertEqual(len(embeds), 1)
        self.assertEqual(embeds[0].title, "**Wizard Valley**")
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    @patch("elobot.extensions.preset.Player")
    async def test_view_reports_no_presets_when_only_missing_links_remain(self, player_model) -> None:
        # Given: every stored preset entry points to a removed base
        player_model.find_one = AsyncMock(
            return_value=MagicMock(base_presets={"Barbarian Camp": None}),
        )
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild.id = 456
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the player views their base presets
        await self.cog.preset_base_view.callback(ctx)

        # Then: the command reports that no presets remain
        message = ctx.send.await_args.args[0]
        self.assertTrue(message.startswith(str(ERROR_EMOJI)))
        self.assertNotIn("embeds", ctx.send.await_args.kwargs)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    @patch("elobot.extensions.preset.datetime")
    @patch("elobot.extensions.preset.Player")
    async def test_view_marks_missing_expiration_as_ninety_days_expired(
            self,
            player_model,
            datetime_type,
    ) -> None:
        # Given: a linked legacy preset has no expiration timestamp
        linked_base = MagicMock(
            expiration_date=None,
            link="https://link.clashofclans.com/example",
            image_url="https://cdn.example/base.webp",
        )
        player_model.find_one = AsyncMock(
            return_value=MagicMock(base_presets={"Wizard Valley": linked_base}),
        )
        datetime_type.now.return_value.timestamp.return_value = 2_000_000_000
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 123
        ctx.guild.id = 456
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        # When: the player views their base presets
        await self.cog.preset_base_view.callback(ctx)

        # Then: Discord receives a relative timestamp exactly 90 days in the past
        embed = ctx.send.await_args.kwargs["embeds"][0]
        self.assertIn("<t:1992224000:R>", embed.description)


if __name__ == "__main__":
    unittest.main()

import sys
import os
sys.path.append(os.getcwd())

import interactions as ipy
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

from cogs.base_commands import BaseAutocompleteRow, BaseCommands
from common.constants import SUCCESS_EMOJI

BASE_SET_LINK_CALLBACK = BaseCommands.base_set_link.callback


class TestBaseCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.owner = MagicMock()
        self.bot.owner.send = AsyncMock()
        self.bot.ext = {}
        self.bot.builder_names = {}
        self.cog = BaseCommands(self.bot)

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.download_base_screenshot")
    @patch("cogs.base_commands.delete_file")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_add_success(self, mock_base_layout, mock_delete_file, mock_download_screenshot, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        mock_download_screenshot.return_value = True
        
        # Mock BaseLayout.find_one to return None (no existing base)
        mock_base_layout.find_one = AsyncMock(return_value=None)
        
        # mock BaseLayout.insert()
        mock_instance = MagicMock()
        mock_instance.insert = AsyncMock()
        mock_base_layout.return_value = mock_instance
        
        # Mock SlashContext
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 12345
        
        # Args
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        base_ss = MagicMock(spec=ipy.Attachment)
        base_ss.url = "httpa://example.com/image.png"
        base_quality = "Excellent"
        
        # Execute
        await self.cog.base_add.callback.func(self.cog, ctx, base_link, base_ss, base_quality)
        
        # Verify
        mock_download_screenshot.assert_called_once()
        self.bot.owner.send.assert_called_once()
        mock_delete_file.assert_called_once()
        mock_instance.insert.assert_called_once()
        
        # Check success message
        expected_msg = (f"{SUCCESS_EMOJI} You have successfully added a base for `Barbarian Camp`. "
                        "Fingerprint extraction pending.")
        ctx.send.assert_awaited_once_with(expected_msg, ephemeral=True)

    @patch("cogs.base_commands.construct_base_embed")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_info_success(self, mock_base_layout, mock_construct_embed):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 12345
        self.bot.owner.id = 99999

        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        
        mock_base = MagicMock()
        mock_base.builder_id = "12345"
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=12345, display_name="Builder"))
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)
        
        mock_embed = MagicMock()
        mock_construct_embed.return_value = mock_embed

        # Execute
        await self.cog.base_info.callback(ctx, base_link)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        mock_construct_embed.assert_called_once_with(
            mock_base, "Performance", names={"12345": "**[Builder](<https://discordapp.com/users/12345>)**"},
        )
        ctx.send.assert_called_with(embed=mock_embed, ephemeral=True)

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    @patch("cogs.base_commands.DISTRICT_CODES_REVERSED", {"3A1": "Barbarian Camp"})
    async def test_base_remove_success(self, mock_base_layout, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        
        mock_base = MagicMock()
        mock_base.delete = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        await self.cog.base_remove.callback(ctx, base_link)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        mock_base.delete.assert_called_once()
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} You have successfully removed a base for `Barbarian Camp`.")

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_set_link_success(self, mock_base_layout, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        old_base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        new_base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3ABBBBB"
        
        mock_base = MagicMock()
        mock_base.district = "Barbarian Camp"
        mock_base.save_changes = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)
        self.cog.send_base_renewal_log = AsyncMock()

        # Execute
        await BASE_SET_LINK_CALLBACK(self.cog, ctx, old_base_link, new_base_link)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        self.assertEqual(mock_base.link, new_base_link)
        mock_base.save_changes.assert_called_once()
        self.cog.send_base_renewal_log.assert_awaited_once_with(mock_base)
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} You have successfully renewed a base for `Barbarian Camp`.")

    @patch("cogs.base_commands.construct_base_embed")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_list_success(self, mock_base_layout, mock_construct_embed):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.has_any_role.return_value = True
        
        mock_query = MagicMock()
        mock_base_layout.find.return_value = mock_query
        mock_query.find.return_value = mock_query
        mock_query.count = AsyncMock(return_value=1)

        mock_base = MagicMock()
        mock_base.builder_id = "12345"
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(id=12345, display_name="Builder"))
        mock_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[mock_base]
        )
        
        mock_embed = MagicMock()
        mock_construct_embed.return_value = mock_embed

        # Execute
        await self.cog.base_list.callback(
            ctx, district="Any", embed_type="Performance", base_quality="Any", builder=None, hidden=True
        )

        # Verify
        mock_base_layout.find.assert_called()
        mock_construct_embed.assert_called_once_with(
            mock_base, "Performance", names={"12345": "**[Builder](<https://discordapp.com/users/12345>)**"},
        )
        ctx.send.assert_called_once()
        
        # Check components (district select, embed-type select, pagination)
        call_args = ctx.send.call_args
        self.assertIn("components", call_args.kwargs)
        components = call_args.kwargs["components"]
        self.assertEqual(len(components), 3)
        self.assertEqual(components[0].components[0].custom_id, "base_list_district_select")
        self.assertEqual(components[1].components[0].custom_id, "base_list_embed_type_select")
        page_number_button = components[2].components[1]
        self.assertEqual(page_number_button.style, ipy.ButtonStyle.SECONDARY)
        self.assertEqual(page_number_button.label, "1/1")
        self.assertEqual(page_number_button.custom_id, "base_page|Any|Performance|Any|Any")
        self.assertFalse(page_number_button.disabled)

    @patch("cogs.base_commands.construct_base_embed")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_list_elobot_filters_unknown_builder(self, mock_base_layout, mock_construct_embed):
        # Given: Elo Bot is selected as the builder filter.
        self.bot.user = MagicMock(id=42)
        builder = MagicMock(spec=ipy.User, id=42, mention="<@42>")
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.has_any_role.return_value = True
        query = MagicMock()
        mock_base_layout.find.return_value = query
        mock_base_layout.builder_id.__eq__.side_effect = lambda value: ("builder_id", value)
        query.find.return_value = query
        query.count = AsyncMock(return_value=1)
        base = MagicMock()
        base.builder_id = None
        query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(return_value=[base])
        mock_construct_embed.return_value = MagicMock()

        # When: the public base list is filtered by Elo Bot.
        await self.cog.base_list.callback.func(
            self.cog,
            ctx,
            district="Any",
            embed_type="Performance",
            base_quality="Any",
            builder=builder,
            hidden=True,
        )

        # Then: only unknown-builder bases are queried while pagination retains the bot selection.
        query.find.assert_called_once_with(("builder_id", None))
        components = ctx.send.await_args.kwargs["components"]
        self.assertEqual(components[2].components[1].custom_id, "base_page|Any|Performance|Any|42")

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_set_expired_success(self, mock_base_layout, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        
        mock_base = MagicMock()
        mock_base.save_changes = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        await self.cog.base_set_expired.callback(ctx, base_link)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        mock_base.save_changes.assert_called_once()
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} This is base is now marked as expired.")

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_set_quality_success(self, mock_base_layout, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        base_quality = "Excellent"
        
        mock_base = MagicMock()
        mock_base.save_changes = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        await self.cog.base_set_quality.callback(ctx, base_link, base_quality)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        self.assertEqual(mock_base.quality, base_quality)
        mock_base.save_changes.assert_called_once()
        ctx.send.assert_called_with(
            f"{SUCCESS_EMOJI} You have successfully set the quality of the base as **{base_quality}**."
        )

    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_set_builder_success(self, mock_base_layout, mock_validate_url):
        # Setup
        mock_validate_url.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        builder = MagicMock(spec=ipy.User)
        builder.id = 12345
        builder.bot = False
        builder.mention = "@builder"
        
        mock_base = MagicMock()
        mock_base.set = AsyncMock()
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        await self.cog.base_set_builder.callback.func(self.cog, ctx, base_link, builder)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        mock_base.set.assert_awaited_once_with({"builder_id": "12345"})
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} You have successfully set {builder.mention} as the builder.")

    @patch("cogs.base_commands.download_base_screenshot")
    @patch("cogs.base_commands.validators.url")
    @patch("cogs.base_commands.BaseLayout")
    async def test_base_set_image_success(self, mock_base_layout, mock_validate_url, mock_download_screenshot):
        # Setup
        mock_validate_url.return_value = True
        mock_download_screenshot.return_value = True
        
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        base_link = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"
        image_link = "https://example.com/image.png"
        uploaded = MagicMock()
        uploaded.attachments = [MagicMock(url="https://cdn.example/durable.webp")]
        self.bot.owner.send.return_value = uploaded
        
        mock_base = MagicMock()
        mock_base.save_changes = AsyncMock()
        mock_base.image_revision = 0
        mock_base.district = "Barbarian Camp"
        collection = MagicMock(update_one=AsyncMock(return_value=MagicMock(matched_count=1)))
        mock_base.get_pymongo_collection.return_value = collection
        mock_base_layout.find_one = AsyncMock(return_value=mock_base)

        # Execute
        await self.cog.base_set_image.callback.func(self.cog, ctx, base_link, image_link)

        # Verify
        mock_base_layout.find_one.assert_called_once()
        self.bot.owner.send.assert_awaited_once()
        self.assertEqual(mock_base.image_url, "https://cdn.example/durable.webp")
        self.assertEqual(mock_base.image_attachment.message_id, str(uploaded.id))
        self.assertEqual(mock_base.image_revision, 1)
        self.assertEqual(mock_base.fingerprint_extraction.status, "pending")
        collection.update_one.assert_awaited_once()
        mock_base.save_changes.assert_not_called()
        ctx.send.assert_called_with(f"{SUCCESS_EMOJI} Screenshot saved. Fingerprint extraction pending.", ephemeral=True)

    @patch("cogs.base_commands.construct_base_leaderboard_string")
    @patch("cogs.base_commands.sort_bases")
    @patch("cogs.base_commands.BaseLayout")
    @patch("cogs.base_commands.LB_ENTRIES_PER_PAGE", 5)
    async def test_base_leaderboard_success(self, mock_base_layout, mock_sort_bases, mock_construct_lb_string):
        # Setup
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        
        mock_bases_sorted = [MagicMock() for _ in range(10)]
        mock_sort_bases.return_value = mock_bases_sorted
        
        mock_construct_lb_string.return_value = "Leaderboard String"

        # Execute
        await self.cog.base_leaderboard.callback(ctx, district="Barbarian Camp", hidden=False)

        # Verify
        mock_base_layout.find.assert_called_once()
        mock_sort_bases.assert_called_once()
        mock_construct_lb_string.assert_called_once()
        ctx.send.assert_called_once()
        
        # Check components
        call_args = ctx.send.call_args
        self.assertIn("components", call_args.kwargs)
        components = call_args.kwargs["components"]
        self.assertEqual(len(components), 3)
        district_values = {
            option.value for option in components[0].components[0].options
        }
        self.assertTrue({"Wizard Valley", "Capital Peak"}.issubset(district_values))
        page_number_button = components[2].components[1]
        self.assertEqual(page_number_button.label, "1/2")
        self.assertEqual(page_number_button.custom_id, "base_lb_page")
        self.assertFalse(page_number_button.disabled)

    def _autocomplete_ctx(self, *, author_id, input_text="", owner_id=99999, member=None):
        self.bot.owner.id = owner_id
        self.bot.fetch_user = AsyncMock(side_effect=AssertionError("autocomplete must not fetch users"))
        self.bot.get_user = MagicMock(return_value=None)
        ctx = MagicMock(spec=ipy.AutocompleteContext)
        ctx.author.id = author_id
        ctx.input_text = input_text
        ctx.send = AsyncMock()
        ctx.guild.get_member.return_value = member
        return ctx

    @staticmethod
    def _mock_autocomplete_query(mock_base_layout, rows):
        mock_base_layout.find_all.return_value.sort.return_value.project.return_value.to_list = AsyncMock(
            return_value=rows,
        )

    async def _run_base_autocomplete(self, ctx):
        await self.cog.base_autocomplete(ctx)

    @patch("cogs.base_commands.BaseLayout")
    async def test_base_autocomplete_uses_cached_builder_and_sends_once(self, mock_base_layout):
        rows = [
            MagicMock(district="Barbarian Camp", builder_id="12345"),
            MagicMock(district="Wizard Valley", builder_id="222"),
        ]
        self._mock_autocomplete_query(mock_base_layout, rows)
        member = MagicMock()
        member.__str__.return_value = "@BuilderOne"
        ctx = self._autocomplete_ctx(author_id=12345, member=member)

        await self._run_base_autocomplete(ctx)

        mock_base_layout.find_all.return_value.sort.assert_called_once_with("_id")
        mock_base_layout.find_all.return_value.sort.return_value.project.assert_called_once_with(BaseAutocompleteRow)
        self.bot.fetch_user.assert_not_called()
        ctx.send.assert_awaited_once_with([
            {"name": "Barbarian Camp by @BuilderOne (1)", "value": 0},
        ])

    @patch("cogs.base_commands.BaseLayout")
    async def test_base_autocomplete_owner_sees_all_bases_without_fetching_users(self, mock_base_layout):
        rows = [
            MagicMock(district="Barbarian Camp", builder_id="111"),
            MagicMock(district="Dragon Cliffs", builder_id=None),
        ]
        self._mock_autocomplete_query(mock_base_layout, rows)
        ctx = self._autocomplete_ctx(author_id=99999, owner_id=99999)
        other_builder = MagicMock()
        other_builder.__str__.return_value = "@OtherBuilder"
        self.bot.get_user.side_effect = lambda user_id: other_builder if str(user_id) == "111" else None

        await self._run_base_autocomplete(ctx)

        self.bot.fetch_user.assert_not_called()
        ctx.send.assert_awaited_once_with([
            {"name": "Barbarian Camp by @OtherBuilder (1)", "value": 0},
            {"name": "Dragon Cliffs by @anonymous (2)", "value": 1},
        ])

    @patch("cogs.base_commands.BaseLayout")
    async def test_base_autocomplete_matches_input_and_keeps_global_index(self, mock_base_layout):
        rows = [
            MagicMock(district="Barbarian Camp", builder_id="111"),
            MagicMock(district="Wizard Valley", builder_id="12345"),
            MagicMock(district="Dragon Cliffs", builder_id="12345"),
        ]
        self._mock_autocomplete_query(mock_base_layout, rows)
        member = MagicMock()
        member.__str__.return_value = "@BuilderOne"
        ctx = self._autocomplete_ctx(author_id=12345, input_text="wizard", member=member)

        await self._run_base_autocomplete(ctx)

        ctx.send.assert_awaited_once_with([
            {"name": "Wizard Valley by @BuilderOne (2)", "value": 1},
        ])

    @patch("cogs.base_commands.BaseLayout")
    async def test_base_autocomplete_uses_builder_name_cache(self, mock_base_layout):
        rows = [MagicMock(district="Barbarian Camp", builder_id="12345")]
        self._mock_autocomplete_query(mock_base_layout, rows)
        self.bot.builder_names = {"12345": "@CachedBuilder"}
        ctx = self._autocomplete_ctx(author_id=12345)
        ctx.guild.get_member.return_value = None
        self.bot.get_user.return_value = None

        await self._run_base_autocomplete(ctx)

        self.bot.fetch_user.assert_not_called()
        ctx.send.assert_awaited_once_with([
            {"name": "Barbarian Camp by @CachedBuilder (1)", "value": 0},
        ])

    @patch("cogs.base_commands.BaseLayout")
    async def test_base_autocomplete_remembers_resolved_builder_names(self, mock_base_layout):
        rows = [MagicMock(district="Barbarian Camp", builder_id="12345")]
        self._mock_autocomplete_query(mock_base_layout, rows)
        member = MagicMock()
        member.tag = "@BuilderOne"
        member.display_name = "Server Nick"
        ctx = self._autocomplete_ctx(author_id=12345, member=member)

        await self._run_base_autocomplete(ctx)

        self.assertEqual(self.bot.builder_names["12345"], "@BuilderOne")

    @patch("cogs.base_commands.anyio.sleep", new_callable=AsyncMock)
    @patch("cogs.base_commands.BaseLayout")
    async def test_warm_builder_names_fetches_only_missing_users(self, mock_base_layout, mock_sleep):
        rows = [
            MagicMock(district="Barbarian Camp", builder_id="111"),
            MagicMock(district="Wizard Valley", builder_id="222"),
            MagicMock(district="Dragon Cliffs", builder_id="111"),
        ]
        mock_base_layout.find_all.return_value.project.return_value.to_list = AsyncMock(return_value=rows)
        self.bot.builder_names = {"111": "@AlreadyCached"}
        self.bot.get_guild.return_value = None
        self.bot.get_user.return_value = None
        fetched = MagicMock()
        fetched.tag = "@FetchedBuilder"
        fetched.display_name = "Fetched Nick"
        self.bot.fetch_user = AsyncMock(return_value=fetched)

        await self.cog.warm_builder_names()

        self.bot.fetch_user.assert_awaited_once_with("222")
        self.assertEqual(self.bot.builder_names, {"111": "@AlreadyCached", "222": "@FetchedBuilder"})
        mock_sleep.assert_awaited_once_with(0.1)


if __name__ == "__main__":
    unittest.main()

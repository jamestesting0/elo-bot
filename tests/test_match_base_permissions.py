import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.match_commands import MatchCommands
from database_models import BaseLink, Match, OverrideCode, Participant


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AHELPER"
FUTURE_TIME = 3_000_000_000


class TestMatchBasePermissions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = MagicMock()
        self.bot.user.id = 999
        self.bot.owner.id = 999_999
        self.bot.fetch_channel = AsyncMock()
        self.cog = MatchCommands(self.bot)

    @staticmethod
    def _context() -> MagicMock:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 111
        ctx.channel.id = 123
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def _match() -> MagicMock:
        match = MagicMock(spec=Match)
        match.token = "TOKEN"
        match.thread = "123"
        match.thread_message_id = None
        match.base_submission = True
        match.is_autowin = False
        match.is_war = True
        match.times = [FUTURE_TIME, FUTURE_TIME]
        match.hosts = [None, None]
        match.final_districts = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
        match.participants = [
            Participant(
                user_id="111",
                bases=[BaseLink(link=BASE_LINK, image="https://example.test/helper.webp"), None, None],
            ),
            Participant(user_id="222"),
        ]
        match.save_changes = AsyncMock()
        return match

    async def test_submit_button_omits_recorded_helper_base(self) -> None:
        # Given: the player's recorded match base also exists in the helper bank.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.channel = None
        ctx.custom_id = "submit_bases|TOKEN"
        ctx.author.id = 111
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()
        match = self._match()
        match.thread = None

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player clicks the submit-bases button.
            await self.cog.submit_bases_button.callback.func(self.cog, ctx)

        # Then: only the empty district slots are editable, with their original IDs.
        ctx.send_modal.assert_awaited_once()
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual([component.custom_id for component in modal.components], ["Balloon Lagoon", "Dragon Cliffs"])
        self.assertEqual([component.label for component in modal.components], ["Balloon Lagoon", "Dragon Cliffs"])
        ctx.send.assert_not_awaited()

    async def test_submit_button_rejects_all_helper_bases_before_thread_work(self) -> None:
        # Given: every district already has a Helper base.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.channel = None
        ctx.custom_id = "submit_bases|TOKEN"
        ctx.author.id = 111
        ctx.send = AsyncMock()
        ctx.send_modal = AsyncMock()
        match = self._match()
        match.participants[0].bases = [match.participants[0].bases[0]] * 3

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player clicks the submit button.
            await self.cog.submit_bases_button.callback.func(self.cog, ctx)

        # Then: an ephemeral rejection replaces the modal, with no state or thread changes.
        ctx.send_modal.assert_not_awaited()
        self.assertIn("helper", ctx.send.await_args.args[0].lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.bot.fetch_channel.assert_not_awaited()
        match.save_changes.assert_not_awaited()

    async def test_submit_button_prefills_non_helper_base(self) -> None:
        # Given: a recorded base has no non-Private entry in the Helper bank.
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.channel = None
        ctx.custom_id = "submit_bases|TOKEN"
        ctx.author.id = 111
        ctx.send_modal = AsyncMock()
        match = self._match()
        match.thread = None

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=None)
            # When: the player opens the modal.
            await self.cog.submit_bases_button.callback.func(self.cog, ctx)

        # Then: the recorded link remains editable and prefilled.
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual([component.custom_id for component in modal.components], ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"])
        self.assertEqual(modal.components[0].value, BASE_LINK)

    async def test_submit_modal_updates_original_slots_and_preserves_omitted_base(self) -> None:
        # Given: the first Helper slot was omitted and the other fields arrive out of order.
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "submit_bases_modal|TOKEN"
        ctx.author.id = 111
        ctx.author.mention = "<@111>"
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = self._match()
        original_base = match.participants[0].bases[0]
        match.participants[0].bases[2] = BaseLink(link="old", image="old-image")
        new_link = BASE_LINK.replace("%3A1%3AHELPER", "%3A3%3APRIVATE")
        message = MagicMock(embeds=[ipy.Embed()], components=[ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN"),
        )])
        message.author.id = 999
        message.edit = AsyncMock()
        thread = MagicMock(fetch_pinned_messages=AsyncMock(return_value=[message]))
        self.bot.fetch_channel.return_value = thread

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=None)
            # When: the remaining fields are submitted.
            await self.cog.submit_bases_modal.callback.func(self.cog, ctx, **{"Dragon Cliffs": "", "Balloon Lagoon": new_link})

        # Then: the correct slot is updated, the empty field clears, and the Helper base survives intact.
        match.save_changes.assert_awaited_once()
        self.assertIs(match.participants[0].bases[0], original_base)
        self.assertEqual(match.participants[0].bases[1].link, new_link)
        self.assertIsNone(match.participants[0].bases[2])
        message.edit.assert_awaited_once()
        self.assertIn("submitted", ctx.send.await_args.args[0])

    async def test_submit_modal_accepts_recorded_helper_base_without_rechecking_it(self) -> None:
        # Given: the modal is prefilled with a Helper link already recorded for this match slot.
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "submit_bases_modal|TOKEN"
        ctx.author.id = 111
        ctx.author.mention = "<@111>"
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = self._match()
        match_message = MagicMock(embeds=[ipy.Embed()], components=[ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN"),
        )])
        match_message.author.id = 999
        match_message.edit = AsyncMock()
        thread = MagicMock()
        thread.fetch_pinned_messages = AsyncMock(return_value=[match_message])
        self.bot.fetch_channel.return_value = thread

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player submits the unchanged helper link from the modal.
            await self.cog.submit_bases_modal.callback.func(
                self.cog,
                ctx,
                **{"Barbarian Camp": BASE_LINK, "Balloon Lagoon": "", "Dragon Cliffs": ""},
            )

        # Then: the unchanged recorded link is persisted without querying Helper ownership.
        match.save_changes.assert_awaited_once()
        base_layout_model.find_one.assert_not_awaited()
        self.assertEqual(match.participants[0].bases[0].link, BASE_LINK)

    async def test_submit_modal_rejects_new_helper_base(self) -> None:
        # Given: the submitted Helper link is not already recorded for this match slot.
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "submit_bases_modal|TOKEN"
        ctx.author.id = 111
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = self._match()
        match.participants[0].bases[0] = None

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player submits the new Helper link.
            await self.cog.submit_bases_modal.callback.func(
                self.cog,
                ctx,
                **{"Barbarian Camp": BASE_LINK, "Balloon Lagoon": "", "Dragon Cliffs": ""},
            )

        # Then: Helper ownership is checked and the new link is rejected.
        match.save_changes.assert_not_awaited()
        base_layout_model.find_one.assert_awaited_once()
        self.assertIn("helper", ctx.send.await_args.args[0].lower())
        self.assertIn("Barbarian Camp", ctx.send.await_args.args[0])

    async def test_submit_modal_accepts_a_private_saved_base(self) -> None:
        # Given: the submitted link exists only in the player's private base bank.
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "submit_bases_modal|TOKEN"
        ctx.author.id = 111
        ctx.author.mention = "<@111>"
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        match = self._match()
        match.participants[0].bases[0] = None
        match_message = MagicMock(embeds=[ipy.Embed()], components=[ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN"),
        )])
        match_message.author.id = 999
        match_message.edit = AsyncMock()
        thread = MagicMock()
        thread.fetch_pinned_messages = AsyncMock(return_value=[match_message])
        self.bot.fetch_channel.return_value = thread

        async def find_layout(_link_filter, quality_filter=None) -> MagicMock | None:
            return MagicMock(quality="Private") if quality_filter is None else None

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(side_effect=find_layout)
            # When: the player submits their private saved link.
            await self.cog.submit_bases_modal.callback.func(
                self.cog,
                ctx,
                **{"Barbarian Camp": BASE_LINK, "Balloon Lagoon": "", "Dragon Cliffs": ""},
            )

        # Then: only helper-bank ownership is blocked.
        match.save_changes.assert_awaited_once()
        self.assertEqual(match.participants[0].bases[0].link, BASE_LINK)

    async def test_remove_allows_a_helper_base_before_opponent_war_start(self) -> None:
        # Given: the opponent's war is scheduled but has no assigned host yet.
        ctx = self._context()
        match = self._match()

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
            patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player removes their helper base.
            await self.cog.match_base_remove.callback.func(
                self.cog,
                ctx,
                player=0,
                district="Barbarian Camp",
            )

        # Then: the scheduled timestamp alone does not protect the helper base.
        self.assertIsNone(match.participants[0].bases[0])
        match.save_changes.assert_awaited_once()

    async def test_remove_all_allows_helper_bases_before_live_start(self) -> None:
        # Given: a live match is scheduled but has no assigned host yet.
        ctx = self._context()
        match = self._match()
        match.is_war = False

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
            patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=MagicMock(quality="Good"))
            # When: the player removes all their bases.
            await self.cog.match_base_remove_all.callback.func(self.cog, ctx, player=0)

        # Then: the scheduled timestamp alone does not protect helper bases.
        self.assertEqual(match.participants[0].bases, [None, None, None])
        match.save_changes.assert_awaited_once()

    async def test_remove_rejects_after_live_match_start(self) -> None:
        # Given: the live match has an assigned host and is therefore underway.
        ctx = self._context()
        match = self._match()
        match.is_war = False
        match.hosts = ["333", None]

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
            patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=None)
            # When: the player tries to remove a base after the live start.
            await self.cog.match_base_remove.callback.func(
                self.cog,
                ctx,
                player=0,
                district="Barbarian Camp",
            )

        # Then: removal is rejected before persistence.
        self.assertIsNotNone(match.participants[0].bases[0])
        match.save_changes.assert_not_awaited()
        self.assertIn("started", ctx.send.await_args.args[0].lower())

    async def test_remove_all_rejects_after_opponent_war_start(self) -> None:
        # Given: the host for the opponent's war side is already assigned.
        ctx = self._context()
        match = self._match()
        match.hosts = [None, "333"]
        original_base = match.participants[0].bases[0]

        with (
            patch("cogs.match_commands.Match") as match_model,
            patch("cogs.match_commands.BaseLayout") as base_layout_model,
            patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            base_layout_model.find_one = AsyncMock(return_value=None)
            # When: the player tries to remove their bases after the opponent's war start.
            await self.cog.match_base_remove_all.callback.func(self.cog, ctx, player=0)

        # Then: removal is rejected before persistence.
        self.assertIs(match.participants[0].bases[0], original_base)
        match.save_changes.assert_not_awaited()
        self.assertIn("started", ctx.send.await_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()

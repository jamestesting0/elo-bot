import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from bson import ObjectId

import cogs.base_commands as commands
from common.base_fingerprints.search import SearchSession, render_page
from common.base_fingerprints.types import MatchScore
from elobot.config.emojis import ERROR_EMOJI
from elobot.db import BaseLayout
from tests._base_fingerprint_fixture import fingerprint
from tests._base_finder_lock_fixture import ContendedLock


class TestSimilarPagination(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        bot = MagicMock(spec=ipy.Client)
        bot.ext = {}
        self.cog = commands.BaseCommands(bot)
        self.bases = [BaseLayout.model_construct(
            id=ObjectId(), district="Barbarian Camp", link=f"https://base/{index}",
            image_url=f"https://image/{index}", quality="Good", builder_id="123",
            fingerprint=fingerprint(), image_revision=0,
        ) for index in range(3)]
        self.session = SearchSession(
            "123", "Barbarian Camp", [MatchScore(
                base_id=str(base.id), source_revision=0, similarity=100 - index, matched=2, total=2,
            ) for index, base in enumerate(self.bases)], [], message_id="777", query=fingerprint(),
        )
        self.cog._similarity.add(self.session)

        async def visible(query: dict) -> BaseLayout | None:
            return next((base for base in self.bases if base.id == query["_id"] and base.quality != "Private"), None)

        self.enterContext(patch.object(BaseLayout, "find_one", new=AsyncMock(side_effect=visible)))

    def context(self, *, modal: bool = False) -> SimpleNamespace:
        prefix = "base_similar_modal" if modal else "base_similar"
        return SimpleNamespace(
            author=SimpleNamespace(id=123, has_any_role=lambda roles: True),
            guild=SimpleNamespace(fetch_member=AsyncMock(
                return_value=SimpleNamespace(id=123, display_name="Builder"))),
            message=SimpleNamespace(id=777, edit=AsyncMock()),
            custom_id=f"{prefix}|{self.session.token}|page", send=AsyncMock(), send_modal=AsyncMock(),
            defer=AsyncMock(), edit=AsyncMock(), edit_origin=AsyncMock(),
        )

    async def jump(self, ctx: SimpleNamespace, page: str) -> None:
        await self.cog.similar_page_modal.callback.func(self.cog, ctx, page=page)

    async def test_pager_matches_base_list_on_first_middle_last_and_single_pages(self) -> None:
        ctx = self.context()
        for total in (3, 1):
            self.session.matches = self.session.matches[:total]
            for index in range(total):
                self.session.page = index
                payload = await render_page(self.session, ctx)
                self.assertEqual(len(payload["components"]), 2)
                actual = payload["components"][0].to_dict()
                expected = commands._build_base_list_components(
                    "Any", index, "Performance", "Any", "Any", total,
                )[-1].to_dict()
                for row in (actual, expected):
                    for button in row["components"]:
                        button.pop("custom_id")
                self.assertEqual(actual, expected)
                self.assertEqual(payload["components"][1].components[0].custom_id,
                                 f"base_compare|{self.session.token}|{self.session.matches[index].base_id}")

    async def test_page_counter_opens_matching_jump_modal(self) -> None:
        ctx = self.context()
        await self.cog.similar_page.callback.func(self.cog, ctx)
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.title, "Jump to page")
        self.assertEqual(modal.custom_id, f"base_similar_modal|{self.session.token}|page")
        self.assertEqual(modal.components[0].custom_id, "page")
        self.assertTrue(modal.components[0].required)
        ctx.defer.assert_not_awaited()
        ctx.edit_origin.assert_not_awaited()

    async def test_jump_updates_same_response_and_action_target(self) -> None:
        ctx = self.context(modal=True)
        for page in ("3", " 1 ", "2"):
            await self.jump(ctx, page)
            index = int(page) - 1
            self.assertEqual(self.session.page, index)
            self.assertEqual(ctx.edit.await_args.args, ("@original",))
            payload = ctx.edit.await_args.kwargs
            self.assertEqual(payload["embeds"][0].image.url, self.bases[index].image_url)
            for button in payload["components"][1].components:
                self.assertTrue(button.custom_id.endswith(str(self.bases[index].id)))
        ctx.edit_origin.assert_not_awaited()
        ctx.send.assert_not_awaited()

    async def test_invalid_jump_preserves_page_and_message(self) -> None:
        ctx = self.context(modal=True)
        self.session.page = 1
        for value in ("0", "-1", "4", "abc", "2.5", ""):
            with self.subTest(value=value):
                await self.jump(ctx, value)
                self.assertEqual(self.session.page, 1)
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        ctx.defer.assert_not_awaited()
        ctx.edit.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()

    async def test_jump_rechecks_visibility_and_current_page_range(self) -> None:
        ctx = self.context(modal=True)
        self.bases[1].quality = "Private"
        await self.jump(ctx, "3")
        self.assertIn("between 1 and 2", ctx.send.await_args.args[0])
        ctx.edit.assert_not_awaited()
        await self.jump(ctx, "2")
        self.assertEqual(ctx.edit.await_args.kwargs["embeds"][0].image.url, self.bases[2].image_url)
        self.assertEqual(ctx.edit.await_args.kwargs["components"][0].components[1].label, "2/2")
        self.bases.clear()
        await self.jump(ctx, "1")
        self.assertIn("No results remain", ctx.edit.await_args.kwargs["content"])
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_modal_rejects_wrong_owner_message_and_missing_message(self) -> None:
        for owner, message in ((456, 777), (123, 778), (123, None)):
            with self.subTest(owner=owner, message=message):
                ctx = self.context(modal=True)
                ctx.author.id = owner
                ctx.message = None if message is None else SimpleNamespace(id=message, edit=AsyncMock())
                await self.jump(ctx, "2")
                ctx.send.assert_awaited_once()
                ctx.edit.assert_not_awaited()
                self.assertEqual(self.session.page, 0)

    async def test_modal_rechecks_expiry_and_access_after_opening(self) -> None:
        button = self.context()
        await self.cog.similar_page.callback.func(self.cog, button)
        self.session.expires_at = time.monotonic() - 1
        ctx = self.context(modal=True)
        await self.jump(ctx, "2")
        self.assertIn("expired", ctx.send.await_args.args[0])
        ctx.message.edit.assert_awaited_once_with(components=[])
        self.session.expires_at = time.monotonic() + 900
        self.cog._similarity.add(self.session)
        ctx = self.context(modal=True)
        ctx.author.has_any_role = lambda roles: False
        await self.jump(ctx, "2")
        self.assertIn("no longer have access", ctx.send.await_args.args[0])
        ctx.edit.assert_not_awaited()

    async def test_expiry_is_rechecked_after_waiting_for_navigation_lock(self) -> None:
        ctx = self.context(modal=True)
        self.session.lock = ContendedLock()
        await self.session.lock.acquire()
        task = asyncio.create_task(self.jump(ctx, "2"))
        try:
            await asyncio.wait_for(self.session.lock.contended.wait(), 5)
            self.session.expires_at = time.monotonic() - 1
        finally:
            self.session.lock.release()
        await asyncio.wait_for(task, 5)
        self.assertEqual(self.session.page, 0)
        self.assertIn("expired", ctx.edit.await_args.kwargs["content"])
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_jump_and_arrow_edits_share_navigation_lock(self) -> None:
        self.session.lock = ContendedLock()
        entered, release = asyncio.Event(), asyncio.Event()
        titles = []
        modal = self.context(modal=True)

        async def edit(message: str, **payload) -> None:
            self.assertEqual(message, "@original")
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            titles.append(payload["embeds"][0].title)

        modal.edit.side_effect = edit
        jumping = asyncio.create_task(self.jump(modal, "2"))
        await asyncio.wait_for(entered.wait(), 5)
        arrow = self.context()
        arrow.custom_id = f"base_similar|{self.session.token}|next"

        async def edit_origin(**payload) -> None:
            titles.append(payload["embeds"][0].title)

        arrow.edit_origin.side_effect = edit_origin
        advancing = asyncio.create_task(self.cog.similar_page.callback.func(self.cog, arrow))
        await asyncio.wait_for(self.session.lock.contended.wait(), 5)
        self.assertEqual(titles, [])
        release.set()
        await asyncio.wait_for(asyncio.gather(jumping, advancing), 5)
        self.assertEqual(titles, ["**Similar Base 2/3**", "**Similar Base 3/3**"])

    async def test_native_discord_contexts_send_modal_edit_original_and_preserve_invalid_input(self) -> None:
        client = MagicMock(spec=ipy.Client)
        client.app = SimpleNamespace(id=42)
        client.cache = MagicMock()
        client.http = MagicMock()
        message = self.context().message
        client.cache.place_message_data.return_value = message
        client.cache.get_message.return_value = message
        client.cache.get_member.return_value = None
        client.cache.get_user.return_value = self.context().author
        client.cache.get_guild.return_value = self.context().guild
        client.http.post_initial_response = AsyncMock(return_value={"id": "777"})
        client.http.edit_interaction_message = AsyncMock(return_value={"id": "777"})

        def native_context(page: str | None = None) -> ipy.ComponentContext | ipy.ModalContext:
            ctx_type = ipy.ComponentContext if page is None else ipy.ModalContext
            data = {"custom_id": f"base_similar{'_modal' if page is not None else ''}|{self.session.token}|page"}
            if page is None:
                data["component_type"] = int(ipy.ComponentType.BUTTON)
            else:
                data["components"] = [{"type": 1, "components": [
                    {"type": 4, "custom_id": "page", "value": page},
                ]}]
            return ctx_type.from_dict(client, {
                "id": "1750000000000000000", "application_id": "42",
                "type": int(ipy.InteractionType.MESSAGE_COMPONENT if page is None else ipy.InteractionType.MODAL_RESPONSE),
                "token": "redacted", "version": 1, "locale": "en-US", "channel_id": "77", "guild_id": "88",
                "user": {"id": "123", "username": "owner", "discriminator": "0", "avatar": None},
                "message": {"id": "777"}, "data": data,
            })

        button = native_context()
        await self.cog.similar_page.callback.func(self.cog, button)
        response = client.http.post_initial_response.await_args.args[0]
        self.assertEqual(response["type"], int(ipy.CallbackType.MODAL))
        self.assertEqual(response["data"]["title"], "Jump to page")
        client.http.post_initial_response.reset_mock()

        invalid = native_context("4")
        await self.cog.similar_page_modal.callback.func(self.cog, invalid, **invalid.responses)
        response = client.http.post_initial_response.await_args.args[0]
        self.assertEqual(response["type"], int(ipy.CallbackType.CHANNEL_MESSAGE_WITH_SOURCE))
        self.assertTrue(response["data"]["flags"] & int(ipy.MessageFlags.EPHEMERAL))
        self.assertIn("between 1 and 3", response["data"]["content"])
        client.http.edit_interaction_message.assert_not_awaited()
        client.http.post_initial_response.reset_mock()

        valid = native_context("3")
        await self.cog.similar_page_modal.callback.func(self.cog, valid, **valid.responses)
        self.assertEqual(client.http.post_initial_response.await_args.args[0]["type"],
                         int(ipy.CallbackType.DEFERRED_UPDATE_MESSAGE))
        edit = client.http.edit_interaction_message.await_args.kwargs
        self.assertEqual(edit["message_id"], "@original")
        self.assertEqual(edit["payload"]["embeds"][0]["title"], "**Similar Base 3/3**")
        self.assertEqual(edit["payload"]["components"][0]["components"][1]["label"], "3/3")

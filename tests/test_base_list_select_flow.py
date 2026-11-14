"""End-to-end coverage for /base list district and embed-type selects.

Ways this flow can fail (written before the cog change):
1. The initial list has no district or embed-type select rows.
2. Select custom IDs reuse leaderboard IDs (`district_type_select` /
   `base_quality_select`), so the wrong handler runs.
3. Left/right navigation or a page jump drops the select rows.
4. A district change loses the quality or builder encoded in pagination IDs.
5. A district change does not reset to page 1.
6. An embed-type change loses district/quality/builder or resets the page.
7. Choosing a district with no matches overwrites the current list.
8. Another user can operate the selects.
9. Any -> named district -> Any loses the all-district filter.
10. After a district change, later pagination still uses the old district.
11. Discord cannot JSON-serialize the send/edit payload (select labels must be
    plain strings, not SlashCommandChoice LocalisedField names).
12. District/embed selects defer an ephemeral reply then edit ctx.message, which
    does not update the ephemeral list the way pagination's edit_origin does.
13. A custom_id or select option exceeds Discord's 100-character limit.
"""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from interactions.models.discord.message import process_message_payload

from elobot.extensions.base import BaseCommands
from elobot.config.game import DISTRICT_CHOICES
from tests._patch_sites import patch_sites

OWNER_ID = 111
OTHER_USER_ID = 999
BOT_ID = 42
LEADERBOARD_SELECT_IDS = {"district_type_select", "base_quality_select"}


class _Field:
    def __init__(self, name: str):
        self.name = name

    def __eq__(self, other):
        return (self.name, "==", other)

    def __ne__(self, other):
        return (self.name, "!=", other)


class _ListQuery:
    def __init__(self, bases, filters, skip=0, limit=None):
        self._bases = bases
        self._filters = list(filters)
        self._skip = skip
        self._limit = limit

    def find(self, *predicates):
        return _ListQuery(self._bases, self._filters + list(predicates), self._skip, self._limit)

    def sort(self, _field):
        return _ListQuery(self._bases, self._filters, self._skip, self._limit)

    def skip(self, n):
        return _ListQuery(self._bases, self._filters, n, self._limit)

    def limit(self, n):
        return _ListQuery(self._bases, self._filters, self._skip, n)

    def _matching(self):
        results = list(self._bases)
        for field, op, value in self._filters:
            if op == "==":
                results = [base for base in results if getattr(base, field) == value]
            elif op == "!=":
                results = [base for base in results if getattr(base, field) != value]
        results.sort(key=lambda base: base.id)
        return results

    async def count(self):
        return len(self._matching())

    async def to_list(self):
        matching = self._matching()
        end = None if self._limit is None else self._skip + self._limit
        return matching[self._skip:end]


def _base(base_id, district, quality, builder_id):
    return SimpleNamespace(
        id=base_id,
        district=district,
        quality=quality,
        builder_id=builder_id,
        link=f"https://link.clashofclans.com/{base_id}",
        image_url=f"https://example.com/{base_id}.png",
        records=[("999", 2, 80.0, 1700000000.0)],
        expiration_date=2000000000.0,
    )


def _install_store(mock_base_layout, bases):
    mock_base_layout.quality = _Field("quality")
    mock_base_layout.district = _Field("district")
    mock_base_layout.builder_id = _Field("builder_id")
    mock_base_layout.find.side_effect = lambda *predicates: _ListQuery(bases, predicates)


def _component_rows(components):
    if isinstance(components, ipy.ActionRow):
        return [components]
    return list(components or [])


def _snapshot_message(message):
    rows = _component_rows(message.components)
    serialized_rows = []
    for row in rows:
        items = []
        for component in row.components:
            item = {
                "type": type(component).__name__,
                "custom_id": getattr(component, "custom_id", None),
            }
            if getattr(component, "options", None) is not None:
                item["options"] = [option.value for option in component.options]
                item["placeholder"] = getattr(component, "placeholder", None)
            else:
                item["label"] = getattr(component, "label", None)
                item["disabled"] = getattr(component, "disabled", None)
            items.append(item)
        serialized_rows.append(items)
    embed = message.embeds[0] if message.embeds else None
    return {
        "title": None if embed is None else embed.title,
        "fields": [] if embed is None else [field.name for field in embed.fields],
        "rows": serialized_rows,
    }


def _discord_json_payload(kwargs):
    payload = json.loads(json.dumps(process_message_payload(
        embeds=kwargs.get("embeds") or kwargs.get("embed"),
        components=kwargs.get("components"),
    )))
    for row in payload.get("components") or []:
        for component in row.get("components") or []:
            custom_id = component.get("custom_id")
            if custom_id is not None and len(custom_id) > 100:
                raise AssertionError(f"custom_id exceeds 100 chars: {custom_id!r}")
            for option in component.get("options") or []:
                if not isinstance(option.get("label"), str) or not isinstance(option.get("value"), str):
                    raise AssertionError(f"select option is not a string: {option!r}")
                if len(option["label"]) > 100 or len(option["value"]) > 100:
                    raise AssertionError(f"select option exceeds 100 chars: {option!r}")
    return payload


def _option_labels(payload):
    labels = []
    for row in payload.get("components") or []:
        for component in row.get("components") or []:
            for option in component.get("options") or []:
                labels.append(option["label"])
    return labels


class TestBaseListSelectFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.user = MagicMock(id=BOT_ID)
        self.cog = BaseCommands(self.bot)
        self.bases = [
            _base(1, "Barbarian Camp", "Excellent", "111"),
            _base(2, "Barbarian Camp", "Excellent", "111"),
            _base(3, "Wizard Valley", "Excellent", "111"),
            _base(4, "Barbarian Camp", "Good", "111"),
            _base(5, "Wizard Valley", "Excellent", "222"),
            _base(6, "Skeleton Park", "Private", "111"),
        ]
        self.message = SimpleNamespace(
            embeds=[],
            components=None,
            interaction=SimpleNamespace(user=OWNER_ID),
        )
        self.publications = []
        self.followups = []
        self.builder = MagicMock(spec=ipy.User, id=OWNER_ID, mention=f"<@{OWNER_ID}>")

    def _publish(self, kwargs):
        payload = _discord_json_payload(kwargs)
        embed = kwargs.get("embed")
        embeds = kwargs.get("embeds")
        if embed is not None:
            self.message.embeds = [embed]
        elif embeds is not None:
            self.message.embeds = list(embeds)
        if "components" in kwargs:
            self.message.components = kwargs["components"]
        snapshot = _snapshot_message(self.message)
        snapshot["option_labels"] = _option_labels(payload)
        self.publications.append(snapshot)

    async def _send(self, content=None, **kwargs):
        if "embed" in kwargs or "components" in kwargs:
            self._publish(kwargs)
            return
        self.followups.append(content)

    async def _edit(self, _target, **kwargs):
        self._publish(kwargs)

    async def _edit_origin(self, **kwargs):
        self._publish(kwargs)

    def _slash_ctx(self):
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.author.id = OWNER_ID
        ctx.author.has_any_role.return_value = True
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock(side_effect=self._send)
        return ctx

    def _component_ctx(self, *, custom_id, values=None, author_id=OWNER_ID):
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.author.id = author_id
        ctx.message = self.message
        ctx.custom_id = custom_id
        ctx.values = values or []
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock(side_effect=self._send)
        ctx.edit = AsyncMock(side_effect=self._edit)
        ctx.edit_origin = AsyncMock(side_effect=self._edit_origin)
        ctx.send_modal = AsyncMock()
        return ctx

    def _modal_ctx(self, custom_id):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.author.id = OWNER_ID
        ctx.message = self.message
        ctx.custom_id = custom_id
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock(side_effect=self._send)
        ctx.edit = AsyncMock(side_effect=self._edit)
        return ctx

    def _row(self, index):
        rows = _component_rows(self.message.components)
        self.assertIsInstance(self.message.components, list)
        self.assertEqual(len(rows), 3)
        return rows[index]

    def _assert_selects(self, *, district, embed_type, quality, builder_id, page, total, field):
        district_select = self._row(0).components[0]
        embed_select = self._row(1).components[0]
        left, page_button, right = self._row(2).components
        self.assertEqual(district_select.custom_id, "base_list_district_select")
        self.assertEqual(embed_select.custom_id, "base_list_embed_type_select")
        self.assertNotIn(district_select.custom_id, LEADERBOARD_SELECT_IDS)
        self.assertNotIn(embed_select.custom_id, LEADERBOARD_SELECT_IDS)
        self.assertEqual(
            [option.value for option in district_select.options],
            ["Any", *(choice.value for choice in DISTRICT_CHOICES)],
        )
        self.assertEqual(
            [option.value for option in embed_select.options],
            ["Performance", "Defense Log"],
        )
        self.assertEqual(
            self.publications[-1]["option_labels"],
            ["Any", *(choice.value for choice in DISTRICT_CHOICES), "Performance", "Defense Log"],
        )
        self.assertEqual(
            left.custom_id,
            f"base_left|{district}|{page - 1}|{embed_type}|{quality}|{builder_id}",
        )
        self.assertEqual(
            page_button.custom_id,
            f"base_page|{district}|{embed_type}|{quality}|{builder_id}",
        )
        self.assertEqual(
            right.custom_id,
            f"base_right|{district}|{page - 1}|{embed_type}|{quality}|{builder_id}",
        )
        self.assertEqual(page_button.label, f"{page}/{total}")
        self.assertEqual(left.disabled, page == 1)
        self.assertEqual(right.disabled, page == total)
        self.assertEqual(self.message.embeds[0].fields[-1].name, field)
        published_ids = {
            item["custom_id"]
            for row in self.publications[-1]["rows"]
            for item in row
        }
        self.assertTrue(published_ids.isdisjoint(LEADERBOARD_SELECT_IDS))

    @patch_sites(('elobot.extensions.base.extraction.BaseLayout', 'elobot.extensions.base.submissions.BaseLayout', 'elobot.extensions.base.manage.BaseLayout', 'elobot.extensions.base.private.BaseLayout', 'elobot.extensions.base.leaderboard.BaseLayout', 'elobot.features.bases.layouts.queries.BaseLayout', 'elobot.features.bases.layouts.ui.BaseLayout', 'elobot.features.bases.layouts.private.BaseLayout'))
    async def test_list_district_and_embed_selects_keep_filters_and_pagination(self, mock_base_layout):
        # Given: public Excellent bases for builder 111 across two districts, plus
        # quality, builder, and private rows that must never appear.
        _install_store(mock_base_layout, self.bases)
        ctx = self._slash_ctx()

        # When: the owner lists Excellent bases and then switches district, page,
        # embed type, and back to Any before a stranger and an empty district act.
        await self.cog.base_list.callback(
            ctx,
            district="Any",
            embed_type="Performance",
            base_quality="Excellent",
            builder=self.builder,
            hidden=True,
        )
        self._assert_selects(
            district="Any", embed_type="Performance", quality="Excellent",
            builder_id="111", page=1, total=3, field="**Performance**",
        )
        self.assertEqual(self.message.embeds[0].title, "**Barbarian Camp Base**")

        district_ctx = self._component_ctx(
            custom_id=self._row(0).components[0].custom_id,
            values=["Barbarian Camp"],
        )
        await self.cog.base_list_district_select.callback(district_ctx)
        self._assert_selects(
            district="Barbarian Camp", embed_type="Performance", quality="Excellent",
            builder_id="111", page=1, total=2, field="**Performance**",
        )
        district_ctx.defer.assert_not_awaited()
        district_ctx.edit.assert_not_awaited()
        district_ctx.edit_origin.assert_awaited()

        right_ctx = self._component_ctx(custom_id=self._row(2).components[2].custom_id)
        await self.cog.base_right_button.callback(right_ctx)
        self._assert_selects(
            district="Barbarian Camp", embed_type="Performance", quality="Excellent",
            builder_id="111", page=2, total=2, field="**Performance**",
        )

        embed_ctx = self._component_ctx(
            custom_id=self._row(1).components[0].custom_id,
            values=["Defense Log"],
        )
        await self.cog.base_list_embed_type_select.callback(embed_ctx)
        embed_ctx.defer.assert_not_awaited()
        embed_ctx.edit.assert_not_awaited()
        embed_ctx.edit_origin.assert_awaited()
        self._assert_selects(
            district="Barbarian Camp", embed_type="Defense Log", quality="Excellent",
            builder_id="111", page=2, total=2, field="**Defense Log**",
        )

        any_ctx = self._component_ctx(
            custom_id=self._row(0).components[0].custom_id,
            values=["Any"],
        )
        await self.cog.base_list_district_select.callback(any_ctx)
        any_ctx.defer.assert_not_awaited()
        any_ctx.edit.assert_not_awaited()
        any_ctx.edit_origin.assert_awaited()
        self._assert_selects(
            district="Any", embed_type="Defense Log", quality="Excellent",
            builder_id="111", page=1, total=3, field="**Defense Log**",
        )
        self.assertEqual(self.message.embeds[0].title, "**Barbarian Camp Base**")

        page_id = self._row(2).components[1].custom_id
        modal_ctx = self._modal_ctx(page_id.replace("base_page|", "base_page_modal|", 1))
        await self.cog.base_page_modal.callback(modal_ctx, page="3")
        self._assert_selects(
            district="Any", embed_type="Defense Log", quality="Excellent",
            builder_id="111", page=3, total=3, field="**Defense Log**",
        )
        self.assertEqual(self.message.embeds[0].title, "**Wizard Valley Base**")

        before_reject = _snapshot_message(self.message)
        stranger_ctx = self._component_ctx(
            custom_id=self._row(0).components[0].custom_id,
            values=["Capital Peak"],
            author_id=OTHER_USER_ID,
        )
        await self.cog.base_list_district_select.callback(stranger_ctx)
        stranger_ctx.edit_origin.assert_not_awaited()
        self.assertEqual(_snapshot_message(self.message), before_reject)

        empty_ctx = self._component_ctx(
            custom_id=self._row(0).components[0].custom_id,
            values=["Goblin Mines"],
        )
        await self.cog.base_list_district_select.callback(empty_ctx)
        empty_ctx.edit_origin.assert_not_awaited()
        self.assertEqual(_snapshot_message(self.message), before_reject)
        self._assert_selects(
            district="Any", embed_type="Defense Log", quality="Excellent",
            builder_id="111", page=3, total=3, field="**Defense Log**",
        )

        self.assertEqual(len(self.followups), 5)
        self.assertEqual(len(self.publications), 6)
        for response_ctx in (district_ctx, embed_ctx, any_ctx, stranger_ctx, empty_ctx):
            response_ctx.send.assert_awaited_once()
            self.assertTrue(response_ctx.send.await_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()

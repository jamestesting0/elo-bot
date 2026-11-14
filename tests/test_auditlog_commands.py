import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import elobot.extensions.auditlog as auditlog_module
from elobot.extensions.auditlog import AuditlogCommands, _load_auditlog_page


def audit_entry(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        recorded_at_ns=(1_700_000_000 + index) * 1_000_000_000,
        command=f"/command-{index}", author_id=str(index), arguments={"index": index},
    )


def audit_query(entries: list[SimpleNamespace]) -> MagicMock:
    query = MagicMock()
    query.count = AsyncMock(return_value=len(entries))
    window = {"skip": 0, "limit": len(entries)}

    def skip(offset: int) -> MagicMock:
        window["skip"] = offset
        return query

    def limit(size: int) -> MagicMock:
        window["limit"] = size
        return query

    async def to_list() -> list[SimpleNamespace]:
        start = window["skip"]
        return entries[start:start + window["limit"]]

    query.sort.return_value = query
    query.skip.side_effect = skip
    query.limit.side_effect = limit
    query.to_list = AsyncMock(side_effect=to_list)
    return query


class TestAuditlogCommands(unittest.IsolatedAsyncioTestCase):
    async def test_command_completion_inserts_typed_audit_document(self) -> None:
        # Given: a monitored command with Discord objects and native scalar arguments
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.command.name = "data"
        ctx.command.group_name = None
        ctx.command.sub_cmd_name = "export"
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 42
        user = MagicMock(spec=ipy.User)
        user.id = 73
        attachment = MagicMock(spec=ipy.Attachment)
        attachment.url = "https://example.test/file"
        ctx.kwargs = {"user": user, "attachment": attachment, "visible": True, "count": 2}

        # When: command completion is audited
        with (patch("elobot.extensions.auditlog.time.time_ns", return_value=123456789),
              patch("elobot.extensions.auditlog.AuditLogEntry.get_pymongo_collection"),
              patch("elobot.extensions.auditlog.AuditLogEntry.insert", new_callable=AsyncMock) as insert):
            await cog.on_command_completion.callback(cog, SimpleNamespace(ctx=ctx))

        # Then: the typed document is inserted once without mutating command arguments
        entry = insert.await_args.args[0]
        self.assertEqual(entry.recorded_at_ns, 123456789)
        self.assertEqual(entry.command, "/data export")
        self.assertEqual(entry.author_id, "42")
        self.assertEqual(entry.arguments, {
            "user": 73, "attachment": "https://example.test/file", "visible": True, "count": 2,
        })
        self.assertIs(ctx.kwargs["user"], user)

    async def test_component_completion_inserts_channel_arguments(self) -> None:
        # Given: a monitored component interaction
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = next(iter(auditlog_module.MONITORED_COMPONENTS)) + "-value"
        ctx.author = MagicMock(spec=ipy.Member)
        ctx.author.id = 42
        ctx.channel.id = 84
        ctx.channel.name = "audit-channel"

        # When: component completion is audited
        with (patch("elobot.extensions.auditlog.time.time_ns", return_value=987654321),
              patch("elobot.extensions.auditlog.AuditLogEntry.get_pymongo_collection"),
              patch("elobot.extensions.auditlog.AuditLogEntry.insert", new_callable=AsyncMock) as insert):
            await cog.on_component_completion.callback(cog, SimpleNamespace(ctx=ctx))

        # Then: Mongo receives the component and typed channel data
        entry = insert.await_args.args[0]
        self.assertEqual(entry.recorded_at_ns, 987654321)
        self.assertEqual(entry.command, ctx.custom_id)
        self.assertEqual(entry.author_id, "42")
        self.assertEqual(entry.arguments, {"channel_id": 84, "channel_name": "audit-channel"})

    async def test_pages_query_newest_and_include_every_entry_in_seven_item_chunks(self) -> None:
        # Given: fifteen documents already returned in newest-first order
        entries = [audit_entry(index) for index in range(14, -1, -1)]
        query = audit_query(entries)
        guild = MagicMock(fetch_member=AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        ))

        # When: each audit page is loaded on its own
        with patch("elobot.extensions.auditlog.AuditLogEntry.find_all", return_value=query):
            pages = []
            for position in range(3):
                embed, total_pages = await _load_auditlog_page(position, guild)
                self.assertEqual(total_pages, 3)
                pages.append(embed)

        # Then: all records, including the single-record last page and oldest row, are rendered once
        self.assertEqual([call.args[0] for call in query.sort.call_args_list], ["-recorded_at_ns"] * 3)
        self.assertEqual([call.args[0] for call in query.skip.call_args_list], [0, 7, 14])
        self.assertEqual([call.args[0] for call in query.limit.call_args_list], [7, 7, 7])
        self.assertEqual(len(pages), 3)
        self.assertEqual([page.description.count("```json") for page in pages], [7, 7, 1])
        rendered = "".join(page.description for page in pages)
        for index in range(15):
            self.assertEqual(rendered.count(f"`/command-{index}`"), 1)

    async def test_auditlog_first_page_disables_left_button(self) -> None:
        query = audit_query([audit_entry(index) for index in range(9)])
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock()
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()

        with patch("elobot.extensions.auditlog.AuditLogEntry.find_all", return_value=query):
            await cog.auditlog_view.callback.func(cog, ctx, hidden=True)

        action_row = ctx.send.await_args.kwargs["components"][0]
        self.assertEqual(action_row.components[0].custom_id, "auditlog_left|0")
        self.assertTrue(action_row.components[0].disabled)
        self.assertEqual(action_row.components[1].label, "1/2")
        self.assertFalse(action_row.components[1].disabled)
        self.assertEqual(action_row.components[2].custom_id, "auditlog_right|1")

    async def test_auditlog_page_button_jumps_to_requested_page(self) -> None:
        query = audit_query([audit_entry(index) for index in range(16)])
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.author.id = 42
        ctx.message.interaction.user = 42
        ctx.edit = AsyncMock()
        ctx.send = AsyncMock()

        with patch("elobot.extensions.auditlog.AuditLogEntry.find_all", return_value=query):
            await cog.auditlog_page_modal.callback(ctx, page="3")

        ctx.edit.assert_awaited_once()
        self.assertIs(ctx.edit.await_args.args[0], ctx.message)
        self.assertIn("[Member 14](<https://discordapp.com/users/14>)", ctx.edit.await_args.kwargs["embed"].description)
        action_row = ctx.edit.await_args.kwargs["components"][0]
        self.assertEqual(action_row.components[1].label, "3/3")
        self.assertTrue(action_row.components[2].disabled)
        ctx.send.assert_not_awaited()

    async def test_auditlog_page_button_rejects_invalid_page(self) -> None:
        query = audit_query([audit_entry(index) for index in range(16)])
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.author.id = 42
        ctx.message.interaction.user = 42
        ctx.edit = AsyncMock()
        ctx.send = AsyncMock()

        with patch("elobot.extensions.auditlog.AuditLogEntry.find_all", return_value=query):
            await cog.auditlog_page_modal.callback(ctx, page="4")

        ctx.send.assert_awaited_once_with("Page must be between 1 and 3.", ephemeral=True)
        ctx.edit.assert_not_awaited()

    async def test_auditlog_buttons_remain_usable_when_page_modal_is_not_submitted(self) -> None:
        query = audit_query([audit_entry(index) for index in range(16)])
        cog = AuditlogCommands(MagicMock())
        page_ctx = MagicMock(spec=ipy.ComponentContext)
        page_ctx.author.id = 42
        page_ctx.message.interaction.user = 42
        page_ctx.send = AsyncMock()
        page_ctx.send_modal = AsyncMock()
        await cog.auditlog_page_button.callback(page_ctx)
        right_ctx = MagicMock(spec=ipy.ComponentContext)
        right_ctx.guild.fetch_member = AsyncMock(return_value=None)
        right_ctx.author.id = 42
        right_ctx.message.interaction.user = 42
        right_ctx.custom_id = "auditlog_right|1"
        right_ctx.edit_origin = AsyncMock()
        right_ctx.send = AsyncMock()

        with patch("elobot.extensions.auditlog.AuditLogEntry.find_all", return_value=query):
            await cog.auditlog_arrow_button.callback(right_ctx)

        page_ctx.send_modal.assert_awaited_once()
        modal = page_ctx.send_modal.await_args.args[0]
        self.assertEqual(modal.custom_id, "auditlog_page_modal")
        self.assertEqual(modal.components[0].custom_id, "page")
        page_ctx.send.assert_not_awaited()
        right_ctx.edit_origin.assert_awaited_once()
        query.skip.assert_called_once_with(7)
        query.limit.assert_called_once_with(7)
        self.assertEqual(right_ctx.edit_origin.await_args.kwargs["components"][0].components[1].label, "2/3")

    async def test_clear_deletes_all_audit_documents(self) -> None:
        cog = AuditlogCommands(MagicMock())
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.send = AsyncMock()

        with patch("elobot.extensions.auditlog.AuditLogEntry.delete_all", new_callable=AsyncMock) as delete_all:
            await cog.auditlog_clear.callback.func(cog, ctx)

        delete_all.assert_awaited_once_with()
        ctx.send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

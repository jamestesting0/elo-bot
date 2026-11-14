# noqa: SIZE_OK — focused lifecycle scenarios share the stored-start fixture

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

import elobot.features.base_store.self_service as self_service
import elobot.extensions.match as match_commands
import elobot.features.hosting.war.helper_base_store as helper_base_store
import elobot.features.hosting.war.helper_base_store_availability as helper_base_store_availability
from elobot.features.hosting.war import war_start
from elobot.features.hosting.war import stored_bases
from elobot.integrations.helper_api import (
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperTrackingFailure,
)
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from tests._stored_start_fixture import (
    BASE_LINKS, DISTRICTS, FIRST_CLAN_TAG, PLAYER_ID, SECOND_CLAN_TAG, TOKEN,
    StoredStartFixture, terminal_job,
)
from tests._patch_sites import patch_sites
import elobot.features.matches.resources as resources_module


class DiscordFollowupFailure(Exception):
    pass


class TestSelfBaseStoreLifecycle(StoredStartFixture):

    def assert_incomplete_storage(self, match: MagicMock, ctx: ipy.SlashContext) -> None:
        store = match.participants[0].base_store
        self.assertIsNotNone(store)
        self.assertTrue(match.participants[0].opponent_bases_stored)
        self.assertIs(store.ready, False)
        self.assertEqual(store.message_id, 4242)
        public_edit = ctx.channel.send.return_value.edit.await_args
        embed = public_edit.kwargs["embed"]
        self.assertEqual(embed["title"] if isinstance(embed, dict) else embed.title, "**Base Storage Incomplete**")
        self.assertFalse(embed.get("description") if isinstance(embed, dict) else embed.description)
        self.assertEqual([button.custom_id for button in public_edit.kwargs["components"]],
                         [f"helper_stored_undo|{TOKEN}|0|{store.reservation_id}"])
        self.assertIn(str(ERROR_EMOJI), ctx.send.await_args.args[0])
        self.assertNotIn(str(SUCCESS_EMOJI), ctx.send.await_args.args[0])

    async def test_self_store_prioritizes_slot_one_across_clans_and_waits_before_exact_controls(self) -> None:
        # Given
        match = self.make_match()
        ctx = self.make_slash_context()
        helper_entered = anyio.Event()
        allow_terminal = anyio.Event()
        trace: list[str] = []
        original_attach = match.attach_base_store_message.side_effect
        public_message = ctx.channel.send.return_value

        async def attach_message(index: int, message_id: int, *, ready: bool = True) -> bool:
            attached = await original_attach(index, message_id, ready=ready)
            trace.append("persist_message")
            return attached

        async def send_pending(*, embed: ipy.Embed, components: list[ipy.Button]) -> MagicMock:
            trace.append("pending_message")
            return public_message

        async def claim_reservation(*_args, **_kwargs) -> MagicMock:
            trace.append("persist")
            return MagicMock(matched_count=1)

        async def choose(*_args, **_kwargs):
            trace.append("choose")
            return match

        async def run_helper(_runtime, interaction_ctx, payload):
            self.assertIs(interaction_ctx, ctx)
            self.assertTrue(match.participants[0].opponent_bases_stored)
            self.assertIsNotNone(match.participants[0].base_store)
            trace.append("helper")
            helper_entered.set()
            await allow_terminal.wait()
            trace.append("store_bases:succeeded")
            return terminal_job("store_bases", "succeeded")

        match.get_pymongo_collection.return_value.update_one.side_effect = claim_reservation
        match.attach_base_store_message.side_effect = attach_message
        ctx.channel.send.side_effect = send_pending
        public_message.edit.side_effect = lambda **_kwargs: trace.append("success_message")
        ctx.send.side_effect = lambda *_args, **_kwargs: trace.append("success_response")
        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands, "sync_base_store_slots", new=AsyncMock(side_effect=self.slot_map)),
                patch_sites(('elobot.extensions.match.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(side_effect=choose)),
                patch.object(
                    helper_base_store,
                    "store_bases_interactively",
                    new=AsyncMock(side_effect=run_helper),
                ) as store,
        ):
            # When
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self.cog.store_self_bases, ctx, match, 0)
                    await helper_entered.wait()
                    ctx.channel.send.assert_not_awaited()
                    allow_terminal.set()

        self.assertEqual(trace, ["choose", "persist", "helper", "store_bases:succeeded",
                                 "pending_message", "persist_message", "success_message", "success_response"])
        self.assertEqual(match.participants[0].base_store.clan_tag, SECOND_CLAN_TAG)
        self.assertEqual(match.participants[0].base_store.slot, "Slot 1")
        runtime, interaction_ctx, payload = store.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(interaction_ctx, ctx)
        self.assertEqual(payload.model_dump(), {
            "host_clan_tag": SECOND_CLAN_TAG,
            "base_links": BASE_LINKS,
            "slot": 1,
        })
        public_send = ctx.channel.send.await_args
        self.assertEqual(public_send.kwargs["embed"].title, "**Base Storage**")
        self.assertEqual(public_send.kwargs["components"], [])
        public_edit = ctx.channel.send.return_value.edit.await_args
        self.assertEqual(public_edit.kwargs["embed"]["title"], "**Bases Stored**")
        nonce = match.participants[0].base_store.reservation_id
        self.assertTrue(nonce)
        self.assertEqual(
            [component.custom_id for component in public_edit.kwargs["components"]],
            [f"helper_stored_start|{TOKEN}|0|{nonce}", f"helper_stored_undo|{TOKEN}|0|{nonce}"],
        )
        self.assertEqual(
            [field["value"] for field in public_edit.kwargs["embed"]["fields"]],
            [f"**[Player {PLAYER_ID}](<https://discordapp.com/users/{PLAYER_ID}>)**",
             f"**[Player {self.bot.user.id}](<https://discordapp.com/users/{self.bot.user.id}>)**",
             f"[{self.clans[SECOND_CLAN_TAG].name} ({SECOND_CLAN_TAG})]"
             f"({self.clans[SECOND_CLAN_TAG].share_link})", "Slot 1"],
        )
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_repeated_store_restores_existing_undo_without_running_helper(self) -> None:
        match = self.make_match(stored=True)
        ctx = self.make_slash_context()
        availability = AsyncMock(return_value=True)
        present = AsyncMock(return_value=True)
        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(helper_base_store_availability, "confirm_store_bases_availability", new=availability),
                patch.object(self_service, "present_retained_base_store", new=present),
                patch.object(helper_base_store, "store_bases_interactively", new_callable=AsyncMock) as store,
        ):
            await self.cog.store_self_bases(ctx, match, 0)

        availability.assert_not_awaited()
        store.assert_not_awaited()
        present.assert_awaited_once()
        recovered = present.await_args.args[1]
        self.assertIs(recovered.match, match)
        self.assertEqual(recovered.player, 0)

    async def test_failed_repeated_store_recovery_does_not_claim_undo_was_restored(self) -> None:
        match = self.make_match(stored=True)
        ctx = self.make_slash_context()

        async def fail_recovery(interaction_ctx, _presentation) -> bool:
            await interaction_ctx.send(str(ERROR_EMOJI), ephemeral=True)
            return False

        with patch.object(self_service, "present_retained_base_store", new=AsyncMock(side_effect=fail_recovery)), \
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)):
            await self.cog.store_self_bases(ctx, match, 0)

        ctx.send.assert_awaited_once_with(str(ERROR_EMOJI), ephemeral=True)

    async def test_remote_store_success_with_missing_reservation_reports_failure(self) -> None:
        # Given: a reservation disappears while Helper is copying the bases.
        match = self.make_match()
        ctx = self.make_slash_context()

        async def complete_with_lost_reservation(*_args) -> stored_bases.HelperStoredBaseTerminalJob:
            match.participants[0].base_store = None
            match.participants[0].opponent_bases_stored = False
            return terminal_job("store_bases", "succeeded")

        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands, "sync_base_store_slots", new=AsyncMock(side_effect=self.slot_map)),
                patch_sites(('elobot.extensions.match.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(return_value=match)),
                patch.object(helper_base_store, "store_bases_interactively",
                             new=AsyncMock(side_effect=complete_with_lost_reservation)),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock) as clear,
        ):
            # When: the command attempts to publish the Helper success.
            await self.cog.store_self_bases(ctx, match, 0)

        # Then: remote success cannot become a false stored-base success for the user.
        public_edit = ctx.channel.send.return_value.edit.await_args
        self.assertEqual(public_edit.kwargs["components"], [])
        payload = public_edit.kwargs["embeds"][0]
        self.assertEqual(payload["title"] if isinstance(payload, dict) else payload.title, "**Base Storage Unavailable**")
        self.assertFalse(payload.get("description") if isinstance(payload, dict) else payload.description)
        self.assertIn(str(ERROR_EMOJI), ctx.send.await_args.args[0])
        self.assertNotIn(str(SUCCESS_EMOJI), ctx.send.await_args.args[0])
        clear.assert_not_awaited()

    async def test_unavailable_helper_stops_before_reserving_a_base_store_slot(self) -> None:
        # Given
        match = self.make_match()
        ctx = self.make_slash_context()
        availability = AsyncMock(return_value=False)
        preparation = AsyncMock(return_value=self_service.BaseStorePreparationFailure("unavailable"))

        with (
                patch.object(
                    helper_base_store_availability,
                    "confirm_store_bases_availability",
                    new=availability,
                ),
                patch.object(self_service, "prepare_base_store", new=preparation),
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
        ):
            # When
            await self.cog.store_self_bases(ctx, match, 0)

        # Then
        availability.assert_awaited_once_with(self.cog, ctx)
        preparation.assert_not_awaited()

    async def test_unsuccessful_store_outcomes_preserve_slot_with_undo(self) -> None:
        outcomes = [
            terminal_job("store_bases", "failed"),
            terminal_job("store_bases", "cancelled"),
            HelperJobCreateRejected(error="slot occupied"),
            HelperJobCreateAcceptedInvalid(),
            HelperJobCreateUnavailable(),
            HelperTrackingFailure(job_id="0123456789abcdef0123456789abcdef", reason="lost"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                # Given
                match = self.make_match()
                ctx = self.make_slash_context()
                with (
                        patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                        patch.object(match_commands, "sync_base_store_slots",
                                     new=AsyncMock(side_effect=self.slot_map)),
                        patch_sites(('elobot.extensions.match.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(return_value=match)),
                        patch.object(
                            helper_base_store,
                            "store_bases_interactively",
                            new=AsyncMock(return_value=outcome),
                        ),
                        patch.object(match_commands, "clear_participant_base_store",
                                     new_callable=AsyncMock) as clear_store,
                ):
                    # When
                    await self.cog.store_self_bases(ctx, match, 0)

                # Then
                clear_store.assert_not_awaited()
                self.assert_incomplete_storage(match, ctx)

    async def test_automatic_start_waits_for_helper_success_and_uses_exact_request(self) -> None:
        # Given
        match = self.make_match(stored=True)
        ctx = self.make_component_context("start")
        helper_entered = anyio.Event()
        allow_terminal = anyio.Event()

        async def run_helper(_runtime, interaction_ctx, _payload):
            self.assertIs(interaction_ctx, ctx)
            helper_entered.set()
            await allow_terminal.wait()
            return terminal_job("host_war_match_on_stored", "succeeded")

        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands.Player, "find_one", new=AsyncMock(return_value=MagicMock(tag="#PLAYER"))),
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=True)),
                patch.object(match_commands, "confirm_pending_opponent_bases", new=AsyncMock(return_value=True)),
                patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                             new=AsyncMock(side_effect=run_helper)) as helper_start,
                patch.object(war_start, "start_war", new=AsyncMock(return_value=MagicMock())) as local_start,
        ):
            # When
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self.cog.helper_stored_start_button.callback.func, self.cog, ctx)
                    await helper_entered.wait()
                    local_start.assert_not_awaited()
                    allow_terminal.set()

        runtime, interaction_ctx, payload = helper_start.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(interaction_ctx, ctx)
        self.assertEqual(payload.model_dump(), {
            "host_clan_tag": FIRST_CLAN_TAG,
            "player_tag": "#PLAYER",
            "districts": DISTRICTS,
            "slot": 2,
            "remove_bases_after": False,
        })
        bot, start_ctx, request = local_start.await_args.args
        self.assertIs(bot, self.bot)
        self.assertIs(start_ctx, ctx)
        self.assertIs(request.match, match)
        self.assertEqual(request.player, 0)
        self.assertIs(request.host, self.bot.user)
        self.assertIsInstance(request.reply, war_start.StoredWarStartReply)
        self.assertTrue(request.reply.helper_hosted)
        self.assertEqual(request.reply.slot, "Slot 2")

    async def test_start_checks_helper_availability_before_reserving_action(self) -> None:
        # Given
        ctx = self.make_component_context("start")
        availability = AsyncMock(return_value=False)
        reserve_side = AsyncMock(return_value=None)
        with (
                patch.object(
                    helper_base_store_availability,
                    "confirm_host_war_match_on_stored_availability",
                    new=availability,
                ),
                patch.object(self.cog, "_base_store_try_reserve_side", new=reserve_side),
        ):
            # When
            await self.cog.helper_stored_start_button.callback.func(self.cog, ctx)

        # Then
        availability.assert_awaited_once_with(self.cog, ctx)
        reserve_side.assert_not_awaited()

    async def test_automatic_start_remote_failures_preserve_unstarted_state_and_slot(self) -> None:
        outcomes = [
            terminal_job("host_war_match_on_stored", "failed"),
            terminal_job("host_war_match_on_stored", "cancelled"),
            HelperJobCreateAcceptedInvalid(),
            HelperTrackingFailure(job_id="0123456789abcdef0123456789abcdef", reason="lost"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                # Given
                resources_module.RESOURCE_CLAN_LOCKS.clear()
                resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
                match = self.make_match(stored=True)
                ctx = self.make_component_context("start")
                with (
                        patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                        patch.object(match_commands.Player, "find_one",
                                     new=AsyncMock(return_value=MagicMock(tag="#PLAYER"))),
                        patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                        patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=True)),
                        patch.object(match_commands, "confirm_pending_opponent_bases",
                                     new=AsyncMock(return_value=True)),
                        patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                                     new=AsyncMock(return_value=outcome)),
                        patch.object(war_start, "start_war", new_callable=AsyncMock) as local_start,
                        patch.object(match_commands, "clear_participant_base_store",
                                     new_callable=AsyncMock) as clear_store,
                ):
                    # When
                    await self.cog.helper_stored_start_button.callback.func(self.cog, ctx)

                # Then
                local_start.assert_not_awaited()
                clear_store.assert_not_awaited()
                self.assertEqual(match.times, [None, None])
                self.assertEqual(match.hosts, [None, None])
                self.assertIsNotNone(match.participants[0].base_store)
                ctx.message.delete.assert_not_awaited()

    async def assert_failed_local_start_retains_slot(
            self, failure: PyMongoError | None, *, readiness_failure: bool = False, edit_failure: bool = False,
    ) -> None:
        # Given
        match = self.make_match(stored=True)
        ctx = self.make_component_context("start")
        store = match.participants[0].base_store
        store.reservation_id, store.message_id, store.ready = "HELD", ctx.message.id, True
        ctx.custom_id += "|HELD"
        expected_store = store.model_dump()
        expected_store["ready"] = readiness_failure
        if readiness_failure:
            match.attach_base_store_message = AsyncMock(side_effect=PyMongoError("readiness write failed"))
        if edit_failure:
            ctx.message.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503))
        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands.Player, "find_one", new=AsyncMock(return_value=MagicMock(tag="#PLAYER"))),
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=True)),
                patch.object(match_commands, "confirm_pending_opponent_bases", new=AsyncMock(return_value=True)),
                patch.object(helper_base_store, "host_war_match_on_stored_interactively", new=AsyncMock(
                    return_value=terminal_job("host_war_match_on_stored", "succeeded"),
                )),
                patch.object(war_start, "start_war", new=AsyncMock(return_value=None, side_effect=failure)),
                patch.object(match_commands, "clear_participant_base_store",
                             new_callable=AsyncMock) as clear_store,
        ):
            # When
            await self.cog.helper_stored_start_button.callback.func(self.cog, ctx)

        # Then
        clear_store.assert_not_awaited()
        self.assertIs(match.participants[0].base_store, store)
        self.assertEqual(store.model_dump(), expected_store)
        self.assertTrue(match.participants[0].opponent_bases_stored)
        edit = ctx.message.edit.await_args.kwargs
        self.assertEqual(edit["embeds"][0].title, "**Match Start Incomplete**")
        self.assertEqual([button.custom_id for button in edit["components"]],
                         [] if readiness_failure else ["helper_stored_undo|SELFSTORE|0|HELD"])
        ctx.message.delete.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_automatic_start_retains_slot_when_local_commit_is_declined(self) -> None:
        await self.assert_failed_local_start_retains_slot(None)

    async def test_automatic_start_retains_slot_when_local_commit_raises(self) -> None:
        await self.assert_failed_local_start_retains_slot(PyMongoError("local commit failed"))

    async def test_failed_start_retains_slot_when_readiness_write_fails(self) -> None:
        with self.assertLogs(level="ERROR"):
            await self.assert_failed_local_start_retains_slot(None, readiness_failure=True)

    async def test_failed_start_still_reports_held_slot_when_message_edit_fails(self) -> None:
        with self.assertLogs(level="ERROR"):
            await self.assert_failed_local_start_retains_slot(None, edit_failure=True)

    async def test_automatic_start_preserves_committed_state_when_followup_raises(self) -> None:
        # Given
        match = self.make_match(stored=True)
        ctx = self.make_component_context("start")

        async def committed_failure(*_args):
            match.times[0] = 1000
            match.hosts[0] = str(self.bot.user.id)
            raise DiscordFollowupFailure("discord follow-up failed")

        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands.Player, "find_one", new=AsyncMock(return_value=MagicMock(tag="#PLAYER"))),
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=True)),
                patch.object(match_commands, "confirm_pending_opponent_bases", new=AsyncMock(return_value=True)),
                patch.object(helper_base_store, "host_war_match_on_stored_interactively", new=AsyncMock(
                    return_value=terminal_job("host_war_match_on_stored", "succeeded"),
                )),
                patch.object(war_start, "start_war", new=AsyncMock(side_effect=committed_failure)),
                patch.object(match_commands, "clear_participant_base_store",
                             new_callable=AsyncMock) as clear_store,
        ):
            # When / Then
            with self.assertRaisesRegex(DiscordFollowupFailure, "discord follow-up failed"):
                await self.cog.helper_stored_start_button.callback.func(self.cog, ctx)

        clear_store.assert_not_awaited()
        self.assertEqual(match.times[0], 1000)

    async def test_automatic_undo_waits_for_success_before_clear_and_retirement(self) -> None:
        # Given
        match = self.make_match(stored=True)
        ctx = self.make_component_context("undo")
        helper_entered = anyio.Event()
        allow_terminal = anyio.Event()

        async def run_helper(_runtime, interaction_ctx, _payload):
            self.assertIs(interaction_ctx, ctx)
            helper_entered.set()
            await allow_terminal.wait()
            return terminal_job("remove_bases", "succeeded")

        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(helper_base_store, "remove_bases_interactively",
                             new=AsyncMock(side_effect=run_helper)) as remove,
                patch.object(match_commands, "clear_participant_base_store",
                             new=AsyncMock(return_value=True)) as clear_store,
        ):
            # When
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self.cog.helper_stored_undo_button.callback.func, self.cog, ctx)
                    await helper_entered.wait()
                    clear_store.assert_not_awaited()
                    ctx.message.delete.assert_not_awaited()
                    allow_terminal.set()

        clear_store.assert_awaited_once_with(
            match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
        )
        ctx.message.delete.assert_not_awaited()
        self.assertEqual(ctx.message.edit.await_args.kwargs["components"], [])
        runtime, interaction_ctx, payload = remove.await_args.args
        self.assertIs(runtime, self.cog)
        self.assertIs(interaction_ctx, ctx)
        self.assertEqual(payload.model_dump(), {
            "host_clan_tag": FIRST_CLAN_TAG,
            "districts": DISTRICTS,
            "slot": 2,
        })

    async def test_undo_checks_helper_availability_before_reserving_action(self) -> None:
        # Given
        ctx = self.make_component_context("undo")
        availability = AsyncMock(return_value=False)
        reserve_side = AsyncMock(return_value=None)
        with (
                patch.object(
                    helper_base_store_availability,
                    "confirm_remove_bases_availability",
                    new=availability,
                ),
                patch.object(self.cog, "_base_store_try_reserve_side", new=reserve_side),
        ):
            # When
            await self.cog.helper_stored_undo_button.callback.func(self.cog, ctx)

        # Then
        availability.assert_awaited_once_with(self.cog, ctx)
        reserve_side.assert_not_awaited()

    async def test_automatic_undo_non_success_preserves_message_and_slot(self) -> None:
        outcomes = [
            terminal_job("remove_bases", "failed"),
            terminal_job("remove_bases", "cancelled"),
            HelperJobCreateUnavailable(),
            HelperTrackingFailure(job_id="0123456789abcdef0123456789abcdef", reason="lost"),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                # Given
                resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
                match = self.make_match(stored=True)
                ctx = self.make_component_context("undo")
                with (
                        patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                        patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                        patch.object(helper_base_store, "remove_bases_interactively",
                                     new=AsyncMock(return_value=outcome)),
                        patch.object(match_commands, "clear_participant_base_store",
                                     new_callable=AsyncMock) as clear_store,
                ):
                    # When
                    await self.cog.helper_stored_undo_button.callback.func(self.cog, ctx)

                # Then
                clear_store.assert_not_awaited()
                ctx.message.delete.assert_not_awaited()
                self.assertIsNotNone(match.participants[0].base_store)

    async def test_concurrent_start_and_undo_create_at_most_one_helper_job(self) -> None:
        # Given
        match = self.make_match(stored=True)
        start_ctx = self.make_component_context("start")
        undo_ctx = self.make_component_context("undo")
        helper_entered = anyio.Event()
        allow_terminal = anyio.Event()

        async def run_helper(_runtime, _interaction_ctx, _payload):
            helper_entered.set()
            await allow_terminal.wait()
            return terminal_job("host_war_match_on_stored", "failed")

        async def run_remove(_runtime, _interaction_ctx, _payload):
            helper_entered.set()
            await allow_terminal.wait()
            return terminal_job("remove_bases", "failed")

        with (
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)),
                patch.object(match_commands.Player, "find_one", new=AsyncMock(return_value=MagicMock(tag="#PLAYER"))),
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=True)),
                patch.object(match_commands, "confirm_pending_opponent_bases", new=AsyncMock(return_value=True)),
                patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                             new=AsyncMock(side_effect=run_helper)) as helper_start,
                patch.object(helper_base_store, "remove_bases_interactively",
                             new=AsyncMock(side_effect=run_remove)) as helper_undo,
                patch.object(war_start, "start_war", new_callable=AsyncMock),
        ):
            # When
            with anyio.fail_after(5):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self.cog.helper_stored_start_button.callback.func, self.cog, start_ctx)
                    await helper_entered.wait()
                    await self.cog.helper_stored_undo_button.callback.func(self.cog, undo_ctx)
                    helper_undo.assert_not_awaited()
                    allow_terminal.set()

        # Then
        self.assertEqual(helper_start.await_count + helper_undo.await_count, 1)

    async def test_legacy_manual_callbacks_never_call_stored_base_helper_jobs(self) -> None:
        # Given
        start_ctx = self.make_component_context("start")
        start_ctx.custom_id = f"start_match|0|{FIRST_CLAN_TAG}|Slot 2"
        undo_ctx = self.make_component_context("undo")
        undo_ctx.custom_id = f"undo_base_storage|{TOKEN}|0"
        match = self.make_match(stored=True)

        with (
                patch.object(self.cog, "_start_war", new_callable=AsyncMock) as legacy_start,
                patch_sites(('elobot.extensions.match.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
                patch_sites(('elobot.extensions.match.is_match_finished', 'elobot.features.matches.occupancy.is_match_finished'), return_value=False),
                patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock),
                patch.object(stored_bases, "store_bases", new_callable=AsyncMock) as helper_store,
                patch.object(stored_bases, "host_war_match_on_stored", new_callable=AsyncMock) as helper_start,
                patch.object(stored_bases, "remove_bases", new_callable=AsyncMock) as helper_remove,
                patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                             new_callable=AsyncMock) as interactive_start,
                patch.object(helper_base_store, "remove_bases_interactively",
                             new_callable=AsyncMock) as interactive_remove,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            # When
            await self.cog.start_match_button.callback.func(self.cog, start_ctx)
            await self.cog.undo_base_storage_button.callback.func(self.cog, undo_ctx)

        # Then
        legacy_start.assert_awaited_once_with(
            start_ctx, 0, FIRST_CLAN_TAG, host=start_ctx.author, slot="Slot 2", stored_reservation_id=None,
        )
        helper_store.assert_not_awaited()
        helper_start.assert_not_awaited()
        helper_remove.assert_not_awaited()
        interactive_start.assert_not_awaited()
        interactive_remove.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

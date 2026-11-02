import unittest
from collections.abc import Awaitable, Callable
from typing import Literal, assert_never
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import anyio
import interactions as ipy

import cogs.war_hosting.helper_host as helper_host
import cogs.war_hosting.helper_host_job as helper_host_job
from cogs.war_hosting.helper_api import HelperJob, HelperJobCreated, HelperJobEvent


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
type ProgressUpdater = Callable[[HelperJob], Awaitable[None]]


class TestSelfHostCancelVisibility(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime = MagicMock()
        self.runtime.helper_api_base_url = "https://helper.invalid"
        self.runtime.helper_api_key = "test-key"
        self.runtime.bot = MagicMock()
        self.runtime.bot.coc_client = MagicMock()
        self.runtime.bot.fetch_user = AsyncMock(return_value=MagicMock(id=200))

        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author = MagicMock(id=REQUESTER_ID)
        self.progress_message = MagicMock(spec=ipy.Message)
        self.progress_message.id = 888
        self.ctx.send = AsyncMock(return_value=self.progress_message)
        self.ctx.edit = AsyncMock()

        caller = MagicMock(user_id=REQUESTER_ID)
        opponent = MagicMock(user_id="200", bases=[MagicMock(link="https://base.one")])
        self.match = MagicMock(
            participants=[caller, opponent],
            token="match-token",
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
        )
        self.request = helper_host.AutomaticWarHost(
            match=self.match,
            player=0,
            clan=MagicMock(tag="#2RY0QP8GY", name="Helper Clan"),
            host=MagicMock(id=300),
        )
        self.poll = AsyncMock()
        self.read = AsyncMock()
        self.cancel = AsyncMock()
        self.start_war = AsyncMock(return_value=MagicMock(id=777))
        self.abort_war_start = AsyncMock(return_value=True)
        self.never_clicked = anyio.Event()
        self.runtime.bot.wait_for_component = AsyncMock(side_effect=self.wait_forever)

    async def wait_forever(self, *_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await self.never_clicked.wait()

    @staticmethod
    def job(status: JobStatus, messages: list[str]) -> HelperJob:
        match status:
            case "queued":
                started_at = None
                finished_at = None
            case "running":
                started_at = 101.0
                finished_at = None
            case "succeeded" | "failed" | "cancelled":
                started_at = 101.0
                finished_at = 102.0
            case unexpected:
                assert_never(unexpected)
        return HelperJob(
            id=JOB_ID,
            job_type="host_war_match",
            status=status,
            created_at=100.0,
            started_at=started_at,
            finished_at=finished_at,
            events=[HelperJobEvent(t=101.0 + index, message=message) for index, message in enumerate(messages)],
            result=None,
            error=None,
        )

    @staticmethod
    def component_context() -> ipy.ComponentContext:
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()
        return component_ctx

    async def invoke(self) -> None:
        with patch.object(helper_host, "confirm_helper_availability", new=AsyncMock(return_value=True)), \
                patch.object(helper_host, "get_available_helper_slot", new=AsyncMock(return_value=1)), \
                patch.object(helper_host, "Player") as player_model, \
                patch.object(
                    helper_host.match_commands,
                    "choose_bases",
                    new=AsyncMock(return_value=self.match),
                ), \
                patch.object(
                    helper_host_job.HelperJobCreator,
                    "create",
                    new=AsyncMock(return_value=HelperJobCreated(id=JOB_ID)),
                ), \
                patch.object(helper_host_job.HelperJobPoller, "wait", new=self.poll), \
                patch.object(helper_host_job.HelperJobPoller, "read", new=self.read), \
                patch.object(helper_host_job.HelperJobCanceller, "cancel", new=self.cancel), \
                patch.object(helper_host.war_start, "start_war", new=self.start_war), \
                patch.object(helper_host.war_start, "abort_war_start", new=self.abort_war_start):
            player_model.find_one = AsyncMock(return_value=MagicMock(tag="#CALLER"))
            await helper_host.host_war_automatically(self.runtime, self.ctx, self.request)

    async def test_cancel_stays_visible_until_first_district_setup(self) -> None:
        # Given
        updates = [
            self.job("queued", []),
            self.job("running", []),
            self.job("running", ["Setting up bases…"]),
            self.job(
                "running",
                ["Setting up bases…", "Setting up Barbarian Camp (1 of 3)…"],
            ),
            self.job("succeeded", ["All bases are ready"]),
        ]

        async def show_updates(
                _session: aiohttp.ClientSession,
                _job_id: str,
                update_progress: ProgressUpdater,
        ) -> HelperJob:
            for current_job in updates:
                await update_progress(current_job)
            return updates[-1]

        self.poll.side_effect = show_updates

        # When
        await self.invoke()

        # Then
        cancel_button = self.ctx.send.await_args.kwargs["components"]
        component_updates = [call.kwargs["components"] for call in self.ctx.edit.await_args_list]
        self.assertIs(component_updates[0], cancel_button)
        self.assertIs(component_updates[1], cancel_button)
        self.assertIs(component_updates[2], cancel_button)
        self.assertListEqual(component_updates[3], [])
        self.assertListEqual(component_updates[4], [])

    async def test_later_running_update_does_not_restore_cancel(self) -> None:
        # Given
        updates = [
            self.job("running", ["Setting up Wizard Valley (1 of 3)..."]),
            self.job("running", ["Setting up Balloon Lagoon (2 of 3)…"]),
            self.job("succeeded", []),
        ]

        async def show_updates(
                _session: aiohttp.ClientSession,
                _job_id: str,
                update_progress: ProgressUpdater,
        ) -> HelperJob:
            for current_job in updates:
                await update_progress(current_job)
            return updates[-1]

        self.poll.side_effect = show_updates

        # When
        await self.invoke()

        # Then
        cancel_button = self.ctx.send.await_args.kwargs["components"]
        self.assertIsNotNone(cancel_button)
        component_updates = [call.kwargs["components"] for call in self.ctx.edit.await_args_list]
        self.assertListEqual(component_updates[0], [])
        self.assertListEqual(component_updates[1], [])
        self.assertListEqual(component_updates[2], [])

    async def test_stale_requester_click_after_cutoff_does_not_cancel(self) -> None:
        # Given
        cutoff_applied = anyio.Event()
        stale_click_removed = anyio.Event()
        component_ctx = self.component_context()
        order: list[str] = []

        async def remove_stale_click(*_args, **_kwargs) -> None:
            order.append("stale click removed")
            stale_click_removed.set()

        component_ctx.edit_origin.side_effect = remove_stale_click
        click_count = 0

        async def deliver_stale_click(*_args, check, **_kwargs) -> MagicMock:
            nonlocal click_count
            click_count += 1
            if click_count == 1:
                with anyio.fail_after(5):
                    await cutoff_applied.wait()
                interaction = MagicMock(ctx=component_ctx)
                self.assertTrue(check(interaction))
                return interaction
            with anyio.fail_after(5):
                await anyio.Event().wait()
            raise AssertionError("unreachable")

        self.runtime.bot.wait_for_component.side_effect = deliver_stale_click

        async def cross_cutoff(
                _session: aiohttp.ClientSession,
                _job_id: str,
                update_progress: ProgressUpdater,
        ) -> HelperJob:
            await update_progress(self.job("running", ["Setting up Builder's Workshop (1 of 2)"]))
            order.append("cutoff applied")
            cutoff_applied.set()
            with anyio.fail_after(5):
                await stale_click_removed.wait()
            order.append("terminal outcome")
            return self.job("succeeded", [])

        self.poll.side_effect = cross_cutoff

        # When
        await self.invoke()

        # Then
        self.cancel.assert_not_awaited()
        component_ctx.edit_origin.assert_awaited_once_with(components=[])
        self.assertEqual(order, ["cutoff applied", "stale click removed", "terminal outcome"])

    async def test_requester_click_before_cutoff_preserves_accepted_cancellation(self) -> None:
        # Given
        monitor_started = anyio.Event()
        component_ctx = self.component_context()
        order: list[str] = []

        async def monitor(
                _session: aiohttp.ClientSession,
                _job_id: str,
                _update_progress: ProgressUpdater,
        ) -> HelperJob:
            order.append("monitor started")
            monitor_started.set()
            with anyio.fail_after(5):
                await anyio.Event().wait()
            raise AssertionError("unreachable")

        async def deliver_click(*_args, check, **_kwargs) -> MagicMock:
            with anyio.fail_after(5):
                await monitor_started.wait()
            interaction = MagicMock(ctx=component_ctx)
            self.assertTrue(check(interaction))
            return interaction

        async def accept_cancel(*_args, **_kwargs) -> None:
            order.append("cancel posted")

        async def abort(*_args, **_kwargs) -> bool:
            order.append("match aborted")
            return True

        self.poll.side_effect = monitor
        self.read.return_value = self.job("cancelled", ["Cancellation confirmed"])
        self.runtime.bot.wait_for_component.side_effect = deliver_click
        self.cancel.side_effect = accept_cancel
        self.abort_war_start.side_effect = abort

        # When
        await self.invoke()

        # Then
        cancel_button = self.ctx.send.await_args.kwargs["components"]
        self.cancel.assert_awaited_once()
        self.assertEqual(self.cancel.await_args.args[1], JOB_ID)
        component_ctx.edit_origin.assert_awaited_once_with(components=cancel_button)
        self.assertTrue(cancel_button.disabled)
        self.abort_war_start.assert_awaited_once()
        self.assertEqual(self.ctx.edit.await_args.kwargs["components"], [])
        self.assertEqual(order, ["monitor started", "cancel posted", "match aborted"])


if __name__ == "__main__":
    unittest.main()

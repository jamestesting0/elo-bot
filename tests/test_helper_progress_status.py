import unittest
from unittest.mock import AsyncMock

import anyio

from elobot.features.hosting.war import helper_reinvite
from elobot.features.hosting.war import helper_host_job
from elobot.integrations.helper_api import HelperHostWarMatchPayload, HelperJobCreateRequest
from elobot.config.emojis import LOADING_EMOJI, SUCCESS_EMOJI
from tests import _helper_stored_bases_fixture as http_fixture


class TestHelperProgressStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.fixture = http_fixture.StoredBaseJobState(self)
        await self.fixture.asyncSetUp()
        self.ctx, _ = self.fixture.slash_context()

        async def no_cancel(**_kwargs) -> None:
            with anyio.fail_after(5):
                await anyio.sleep_forever()

        self.fixture.runtime.bot.wait_for_component = AsyncMock(side_effect=no_cancel)

    async def asyncTearDown(self) -> None:
        await self.fixture.asyncTearDown()

    def responses(self, job_type: str) -> None:
        for status in ("running", "succeeded"):
            job = self.fixture.job_document("store_bases", status)
            job["type"] = job_type
            self.fixture.status_responses.append((200, job))

    def assert_pending_then_success(self) -> None:
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(self.ctx.edit.await_args_list[0].kwargs["content"].startswith(str(LOADING_EMOJI)))
        self.assertTrue(self.ctx.edit.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))

    async def test_hosting_progress_does_not_announce_success_while_pending(self) -> None:
        # Given
        self.responses("host_war_match")
        request = helper_host_job.HelperHostJobRequest(
            create_request=HelperJobCreateRequest(
                job_type="host_war_match", requested_by=http_fixture.HUMAN_ID,
                payload=HelperHostWarMatchPayload(
                    host_clan_tag="#CLAN", player_tag="#PLAYER", base_links=["https://example.com/base"], slot=1,
                ),
            ),
            requester_id=http_fixture.HUMAN_ID,
        )
        # When
        await helper_host_job.run_helper_host_job(self.fixture.runtime, self.ctx, request)
        # Then
        self.assert_pending_then_success()

    async def test_reinvite_progress_does_not_announce_success_while_pending(self) -> None:
        # Given
        self.responses("kick_and_reinvite")
        # When
        await helper_reinvite.kick_and_reinvite(
            self.fixture.runtime, self.ctx,
            helper_reinvite.HelperReinviteRequest(http_fixture.HUMAN_ID, "#CLAN", "#PLAYER"),
        )
        # Then
        self.assert_pending_then_success()

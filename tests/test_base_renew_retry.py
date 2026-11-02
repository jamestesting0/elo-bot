import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.war_hosting import helper_preserve
from cogs.war_hosting.helper_api import (
    HelperJobCreateAcceptedInvalid,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperTrackingFailure,
)


class TestBaseRenewRetry(unittest.IsolatedAsyncioTestCase):
    async def test_unconfirmed_failure_or_cancellation_does_not_retry(self) -> None:
        # Given
        outcomes = [
            HelperJobCreateRejected(error="Invalid base link"),
            HelperJobCreateAcceptedInvalid(),
            HelperJobCreateUnavailable(),
            HelperTrackingFailure(job_id="job", reason="Status unavailable"),
            helper_preserve.HelperPreserveBaseLinkTerminalJob.model_validate({
                "id": "0123456789abcdef0123456789abcdef",
                "type": "preserve_base_link",
                "status": "cancelled",
                "created_at": 100.0,
                "started_at": 101.0,
                "finished_at": 102.0,
                "events": [],
                "result": None,
                "error": None,
            }),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                base = MagicMock(link="stored-link", expiration_date=100.0)
                base.save_changes = AsyncMock()
                runtime = MagicMock()
                runtime.send_base_renewal_log = AsyncMock()
                runtime.send_base_renewal_failure_log = AsyncMock()
                notify = AsyncMock()
                with patch.object(helper_preserve, "preserve_base_link", return_value=outcome) as preserve:
                    # When
                    result = await helper_preserve.preserve_and_renew_base(runtime, base, "123", notify)

                # Then
                self.assertIs(result, outcome)
                preserve.assert_awaited_once_with(runtime, "123", "stored-link", notify)
                self.assertEqual(base.expiration_date, 100.0)
                base.save_changes.assert_not_awaited()
                runtime.send_base_renewal_log.assert_not_awaited()
                runtime.send_base_renewal_failure_log.assert_awaited_once()

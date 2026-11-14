# noqa: SIZE_OK - score-store scenarios share one local HTTP server fixture
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

from cogs.war_hosting import result_store_ui
from elobot.integrations.helper_api import HelperJob, HelperEtaFailure
from cogs.war_hosting.confirmation import HelperQueueDecision
from elobot.config.ids import STAFF_ROLE
from elobot.config.emojis import STAR_EMOJI


from tests._helper_score_fixture import HelperScoreStateMixin

from tests._helper_score_fixture import JOB_ID, REQUESTER_ID

class TestHelperScoreStore(HelperScoreStateMixin, unittest.IsolatedAsyncioTestCase):
    @staticmethod
    async def wait_for_component_forever(*_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await anyio.sleep_forever()

    async def test_local_http_partial_failure_merges_trusted_scores(self) -> None:
        self.status_responses = [(
            200,
            self.job_document("failed", {
                "Wizard Valley": self.score(2, 85),
                "Balloon Lagoon": None,
                "Dragon Cliffs": self.score(1, 70, verified=False),
            }),
        )]

        ctx = self.make_context()
        outcome = await self.invoke(ctx, self.make_match())

        self.assertEqual(self.eta_types, ["fetch_score"])
        self.assertEqual(self.create_api_keys, ["test-key"])
        self.assertEqual(self.status_api_keys, ["test-key"])
        self.assertEqual(self.create_payloads, [{
            "type": "fetch_score",
            "requested_by": REQUESTER_ID,
            "payload": {
                "host_clan_tag": "#CLAN",
                "attacker_tag": "#PLAYER",
                "attacks_started_after": -200.0,
                "attacks_ended_before": 500.0,
                "districts": ["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"],
                "max_allowed_attacks": {
                    "Wizard Valley": 2,
                    "Balloon Lagoon": 2,
                    "Dragon Cliffs": 2,
                },
            },
        }])
        self.assertEqual(outcome.results, ("2.85", "2.80", "3.100"))
        self.assertTrue(outcome.changed)
        self.assertEqual(outcome.job_id, JOB_ID)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual([skip.district for skip in outcome.skipped], ["Balloon Lagoon", "Dragon Cliffs"])
        ctx.send.assert_awaited_once()
        progress_button = ctx.send.await_args.kwargs["components"]
        self.assertEqual(progress_button.custom_id, f"helper_score_cancel|{JOB_ID}")
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_relaxed_store_sends_latest_two_attack_limit_for_every_district(self) -> None:
        # Given
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": self.score(3, 100),
        }))]

        # When
        await self.invoke(
            self.make_context(),
            self.make_match(results=[None, None, None]),
            max_allowed_attacks=-2,
        )

        # Then
        payload = self.create_payloads[0]["payload"]
        self.assertEqual(payload["max_allowed_attacks"], {
            "Wizard Valley": -2,
            "Balloon Lagoon": -2,
            "Dragon Cliffs": -2,
        })

    async def test_result_uses_gained_fields_instead_of_attack_rows(self) -> None:
        # Given the API's gained result disagrees with the individual attack row
        score = self.score(2, 90)
        score["attacks"] = [self.attack(1, 1, 40)]
        self.status_responses = [(200, self.job_document("succeeded", {"Wizard Valley": score}))]

        # When the shared Helper score-store flow processes the result
        outcome = await self.invoke(
            self.make_context(),
            self.make_match(results=[None, None, None]),
        )

        # Then the API's gained fields supply the stored result
        self.assertEqual(outcome.results[0], "2.90")
        self.assertEqual(outcome.trusted, (("Wizard Valley", "2.90"),))

    async def test_missing_gained_stars_does_not_fall_back_to_attack_rows(self) -> None:
        # Given the API could not determine gained stars but an attack row contains stars
        score = self.score(None, 90)
        score["attacks"] = [self.attack(1, 3, 100)]
        self.status_responses = [(200, self.job_document("succeeded", {"Wizard Valley": score}))]

        # When the shared Helper score-store flow processes the result
        outcome = await self.invoke(
            self.make_context(),
            self.make_match(results=[None, None, None]),
        )

        # Then the district is skipped instead of deriving a result from the row
        self.assertIsNone(outcome.results[0])
        self.assertEqual(outcome.trusted, ())
        self.assertEqual(outcome.skipped[0].district, "Wizard Valley")

    async def test_relaxed_miner_violation_in_latest_selected_attack_is_detected(self) -> None:
        # Given Helper watched the latest two attacks and found a miner violation there
        score = self.score(3, 100)
        score["attacks"] = [
            self.attack(5, 3, 100, miner_count=4),
            self.attack(4, None, 95, miner_count=0),
            self.attack(3, None, 90, miner_count=None),
            self.attack(2, None, 70, miner_count=None),
            self.attack(1, None, 40, miner_count=None),
        ]
        self.status_responses = [(200, self.job_document("succeeded", {"Wizard Valley": score}))]
        ctx = self.make_context()

        # When the relaxed Helper score-store flow processes the result
        outcome = await self.invoke(
            ctx,
            self.make_match(results=[None, None, None]),
            max_allowed_attacks=-2,
        )

        # Then the server-selected miner violation blocks storage and alerts the thread
        self.assertEqual(outcome.trusted, ())
        ctx.channel.send.assert_awaited_once()

    async def test_end_time_must_be_after_persisted_start_time(self) -> None:
        ctx = self.make_context()

        outcome = await self.invoke(ctx, self.make_match(start_time=200), now=200.0)

        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.job_id)
        self.assertEqual(self.create_payloads, [])
        self.assertTrue(ctx.send.await_args.args[0].startswith("<:error:"))

    async def test_score_job_queues_without_challenge_cancellation_consent(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": self.score(3, 100),
        }))]
        ctx = self.make_context()

        outcome = await self.invoke(ctx, self.make_match())

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(self.create_payloads), 1)
        ctx.send.assert_awaited_once()
        cancel_button = ctx.send.await_args.kwargs["components"]
        self.assertEqual(cancel_button.custom_id, f"helper_score_cancel|{JOB_ID}")

    async def test_nullable_success_keeps_existing_results(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": None,
            "Balloon Lagoon": None,
            "Dragon Cliffs": None,
        }))]

        outcome = await self.invoke(self.make_context(), self.make_match())

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.results, ("1.50", "2.80", "3.100"))

    async def test_unverified_score_never_overwrites_old_value(self) -> None:
        score = self.score(3, 100, same_attacker=False)
        score["attacks"] = [self.attack(1, 3, 100, miner_count=5)]
        self.status_responses = [(200, self.job_document("succeeded", {"Wizard Valley": score}))]
        ctx = self.make_context()

        outcome = await self.invoke(ctx, self.make_match())

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.results[0], "1.50")
        self.assertIn("could not verify", outcome.skipped[0].reason)
        ctx.channel.send.assert_not_awaited()

    async def test_miner_limit_violation_rejects_every_score_and_alerts_the_match_thread(self) -> None:
        # Given
        wizard_score = self.score(3, 100)
        wizard_score["attacks"] = [
            self.attack(2, 3, 100, miner_count=4),
            self.attack(1, 2, 80, miner_count=2),
        ]
        dragon_score = self.score(3, 100)
        dragon_score["attacks"] = [self.attack(1, 1, 80, miner_count=5)]
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": wizard_score,
            "Balloon Lagoon": self.score(2, 90),
            "Dragon Cliffs": {
                **dragon_score,
                "stars_gained": 1,
                "destruction_gained": 80,
                "total_destruction_on_challenge": 80,
            },
        }))]
        ctx = self.make_context()
        match = self.make_match(results=[None, None, None])

        # When
        outcome = await self.invoke(ctx, match)

        # Then
        self.assertEqual(outcome.results, (None, None, None))
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.trusted, ())
        self.assertEqual(
            [skip.district for skip in outcome.skipped],
            ["Wizard Valley", "Dragon Cliffs"],
        )
        ctx.channel.send.assert_awaited_once()
        alert = ctx.channel.send.await_args.args[0]
        self.assertIn(f"<@&{STAFF_ROLE}>", alert)
        self.assertIn(f"<@{REQUESTER_ID}>", alert)
        self.assertIn("Wizard Valley", alert)
        self.assertIn("Dragon Cliffs", alert)
        self.assertIn(f"Wizard Valley: 3{STAR_EMOJI} 100%", alert)
        self.assertIn(f"Balloon Lagoon: 2{STAR_EMOJI} 90%", alert)
        self.assertIn(f"Dragon Cliffs: 1{STAR_EMOJI} 80%", alert)
        self.assertIn("\n\n**Results:**\n", alert)

    async def test_three_miners_in_an_attack_is_allowed(self) -> None:
        # Given
        wizard_score = self.score(3, 100)
        wizard_score["attacks"] = [
            self.attack(3, 3, 100, miner_count=-1),
            self.attack(2, 3, 100, miner_count=None),
            self.attack(1, 2, 80, miner_count=3),
        ]
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": wizard_score,
        }))]
        ctx = self.make_context()

        # When
        outcome = await self.invoke(ctx, self.make_match(results=[None, None, None]))

        # Then
        self.assertEqual(outcome.trusted, (("Wizard Valley", "3.100"),))
        ctx.channel.send.assert_not_awaited()

    async def test_invalid_score_never_overwrites_old_value(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": self.score(3, 99.5),
        }))]

        outcome = await self.invoke(self.make_context(), self.make_match())

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.results[0], "1.50")
        self.assertIn("invalid", outcome.skipped[0].reason)

    async def test_malformed_response_returns_unchanged_outcome(self) -> None:
        self.status_responses = [(200, "not-json")]

        outcome = await self.invoke(self.make_context(), self.make_match())

        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.status)
        self.assertIn("Could not monitor", outcome.summary)

    async def test_unexpected_job_type_never_changes_results(self) -> None:
        self.status_responses = [(200, {
            **self.job_document("succeeded"),
            "type": "host_war_match",
        })]

        outcome = await self.invoke(self.make_context(), self.make_match())

        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.status)
        self.assertIn("unexpected job", outcome.summary.lower())

    async def test_create_error_outcomes_are_reported_without_polling(self) -> None:
        for status, body, expected in [
            (400, {"error": "bad payload"}, "rejected"),
            (401, {}, "could not queue"),
            (500, {}, "could not queue"),
        ]:
            with self.subTest(status=status):
                self.create_status = status
                self.create_body = body
                outcome = await self.invoke(self.make_context(), self.make_match())
                self.assertFalse(outcome.changed)
                self.assertIsNone(outcome.job_id)
                self.assertIn(expected, outcome.summary.lower())

    async def test_status_error_outcomes_are_reported_without_merging(self) -> None:
        for status in (401, 404, 500):
            with self.subTest(status=status):
                self.status_responses = [(status, {})] * 3
                outcome = await self.invoke(self.make_context(), self.make_match())
                self.assertFalse(outcome.changed)
                self.assertEqual(outcome.job_id, JOB_ID)
                self.assertIsNone(outcome.status)

    async def test_busy_eta_requires_continue_before_creating_score_job(self) -> None:
        self.eta_start = 1000.0
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": self.score(3, 100),
        }))]
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.custom_id = HelperQueueDecision.CONTINUE
        component_ctx.edit_origin = AsyncMock()
        async def wait_for_components(*_args, components, **_kwargs) -> MagicMock:
            nested_components = getattr(components, "components", [components])
            custom_ids = {component.custom_id for component in nested_components}
            if HelperQueueDecision.CONTINUE in custom_ids:
                return MagicMock(ctx=component_ctx)
            with anyio.fail_after(5):
                await anyio.Event().wait()

        self.bot.wait_for_component.side_effect = wait_for_components

        outcome = await self.invoke(self.make_context(), self.make_match())

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(self.create_payloads), 1)
        self.assertGreaterEqual(component_ctx.edit_origin.await_count, 1)

    async def test_scheduled_store_skips_prompts_and_has_no_cancel_control(self) -> None:
        self.eta_start = 1000.0
        self.status_responses = [(200, self.job_document("succeeded", {
            "Wizard Valley": self.score(3, 100),
        }))]
        self.bot.wait_for_component.side_effect = self.wait_for_component_forever
        progress_message = MagicMock(spec=ipy.Message)
        progress_message.id = 444
        progress_message.edit = AsyncMock()
        channel = MagicMock(spec=ipy.ThreadChannel)
        channel.send = AsyncMock(return_value=progress_message)
        ctx = result_store_ui.ScheduledResultStoreContext(
            author=ipy.SnowflakeObject(int(REQUESTER_ID)),
            channel=channel,
        )

        outcome = await self.invoke(
            ctx,
            self.make_match(),
            interactive=False,
            allow_cancellation=False,
        )

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(len(self.create_payloads), 1)
        channel.send.assert_awaited_once()
        self.assertEqual(channel.send.await_args.kwargs["components"], [])
        self.bot.wait_for_component.assert_not_awaited()
        progress_message.edit.assert_awaited()

    async def test_requester_cancel_only_cancels_the_score_job(self) -> None:
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()
        async def wait_for_components(*_args, **_kwargs) -> MagicMock:
            return MagicMock(ctx=component_ctx)

        self.bot.wait_for_component.side_effect = wait_for_components

        for status in ("succeeded", "failed", "cancelled"):
            with self.subTest(status=status):
                pending = anyio.Event()
                self.cancel_body = self.job_document(status, {"Wizard Valley": self.score(2, 85)})
                ctx = self.make_context()

                async def record_progress(*_args, **_kwargs) -> None:
                    pending.set()

                ctx.edit.side_effect = record_progress

                async def poll_terminal(_session, _job_id, update_progress) -> HelperJob:
                    with anyio.fail_after(5):
                        await pending.wait()
                    job = HelperJob.model_validate(self.cancel_body)
                    await update_progress(job)
                    return job

                with patch("cogs.war_hosting.helper_store.HelperJobPoller.wait", new=AsyncMock(side_effect=poll_terminal)):
                    outcome = await self.invoke(ctx, self.make_match())

                self.assertEqual(outcome.status, status)
                self.assertEqual(outcome.job_id, JOB_ID)
                self.assertEqual(outcome.trusted, () if status == "cancelled" else (("Wizard Valley", "2.85"),))
        self.assertEqual(self.cancel_paths, [f"/v1/jobs/{JOB_ID}/cancel"] * 3)

    async def test_unavailable_eta_transport_returns_unchanged_outcome(self) -> None:
        ctx = self.make_context()
        with patch("cogs.war_hosting.helper_store.HelperEtaReader.read", new=AsyncMock(
            return_value=HelperEtaFailure(reason="offline"),
        )):
            outcome = await self.invoke(ctx, self.make_match())

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.results, ("1.50", "2.80", "3.100"))
        self.assertIsNone(outcome.job_id)
        self.assertIsNone(outcome.status)
        self.assertEqual(self.create_payloads, [])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.args[0].startswith("<:error:"))

    async def test_discord_edit_failure_during_polling_and_summary_does_not_fail_outcome(self) -> None:
        self.status_responses = [
            (200, self.job_document("running", {})),
            (200, self.job_document("succeeded", {"Wizard Valley": self.score(3, 100)})),
        ]
        ctx = self.make_context()
        ctx.edit.side_effect = ipy.errors.DiscordError(
            MagicMock(status=503),
            response_data="upstream connect error",
        )

        outcome = await self.invoke(ctx, self.make_match())

        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(outcome.results, ("3.100", "2.80", "3.100"))
        self.assertEqual(outcome.trusted, (("Wizard Valley", "3.100"),))

    async def test_discord_edit_failure_during_cancellation_does_not_abort_monitoring(self) -> None:
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin.side_effect = ipy.errors.DiscordError(
            MagicMock(status=503),
            response_data="upstream connect error",
        )
        component_ctx.send = AsyncMock()

        async def wait_for_components(*_args, **_kwargs) -> MagicMock:
            return MagicMock(ctx=component_ctx)

        self.bot.wait_for_component.side_effect = wait_for_components

        pending = anyio.Event()
        self.cancel_body = self.job_document("cancelled", {"Wizard Valley": self.score(2, 85)})
        ctx = self.make_context()

        async def record_progress(*_args, **_kwargs) -> None:
            pending.set()

        ctx.edit.side_effect = record_progress

        async def poll_terminal(_session, _job_id, update_progress) -> HelperJob:
            with anyio.fail_after(5):
                await pending.wait()
            job = HelperJob.model_validate(self.cancel_body)
            await update_progress(job)
            return job

        with patch("cogs.war_hosting.helper_store.HelperJobPoller.wait", new=AsyncMock(side_effect=poll_terminal)):
            outcome = await self.invoke(ctx, self.make_match())

        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(outcome.job_id, JOB_ID)
        self.assertEqual(self.cancel_paths, [f"/v1/jobs/{JOB_ID}/cancel"])


if __name__ == "__main__":
    unittest.main()

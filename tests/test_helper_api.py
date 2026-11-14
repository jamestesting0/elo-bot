import unittest
from typing import Literal

from pydantic import ValidationError

from elobot.integrations import helper_api
from elobot.integrations.helper_api import HelperJob, helper_job_notification


JOB_ID = "0123456789abcdef0123456789abcdef"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class TestHelperJobNotification(unittest.TestCase):
    @staticmethod
    def job_document(status: JobStatus) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": "host_war_match",
            "status": status,
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [{"t": 101.5, "message": "All bases are ready"}],
            "result": None,
            "error": None,
        }

    def test_failed_job_notification_includes_only_the_last_event(self) -> None:
        job = HelperJob.model_validate({
            "id": JOB_ID,
            "type": "host_war_match",
            "status": "failed",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [
                {"t": 101.0, "message": "Earlier event"},
                {"t": 101.5, "message": "Final failure event"},
            ],
            "result": None,
            "error": "Unable to invite the player",
        })

        notification = helper_job_notification(job)

        self.assertIn(job.events[-1].message, notification)
        self.assertNotIn(job.events[0].message, notification)

    def test_succeeded_job_accepts_null_result_as_the_documented_outcome(self) -> None:
        # Given
        job = HelperJob.model_validate(self.job_document("succeeded"))

        # When
        notification = helper_job_notification(job)

        # Then
        self.assertIn("succeeded", notification)
        self.assertNotIn("without a result", notification)
        self.assertNotIn("contact a helper", notification.lower())

    def test_cancelled_job_is_reported_as_a_clean_terminal_stop(self) -> None:
        # Given
        job = HelperJob.model_validate(self.job_document("cancelled"))

        # When
        notification = helper_job_notification(job)

        # Then
        self.assertIn("cancelled", notification)
        self.assertNotIn("failed", notification)

    def test_fetch_score_success_accepts_nullable_districts(self) -> None:
        # Given
        job = HelperJob.model_validate({
            "id": JOB_ID,
            "type": "fetch_score",
            "status": "succeeded",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [],
            "result": {
                "scores": {
                    "Wizard Valley": {
                        "attacker_name_verified": True,
                        "no_of_valid_attacks": 1,
                        "total_no_of_attacks": 1,
                        "same_attacker_for_all_attacks": True,
                        "stars_gained": 3,
                        "destruction_gained": 100,
                        "total_destruction_on_challenge": 100,
                        "attacks": [{
                            "attack_number": 1,
                            "stars": 3,
                            "destruction": 100,
                            "miner_count": 4,
                            "invalid_replay": False,
                        }],
                    },
                    "Balloon Lagoon": None,
                },
            },
            "error": None,
        })

        # When
        notification = helper_job_notification(job)

        # Then
        self.assertEqual(job.result.scores["Wizard Valley"].stars_gained, 3)
        self.assertEqual(job.result.scores["Wizard Valley"].total_no_of_attacks, 1)
        self.assertEqual(job.result.scores["Wizard Valley"].attacks[0].miner_count, 4)
        self.assertIsNone(job.result.scores["Balloon Lagoon"])
        self.assertIn("1 score", notification)
        self.assertIn("1 district without a score", notification)

    def test_failed_fetch_job_retains_partial_scores(self) -> None:
        # Given
        job = HelperJob.model_validate({
            "id": JOB_ID,
            "type": "fetch_score",
            "status": "failed",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [{"t": 101.5, "message": "Wizard Valley extracted"}],
            "result": {
                "scores": {
                    "Wizard Valley": {
                        "attacker_name_verified": True,
                        "no_of_valid_attacks": 1,
                        "total_no_of_attacks": None,
                        "same_attacker_for_all_attacks": True,
                        "stars_gained": None,
                        "destruction_gained": 97.5,
                        "total_destruction_on_challenge": 97.5,
                        "attacks": [{
                            "attack_number": 1,
                            "stars": None,
                            "destruction": 97.5,
                            "miner_count": -1,
                            "invalid_replay": False,
                        }],
                    },
                },
            },
            "error": "The device disconnected",
        })

        # When
        notification = helper_job_notification(job)

        # Then
        self.assertIsNone(job.result.scores["Wizard Valley"].stars_gained)
        self.assertIsNone(job.result.scores["Wizard Valley"].total_no_of_attacks)
        self.assertIn("failed", notification)
        self.assertIn("1 partial score", notification)


class TestHelperStoredBaseContract(unittest.TestCase):
    def test_store_bases_serializes_exact_request(self) -> None:
        # Given
        request = helper_api.HelperJobCreateRequest(
            job_type="store_bases",
            requested_by="123456789012345678",
            payload=helper_api.HelperStoreBasesPayload(
                host_clan_tag="#CLAN",
                base_links=["https://link.clashofclans.com/en?action=OpenLayout&id=one"],
                slot=1,
            ),
        )

        # When
        serialized = request.model_dump(by_alias=True)

        # Then
        self.assertEqual(serialized, {
            "type": "store_bases", "requested_by": "123456789012345678",
            "payload": {
                "host_clan_tag": "#CLAN",
                "base_links": ["https://link.clashofclans.com/en?action=OpenLayout&id=one"],
                "slot": 1,
            },
        })

    def test_host_on_stored_serializes_exact_request(self) -> None:
        # Given
        request = helper_api.HelperJobCreateRequest(
            job_type="host_war_match_on_stored",
            requested_by="123456789012345678",
            payload=helper_api.HelperHostWarMatchOnStoredPayload(
                host_clan_tag="#CLAN",
                player_tag="#PLAYER",
                districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
                slot=2,
                remove_bases_after=False,
            ),
        )

        # When
        serialized = request.model_dump(by_alias=True)

        # Then
        self.assertEqual(serialized, {
            "type": "host_war_match_on_stored", "requested_by": "123456789012345678",
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_tag": "#PLAYER",
                "districts": ["Barbarian Camp", "Wizard Valley", "Capital Peak"],
                "slot": 2, "remove_bases_after": False,
            },
        })

    def test_stored_hosting_rejects_automatic_base_removal(self) -> None:
        # Given
        payloads = (
            (helper_api.HelperHostWarMatchOnStoredPayload, {
                "host_clan_tag": "#CLAN", "player_tag": "#PLAYER",
                "districts": ["Wizard Valley"], "slot": 1, "remove_bases_after": True,
            }),
            (helper_api.HelperHostLiveMatchOnStoredPayload, {
                "host_clan_tag": "#CLAN", "player_1_tag": "#PLAYER1", "player_2_tag": "#PLAYER2",
                "districts": ["Wizard Valley"], "remove_bases_after": True,
            }),
        )
        for model, payload in payloads:
            with self.subTest(model=model.__name__):
                # When / Then
                with self.assertRaises(ValidationError) as error:
                    model.model_validate(payload)
                self.assertEqual(error.exception.errors()[0]["loc"], ("remove_bases_after",))

    def test_remove_bases_serializes_exact_request(self) -> None:
        # Given
        request = helper_api.HelperJobCreateRequest(
            job_type="remove_bases",
            requested_by="123456789012345678",
            payload=helper_api.HelperRemoveBasesPayload(
                host_clan_tag="#CLAN",
                districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
                slot=1,
            ),
        )

        # When
        serialized = request.model_dump(by_alias=True)

        # Then
        self.assertEqual(serialized, {
            "type": "remove_bases", "requested_by": "123456789012345678",
            "payload": {
                "host_clan_tag": "#CLAN", "slot": 1,
                "districts": ["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            },
        })

    def test_mismatched_job_type_and_payload_is_rejected(self) -> None:
        # Given
        payload = helper_api.HelperStoreBasesPayload(
            host_clan_tag="#CLAN",
            base_links=["https://link.clashofclans.com/en?action=OpenLayout&id=one"],
            slot=1,
        )

        # When / Then
        with self.assertRaises(ValidationError):
            helper_api.HelperJobCreateRequest(
                job_type="remove_bases",
                requested_by="123456789012345678",
                payload=payload,
            )


class TestHelperBaseImageContract(unittest.TestCase):
    def test_capture_request_serializes_privacy_and_image_options(self):
        for keep_secret in (False, True):
            with self.subTest(keep_secret=keep_secret):
                request = helper_api.HelperJobCreateRequest(
                    job_type="get_base_image", requested_by="123",
                    payload=helper_api.HelperGetBaseImagePayload(
                        base_link="https://link.clashofclans.com/base", keep_secret=keep_secret,
                    ),
                )
                self.assertEqual(request.model_dump(by_alias=True), {
                    "type": "get_base_image", "requested_by": "123",
                    "payload": {"base_link": "https://link.clashofclans.com/base",
                                "scout_view": False, "filetype": "webp", "keep_secret": keep_secret},
                })

    def test_capture_request_rejects_wrong_payload_and_capture_options(self):
        with self.assertRaises(ValidationError):
            helper_api.HelperJobCreateRequest(
                job_type="get_base_image", requested_by="123",
                payload=helper_api.HelperPreserveBaseLinkPayload(base_link="https://example/base"),
            )
        for options in ({"scout_view": True}, {"filetype": "png"}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                helper_api.HelperGetBaseImagePayload(base_link="https://example/base", keep_secret=True, **options)

    def test_capture_result_is_narrow_and_success_requires_an_artifact(self):
        document = TestHelperJobNotification.job_document("succeeded")
        document["type"] = "get_base_image"
        document["result"] = {"image": f"{JOB_ID}.webp"}
        job = HelperJob.model_validate(document)
        self.assertIsInstance(job.result, helper_api.HelperBaseImageResult)
        self.assertEqual(job.result.image, f"{JOB_ID}.webp")
        self.assertTrue(helper_job_notification(job))
        for result in (None, {"image": "../image.webp"}, {"image": f"{JOB_ID}.png"}, {"scores": {}}):
            with self.subTest(result=result), self.assertRaises(ValidationError):
                HelperJob.model_validate({**document, "result": result})
        with self.assertRaises(ValidationError):
            HelperJob.model_validate({**document, "type": "preserve_base_link"})

    def test_non_success_capture_states_accept_no_result(self):
        for status in ("queued", "running", "failed", "cancelled"):
            with self.subTest(status=status):
                document = TestHelperJobNotification.job_document(status)
                job = HelperJob.model_validate({**document, "type": "get_base_image"})
                self.assertIsNone(job.result)
                self.assertTrue(helper_job_notification(job))


if __name__ == "__main__":
    unittest.main()

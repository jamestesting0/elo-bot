import unittest

from elobot.integrations import helper_api


class TestHelperKickAndReinviteContract(unittest.TestCase):
    def test_kick_and_reinvite_serializes_exact_request(self) -> None:
        # Given
        request = helper_api.HelperJobCreateRequest(
            job_type="kick_and_reinvite",
            requested_by="123456789012345678",
            payload=helper_api.HelperKickAndReinvitePayload(
                host_clan_tag="#CLAN",
                player_tag="#PLAYER",
            ),
        )

        # When
        serialized = request.model_dump(by_alias=True)

        # Then
        self.assertEqual(serialized, {
            "type": "kick_and_reinvite",
            "requested_by": "123456789012345678",
            "payload": {"host_clan_tag": "#CLAN", "player_tag": "#PLAYER"},
        })



if __name__ == "__main__":
    unittest.main()

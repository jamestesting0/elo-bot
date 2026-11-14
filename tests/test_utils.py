import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import interactions

from elobot.discord.members import fetch_member_otherwise_user
from elobot.app.extensions import EXTENSIONS
from elobot.core.raid_weekend import is_raid_weekend, latest_completed_raid_weekend_end_timestamp


def frozen_utc(now: datetime) -> type[datetime]:
    class FrozenDatetime(datetime):
        @classmethod
        def utcnow(cls) -> datetime:
            return now

    return FrozenDatetime


class TestRaidWeekendClock(unittest.TestCase):
    def test_latest_completed_raid_weekend_end_moves_only_when_a_weekend_ends(self) -> None:
        previous_end = datetime(2026, 10, 26, 7, tzinfo=timezone.utc).timestamp()
        next_end = datetime(2026, 11, 2, 7, tzinfo=timezone.utc).timestamp()
        for now, expected in (
                (datetime(2026, 10, 28, 12), previous_end),
                (datetime(2026, 10, 31, 12), previous_end),
                (datetime(2026, 11, 2, 6, 59), previous_end),
                (datetime(2026, 11, 2, 7, 0, 1), next_end),
        ):
            with self.subTest(now=now), patch("elobot.core.raid_weekend.datetime", frozen_utc(now)):
                self.assertEqual(latest_completed_raid_weekend_end_timestamp(), int(expected))

    def test_raid_weekend_runs_from_friday_to_monday_0700_utc(self) -> None:
        for now, expected in (
                (datetime(2026, 10, 30, 6, 59), False),
                (datetime(2026, 10, 30, 7, 0, 1), True),
                (datetime(2026, 11, 2, 6, 59), True),
                (datetime(2026, 11, 2, 7, 0, 1), False),
        ):
            with self.subTest(now=now), patch("elobot.core.raid_weekend.datetime", frozen_utc(now)):
                self.assertIs(is_raid_weekend(), expected)


class TestGetExtensions(unittest.TestCase):
    def test_discovers_only_command_modules(self) -> None:
        # Given the extension package with its public modules, packages and private helpers
        package = Path(__file__).resolve().parents[1] / "elobot" / "extensions"

        # When the public extension modules are listed
        modules = sorted(
            f"elobot.extensions.{path.stem}"
            for path in package.iterdir()
            if not path.name.startswith("_") and (path.suffix == ".py" or (path / "__init__.py").is_file())
        )

        # Then the registry loads every one of them
        self.assertEqual(sorted(EXTENSIONS), modules)


class TestFetchMemberOtherwiseUser(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_to_user_when_member_fetch_is_unavailable(self) -> None:
        response = MagicMock(status=503, reason="Service Unavailable")
        unavailable = interactions.errors.DiscordError(
            response,
            response_data={"message": "upstream unavailable"},
        )
        guild = MagicMock(spec=interactions.Guild)
        guild.fetch_member = AsyncMock(side_effect=unavailable)
        bot = MagicMock(spec=interactions.Client)
        user = MagicMock(spec=interactions.User)
        bot.fetch_user = AsyncMock(return_value=user)

        result = await fetch_member_otherwise_user(guild, bot, "123")

        self.assertIs(result, user)
        guild.fetch_member.assert_awaited_once_with("123")
        bot.fetch_user.assert_awaited_once_with("123")

    async def test_reraises_other_member_fetch_server_errors(self) -> None:
        response = MagicMock(status=500, reason="Internal Server Error")
        server_error = interactions.errors.DiscordError(
            response,
            response_data={"message": "unexpected server error"},
        )
        guild = MagicMock(spec=interactions.Guild)
        guild.fetch_member = AsyncMock(side_effect=server_error)
        bot = MagicMock(spec=interactions.Client)
        bot.fetch_user = AsyncMock()

        with self.assertRaises(interactions.errors.DiscordError) as raised:
            await fetch_member_otherwise_user(guild, bot, "123")

        self.assertIs(raised.exception, server_error)
        bot.fetch_user.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

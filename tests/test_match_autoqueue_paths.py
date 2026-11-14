import unittest
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elobot.extensions.match import MatchCommands
from elobot.db import Match


class TestAutoqueueTerminalPaths(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.cog = object.__new__(MatchCommands)
        self.cog.bot = self.bot

    @staticmethod
    def _callback():
        callback = MatchCommands.match_autowin.callback
        return getattr(callback, "func", callback)

    @staticmethod
    def _match(*, already_autowin: bool = False):
        match = MagicMock(spec=Match)
        match.token = "TOKEN"
        match.thread = "321"
        match.is_autowin = already_autowin
        match.is_war = True
        match.hosts = [None, None]
        match.host_clans = [None, None]
        match.times = [None, None]
        match.matched_time = None
        match.submitters = [None, None]
        match.participants = [MagicMock(user_id="111"), MagicMock(user_id="222")]
        match.save_changes = AsyncMock()
        return match

    @staticmethod
    def _context():
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author.id = 999
        ctx.author.mention = "<@999>"
        ctx.channel.id = 321
        ctx.channel.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    async def test_autowin_calls_autoqueue_after_terminal_cleanup(self) -> None:
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        events = fixture.events

        async def clans_freed(*_args) -> None:
            events.append("free-clans")

        async def cleanup(*_args) -> None:
            events.append("cleanup")

        def schedule(*_args) -> None:
            events.append("rename")

        async def autoqueue(*_args) -> None:
            self.assertTrue(fixture.rows[Match][0]["is_autowin"])
            self.assertEqual(fixture.elos(), [1005, 965])
            events.append("autoqueue")

        fixture.autoqueue.side_effect = autoqueue
        fixture.free_clans.side_effect = clans_freed
        fixture.cleanup.side_effect = cleanup
        fixture.schedule.side_effect = schedule
        await fixture.invoke(fixture.context())

        fixture.autoqueue.assert_awaited_once_with(fixture.bot, "AUTOWIN", ("111", "222"))
        self.assertEqual(events[-3:], ["autoqueue", "Match", "rename"])
        self.assertTrue(fixture.rows[Match][0]["autowin_completed"])
        self.assertLess(events.index("free-clans"), events.index("autoqueue"))
        self.assertLess(events.index("cleanup"), events.index("autoqueue"))
        self.assertLess(events.index("autoqueue"), events.index("rename"))

    async def test_repeated_autowin_never_calls_autoqueue(self) -> None:
        from tests._autowin_fixture import AutowinState

        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0]["is_autowin"] = True
        await fixture.invoke(fixture.context())

        fixture.autoqueue.assert_not_awaited()

if __name__ == "__main__":
    unittest.main()

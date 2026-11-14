import unittest

from elobot.features.matches.timing import hosting_started
from elobot.db import Match


class TestMatchTiming(unittest.TestCase):
    def test_final_schedules_and_preassigned_hosts_remain_startable_until_hosting_commits(self):
        for times, schedules, starts, committed, hosts, expected in (
            ([100, 100], [100, 100], [None, None], [None, None], [None, None], False),
            ([100, 100], [100, 100], [None, None], [None, None], ["host", None], False),
            ([100, 100], [100, 100], [100, 100], [100, 100], ["host", None], True),
            ([200, 100], [100, 100], [None, None], [None, None], [None, None], True),
            ([None, None], [100, 100], [100, 100], [None, None], [None, None], False),
            ([100, 100], [100, 100], [100, 100], [None, None], [None, None], False),
            ([100, 100], [100, 100], [100, 100], [None, None], ["host", None], False),
        ):
            with self.subTest(times=times, starts=starts, hosts=hosts):
                source = Match.model_construct(format="elo_final_seven", times=times,
                    scheduled_times=schedules, final_started_times=starts, final_hosting_times=committed, hosts=hosts)
                self.assertEqual(hosting_started(source), expected)

    def test_war_side_and_ordinary_start_guards_keep_their_own_state(self):
        source = Match.model_construct(format="elo_final_seven", times=[200, 300], scheduled_times=[100, 300],
                                      final_started_times=[200, None], final_hosting_times=[None, None],
                                      hosts=["host", "assigned"])
        self.assertTrue(hosting_started(source, 0))
        self.assertFalse(hosting_started(source, 1))
        for times, hosts in (([100, None], [None, None]), ([None, None], ["host", None])):
            with self.subTest(times=times):
                self.assertTrue(hosting_started(Match.model_construct(times=times, hosts=hosts)))

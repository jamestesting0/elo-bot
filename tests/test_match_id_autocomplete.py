import asyncio
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import interactions as ipy

from elobot.extensions.player import (
    PlayerCommands,
    _fetch_display_names,
    _global_match_choice,
    _user_match_choice,
    forget_match_id_choice,
    forget_replaced_match_choice,
    hydrate_match_id_choice_names,
    publish_match_id_choices,
    rebuild_match_id_choices,
    refresh_user_match_id_choices,
    select_match_id_choices,
)


class AsyncCursor:
    def __init__(self, documents):
        self.documents = list(documents)

    def __aiter__(self):
        self._iter = iter(self.documents)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None


class MemberDirectory:
    def __init__(self, members):
        self.members = {str(user_id): member for user_id, member in members.items()}
        self.fetch_member = AsyncMock()
        self.lookups = []

    def get_member(self, user_id):
        self.lookups.append(str(user_id))
        return self.members.get(str(user_id))


def member(name):
    return SimpleNamespace(display_name=name)


def row(match_id, *player_ids):
    return {"_id": str(match_id), "player_ids": [str(player_id) for player_id in player_ids]}


class TestMatchIdAutocomplete(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = SimpleNamespace(match_id_choices={}, fetch_user=AsyncMock(return_value=None))
        self.guild = MemberDirectory({
            "111": member("Ann"),
            "222": member("Bob"),
            "444": member("Cat"),
        })

    def _collections(self, recorded_rows, players, found_player=None):
        recorded = SimpleNamespace()
        recorded.pipelines = []

        async def aggregate(pipeline):
            recorded.pipelines.append(pipeline)
            return AsyncCursor(recorded_rows)

        recorded.aggregate = aggregate
        player_collection = SimpleNamespace(
            find=lambda *args, **kwargs: AsyncCursor(players),
            find_one=AsyncMock(return_value=found_player),
        )
        player_collection.filters = []

        def find(query, projection):
            player_collection.filters.append((query, projection))
            return AsyncCursor(players)

        player_collection.find = find
        return recorded, player_collection

    async def test_rebuild_uses_one_projected_read_and_cached_names(self):
        recorded_rows = [
            row(9, "111", "444"),
            row(10, "222", "111"),
            row(8, "111", "222"),
            row(7, "111"),
        ]
        players = [{
            "user_id": "111",
            "matches": [SimpleNamespace(id="9"), {"$id": "10"}, "6"],
        }]
        recorded, player_collection = self._collections(recorded_rows, players)

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
                patch("elobot.extensions.player.Player.find_one", side_effect=AssertionError("full player load")),
                patch("elobot.extensions.player.RecordedMatch.find_all", side_effect=AssertionError("full match load")),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(len(recorded.pipelines), 1)
        self.assertNotIn("$match", recorded.pipelines[0][0])
        self.assertEqual(set(recorded.pipelines[0][0]["$project"]), {"player_ids"})
        self.assertEqual(player_collection.filters, [({}, {"user_id": 1, "matches": 1})])
        self.bot.fetch_user.assert_not_awaited()
        self.guild.fetch_member.assert_not_awaited()
        self.assertEqual(sorted(set(self.guild.lookups)), ["111", "222", "444"])

        self.assertEqual(
            [choice["value"] for choice in self.bot.match_id_choices["all"]],
            ["10", "9", "8"],
        )
        self.assertEqual(self.bot.match_id_choices["all"][0]["name"], "Bob vs. Ann (10)")
        self.assertEqual(
            [choice["name"] for choice in self.bot.match_id_choices["111"]],
            ["vs. Bob (10)", "vs. Cat (9)"],
        )
        self.assertNotIn("222", self.bot.match_id_choices)
        self.assertNotIn("6", {choice["value"] for choice in self.bot.match_id_choices["all"]})

    async def test_missing_member_is_fetched_once(self):
        recorded_rows = [row(match_id, "111", "333") for match_id in (1, 2, 3)]
        players = [{"user_id": "111", "matches": ["1", "2", "3"]}]
        recorded, player_collection = self._collections(recorded_rows, players)
        published = {}

        async def fetch_user(user_id):
            published["values"] = [choice["value"] for choice in self.bot.match_id_choices["all"]]
            published["label"] = self.bot.match_id_choices["111"][0]["name"]
            return member("Gone")

        self.bot.fetch_user = fetch_user

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(published["values"], ["3", "2", "1"])
        self.assertEqual(published["label"], "vs. 333 (3)")
        self.assertEqual(self.bot.match_id_choices["111"][0]["name"], "vs. Gone (3)")

    async def test_deleted_discord_user_falls_back_to_the_id(self):
        recorded_rows = [row(4, "111", "333")]
        players = [{"user_id": "111", "matches": ["4"]}]
        recorded, player_collection = self._collections(recorded_rows, players)

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(self.bot.match_id_choices["111"][0]["name"], "vs. 333 (4)")

    async def test_refresh_adds_only_the_new_match_and_keeps_newest_first(self):
        self.bot.match_id_choices = {
            "111": [_user_match_choice("Bob", "5")],
            "all": [_global_match_choice("Ann", "Bob", "5")],
        }
        recorded, player_collection = self._collections(
            [row(4, "111", "222"), row(6, "222", "111")],
            [],
            {"matches": [SimpleNamespace(id="5"), SimpleNamespace(id="4"), SimpleNamespace(id="6")]},
        )

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await refresh_user_match_id_choices(self.bot, self.guild, "111")
            await refresh_user_match_id_choices(self.bot, self.guild, "222")

        self.assertEqual(recorded.pipelines[0][0]["$match"]["_id"]["$in"], ["4", "6"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["111"]], ["6", "5", "4"])
        self.assertEqual(self.bot.match_id_choices["111"][0]["name"], "vs. Bob (6)")
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["6", "5", "4"])
        self.assertEqual(
            [choice["value"] for choice in self.bot.match_id_choices["all"]],
            list(dict.fromkeys(choice["value"] for choice in self.bot.match_id_choices["all"])),
        )

    async def test_refresh_skips_work_when_the_player_cache_is_current(self):
        self.bot.match_id_choices = {
            "111": [_user_match_choice("Bob", "2"), _user_match_choice("Bob", "1")],
            "all": [
                _global_match_choice("Ann", "Bob", "2"),
                _global_match_choice("Ann", "Bob", "1"),
            ],
        }
        recorded, player_collection = self._collections(
            [],
            [],
            {"matches": ["2", "1", "1"]},
        )

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await refresh_user_match_id_choices(self.bot, self.guild, "111")

        self.assertEqual(recorded.pipelines, [])
        self.bot.fetch_user.assert_not_awaited()
        self.assertEqual(len(self.bot.match_id_choices["all"]), 2)

    async def test_refresh_drops_matches_removed_from_the_player_history(self):
        self.bot.match_id_choices = {
            "111": [_user_match_choice("Bob", "2"), _user_match_choice("Bob", "1")],
            "all": [
                _global_match_choice("Ann", "Bob", "2"),
                _global_match_choice("Ann", "Bob", "1"),
            ],
        }
        recorded, player_collection = self._collections(
            [row(1, "111", "222")],
            [],
            {"matches": ["1"]},
        )

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await refresh_user_match_id_choices(self.bot, self.guild, "111")

        self.assertEqual(recorded.pipelines[0][0]["$match"]["_id"]["$in"], ["1"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["111"]], ["1"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["2", "1"])
        await forget_match_id_choice(self.bot, "2")
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["1"])

    async def test_deleted_match_leaves_the_player_and_global_suggestions(self):
        self.bot.file_locks = defaultdict(asyncio.Lock)
        self.bot.match_id_choices = {
            "all": [
                _global_match_choice("Ann", "Bob", "3"),
                _global_match_choice("Ann", "Bob", "2"),
                _global_match_choice("Cat", "Dee", "1"),
            ],
            "111": [_user_match_choice("Bob", "3"), _user_match_choice("Bob", "2")],
            "222": [_user_match_choice("Ann", "2")],
        }

        await forget_match_id_choice(self.bot, "2")

        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["3", "1"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["111"]], ["3"])
        self.assertEqual(self.bot.match_id_choices["222"], [])

    async def test_global_list_backfills_a_match_the_player_cache_already_has(self):
        self.bot.match_id_choices = {
            "111": [_user_match_choice("Bob", "2"), _user_match_choice("Bob", "1")],
            "all": [_global_match_choice("Ann", "Bob", "2")],
        }
        recorded, player_collection = self._collections(
            [row(1, "111", "222")],
            [],
            {"matches": ["2", "1"]},
        )

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await refresh_user_match_id_choices(self.bot, self.guild, "111")

        self.assertEqual(recorded.pipelines[0][0]["$match"]["_id"]["$in"], ["1"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["2", "1"])

    async def test_replaced_result_removes_only_the_previous_match(self):
        self.bot.match_id_choices = {
            "all": [_global_match_choice("Ann", "Bob", "5"), _global_match_choice("Ann", "Bob", "4")],
            "111": [_user_match_choice("Bob", "5"), _user_match_choice("Bob", "4")],
        }
        replaced = SimpleNamespace(result_operation=SimpleNamespace(
            previous_record=SimpleNamespace(id="4"),
            record=SimpleNamespace(id="5"),
        ))
        await forget_replaced_match_choice(self.bot, replaced)
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["all"]], ["5"])
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["111"]], ["5"])

        same_record = SimpleNamespace(result_operation=SimpleNamespace(
            previous_record=SimpleNamespace(id="5"),
            record=SimpleNamespace(id="5"),
        ))
        await forget_replaced_match_choice(self.bot, same_record)
        self.assertEqual([choice["value"] for choice in self.bot.match_id_choices["111"]], ["5"])

    async def test_non_numeric_match_id_does_not_break_the_cache(self):
        recorded_rows = [row("legacy", "111", "222"), row(2, "111", "222")]
        players = [{"user_id": "111", "matches": ["legacy", "2"]}]
        recorded, player_collection = self._collections(recorded_rows, players)

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(
            [choice["value"] for choice in self.bot.match_id_choices["all"]],
            ["2", "legacy"],
        )

    async def test_name_hydration_keeps_a_match_recorded_while_names_load(self):
        recorded_rows = [row(4, "111", "333")]
        players = [{"user_id": "111", "matches": ["4"]}]
        recorded, player_collection = self._collections(recorded_rows, players)

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            snapshot = await publish_match_id_choices(self.bot, self.guild)
            self.bot.match_id_choices["all"].insert(0, _global_match_choice("Ann", "Bob", "8"))
            self.bot.match_id_choice_epoch += 1
            self.bot.fetch_user = AsyncMock(return_value=member("Gone"))
            await hydrate_match_id_choice_names(self.bot, snapshot)

        self.assertEqual(self.bot.match_id_choices["all"][0]["value"], "8")
        self.assertIn("333", self.bot.match_id_choices["111"][0]["name"])

    async def test_discord_lookup_failure_still_publishes_choices(self):
        class BrokenMember:
            @property
            def display_name(self):
                raise AttributeError("missing user")

        self.guild.members["333"] = BrokenMember()
        recorded_rows = [row(4, "111", "333")]
        players = [{"user_id": "111", "matches": ["4"]}]
        recorded, player_collection = self._collections(recorded_rows, players)
        self.bot.fetch_user = AsyncMock(side_effect=ipy.errors.HTTPException(
            SimpleNamespace(status=429, reason="Too Many Requests"),
        ))

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(self.bot.match_id_choices["111"][0]["name"], "vs. 333 (4)")
        self.assertEqual(self.bot.match_id_choices["all"][0]["value"], "4")

    async def test_missing_display_names_are_fetched_one_user_at_a_time(self):
        recorded_rows = [row(1, "555", "666"), row(2, "777", "888")]
        players = [{"user_id": "555", "matches": ["1"]}]
        recorded, player_collection = self._collections(recorded_rows, players)
        in_flight = 0
        max_in_flight = 0

        async def fetch_user(user_id):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                if in_flight > 1:
                    raise RuntimeError("Attempted to lock a bucket that is already locked.")
                await asyncio.sleep(0)
                return member(f"Name{user_id}")
            finally:
                in_flight -= 1

        self.bot.fetch_user = fetch_user

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(self.bot.match_id_choices["555"][0]["name"], "vs. Name666 (1)")
        self.assertEqual(
            [choice["name"] for choice in self.bot.match_id_choices["all"]],
            ["Name777 vs. Name888 (2)", "Name555 vs. Name666 (1)"],
        )

    async def test_rate_limit_lock_on_one_user_keeps_the_other_names(self):
        recorded_rows = [row(1, "555", "666")]
        players = [{"user_id": "555", "matches": ["1"]}]
        recorded, player_collection = self._collections(recorded_rows, players)

        async def fetch_user(user_id):
            if str(user_id) == "666":
                raise RuntimeError("Attempted to lock a bucket that is already locked.")
            return member(f"Name{user_id}")

        self.bot.fetch_user = fetch_user

        with (
                patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded),
                patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=player_collection),
        ):
            await rebuild_match_id_choices(self.bot, self.guild)

        self.assertEqual(self.bot.match_id_choices["555"][0]["name"], "vs. 666 (1)")
        self.assertEqual(self.bot.match_id_choices["all"][0]["name"], "Name555 vs. 666 (1)")

    async def test_overlapping_name_fetches_do_not_share_the_user_bucket(self):
        in_flight = 0
        max_in_flight = 0

        async def fetch_user(user_id):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0)
                return member(f"Name{user_id}")
            finally:
                in_flight -= 1

        self.bot.fetch_user = fetch_user
        async with asyncio.timeout(5):
            first, second = await asyncio.gather(
                _fetch_display_names(self.bot, {"1", "2"}),
                _fetch_display_names(self.bot, {"3", "4"}),
            )

        self.assertEqual(max_in_flight, 1)
        self.assertEqual(set(first), {"1", "2"})
        self.assertEqual(set(second), {"3", "4"})

    def test_duplicate_choice_values_are_sent_once(self):
        choice = _user_match_choice("Ann", "4")
        self.assertEqual(
            select_match_id_choices([choice, dict(choice)], ""),
            [{"name": choice["name"], "value": "4"}],
        )

    async def test_choice_names_stay_within_discord_limit(self):
        choice = _user_match_choice("A" * 120, "15")
        self.assertEqual(len(choice["name"]), 100)
        self.assertLessEqual(len(_global_match_choice("A" * 80, "B" * 80, "15")["name"]), 100)

    def test_search_prefers_exact_id_then_prefix_then_name(self):
        choices = [
            _user_match_choice("Ann", "151"),
            _user_match_choice("Bob", "150"),
            _user_match_choice("Cat", "15"),
            _global_match_choice("Alice", "Bob", "3"),
        ]

        self.assertEqual(
            [choice["value"] for choice in select_match_id_choices(choices, "15")],
            ["15", "151", "150"],
        )
        self.assertEqual(select_match_id_choices(choices, "ALI"), [{"name": "Alice vs. Bob (3)", "value": "3"}])
        self.assertEqual(len(select_match_id_choices(choices, "")), 4)
        self.assertNotIn("search", select_match_id_choices(choices, "ann")[0])
        choices.extend(_user_match_choice(f"Opponent {index}", str(20000 - index)) for index in range(20000))
        choices.append(_user_match_choice("Zelda", "1"))
        self.assertEqual(select_match_id_choices(choices, "zelda"), [{"name": "vs. Zelda (1)", "value": "1"}])

    def test_empty_input_returns_the_newest_25_choices(self):
        choices = [_user_match_choice("Ann", str(1000 - index)) for index in range(40)]
        selected = select_match_id_choices(choices, "")
        self.assertEqual(len(selected), 25)
        self.assertEqual(selected[0]["value"], "1000")
        self.assertEqual(selected[-1]["value"], "976")

    async def _autocomplete(self, **kwargs):
        ctx = SimpleNamespace(
            command=SimpleNamespace(name=kwargs.pop("command", "player")),
            kwargs=kwargs.pop("options", {}),
            input_text=kwargs.pop("text", ""),
            author=SimpleNamespace(id=kwargs.pop("author", "111")),
            guild=kwargs.pop("guild", None),
            send=AsyncMock(),
        )
        cog = SimpleNamespace(bot=self.bot)
        collection = SimpleNamespace(find_one=AsyncMock(return_value=kwargs.pop("registered", {"_id": "111"})))
        with patch("elobot.extensions.player.Player.get_pymongo_collection", return_value=collection):
            await PlayerCommands.match_id_autocomplete.callback(cog, ctx)
        return ctx, collection

    async def test_player_autocomplete_uses_the_selected_user_id(self):
        self.bot.match_id_choices = {"222": [_user_match_choice("Ann", "9"), _user_match_choice("Bob", "8")]}

        class ResolvedUser:
            id = "222"

            def __str__(self):
                return "@someone"

        ctx, collection = await self._autocomplete(options={"user": ResolvedUser()}, text="")
        ctx.send.assert_awaited_once_with([
            {"name": "vs. Ann (9)", "value": "9"},
            {"name": "vs. Bob (8)", "value": "8"},
        ])
        collection.find_one.assert_awaited_once_with(
            {"user_id": "111"}, {"_id": 0, "user_id": 1}
        )

    async def test_player_autocomplete_hides_choices_from_unregistered_authors(self):
        self.bot.match_id_choices = {"222": [_user_match_choice("Ann", "9")]}
        ctx, collection = await self._autocomplete(options={"user": "222"}, registered=None, text="ann")
        ctx.send.assert_awaited_once_with([])
        collection.find_one.assert_awaited_once()

    async def test_player_autocomplete_answers_without_a_database_read_when_cache_is_empty(self):
        ctx, collection = await self._autocomplete(options={"user": "222"}, text="1")
        ctx.send.assert_awaited_once_with([])
        collection.find_one.assert_not_awaited()

    async def test_autocomplete_answers_before_the_user_option_is_filled(self):
        ctx, collection = await self._autocomplete(options={}, text="1")
        ctx.send.assert_awaited_once_with([])
        collection.find_one.assert_not_awaited()

    async def test_other_commands_autocomplete_from_the_global_list(self):
        self.bot.match_id_choices = {
            "all": [
                _global_match_choice("Ann", "Bob", "10"),
                _global_match_choice("Cat", "Dee", "9"),
            ]
        }
        ctx, collection = await self._autocomplete(command="data", text="cat")
        ctx.send.assert_awaited_once_with([{"name": "Cat vs. Dee (9)", "value": "9"}])
        collection.find_one.assert_not_awaited()

    async def test_data_match_autocomplete_uses_recent_matches_before_the_cache_is_ready(self):
        self.bot.match_id_choices = {}
        recorded_rows = [row(9, "111", "444"), row(10, "222", "111")]
        recorded, _player_collection = self._collections(recorded_rows, [])

        with patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded):
            ctx, _collection = await self._autocomplete(command="data", text="", guild=self.guild)

        self.bot.fetch_user.assert_not_awaited()
        self.assertEqual(recorded.pipelines[0][0], {"$sort": {"time": -1}})
        self.assertEqual(recorded.pipelines[0][1], {"$limit": 25})
        ctx.send.assert_awaited_once_with([
            {"name": "Bob vs. Ann (10)", "value": "10"},
            {"name": "Ann vs. Cat (9)", "value": "9"},
        ])

    async def test_data_match_autocomplete_prefixes_ids_before_the_cache_is_ready(self):
        self.bot.match_id_choices = {}
        recorded, _player_collection = self._collections([row(10, "222", "111"), row(9, "111", "444")], [])

        with patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded):
            ctx, _collection = await self._autocomplete(command="data", text="10", guild=self.guild)

        self.assertEqual(recorded.pipelines[0][0], {"$match": {"_id": {"$regex": "^10"}}})
        self.bot.fetch_user.assert_not_awaited()
        ctx.send.assert_awaited_once_with([{"name": "Bob vs. Ann (10)", "value": "10"}])

    async def test_data_match_name_search_waits_for_the_cache_without_discord_lookups(self):
        self.bot.match_id_choices = {}
        recorded, _player_collection = self._collections([row(10, "222", "111")], [])

        with patch("elobot.extensions.player.RecordedMatch.get_pymongo_collection", return_value=recorded):
            ctx, _collection = await self._autocomplete(command="data", text="bob", guild=self.guild)

        self.assertEqual(recorded.pipelines, [])
        self.bot.fetch_user.assert_not_awaited()
        ctx.send.assert_awaited_once_with([])

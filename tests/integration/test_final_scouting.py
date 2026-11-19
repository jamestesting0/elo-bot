import time
from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.game import DISTRICT_CODES_REVERSED
from elo_bot.db import BaseLink, Match
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.matches import bind_thread, prepare_match
from elo_bot.features.finals.scouting import FINAL_BASES_LOCKED, prepare_open_scouting
from tests.integration._final_ui_fixture import FinalUICase

SCOUT = "elo_bot.extensions.match.bases.store_scout_image"


class TestFinalScouting(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(self.final, "9")
        self.source = await prepare_match(self.final, "QF1")
        await self.source.set({"thread": "777"})
        await bind_thread(self.final, self.source, "777")
        self.cog = MatchCommands(self.bot)
        self.players = [participant.user_id for participant in self.source.participants]

    async def schedule(self, minutes_ahead: int):
        at = int(time.time()) + minutes_ahead * 60
        await self.source.set({"scheduled_times": [at, at], "times": [at, at]})

    def player(self, side: int):
        ctx = self.context(int(self.players[side]), manager=False)
        ctx.channel.id = 777
        return ctx

    async def select(self, panel, row: int, option: int, *, user: int, manager: bool = False):
        menu = panel.kwargs["components"][row].components[0]
        ctx = self.context(user, manager=manager, component=True)
        ctx.channel.id = 777
        ctx.custom_id = menu.custom_id
        ctx.values = [menu.options[option].value]
        await self.cog.final_scout_select.callback.func(self.cog, ctx)
        return ctx

    def link(self, district: str) -> str:
        code = next(code for code, name in DISTRICT_CODES_REVERSED.items() if name == district)
        return f"https://link.clashofclans.com/en?action=OpenLayout&id=TH15%3ACC%{code}%3AAAAAA"

    async def submit(self, ctx, district: str, **options):
        with patch("elo_bot.extensions.match.bases.find_match_thread_announcement", new=AsyncMock(return_value=None)):
            await self.cog.match_base_submit.callback.func(self.cog, ctx, base_link=self.link(district), **options)

    async def test_submission_uses_the_scout_view_and_locks_when_the_opponent_can_scout(self):
        districts = self.source.final_districts
        await self.schedule(60)
        ctx = self.player(0)
        with patch(SCOUT, new=AsyncMock(return_value="https://cdn.example/scout.webp")) as scout:
            await self.submit(ctx, districts[0])
            self.assertIs(scout.await_args.kwargs["ctx"], ctx)
            saved = (await Match.get(self.source.id)).participants[0].bases[0]
            self.assertEqual((saved.image, saved.scout_view), ("https://cdn.example/scout.webp", True))

            screenshot = self.player(0)
            await self.submit(screenshot, districts[1], base_ss=MagicMock(url="https://cdn.example/upload.png"))
            await self.schedule(20)
            locked = self.player(0)
            await self.submit(locked, districts[1])
        self.assertEqual(scout.await_count, 1)
        self.assertTrue(screenshot.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertEqual(locked.send.await_args.args[0], f"{ERROR_EMOJI} {FINAL_BASES_LOCKED}")
        self.assertIsNone((await Match.get(self.source.id)).participants[0].bases[1])

    async def test_failed_scout_capture_saves_nothing(self):
        await self.schedule(60)
        with patch(SCOUT, new=AsyncMock(return_value=None)):
            await self.submit(self.player(0), self.source.final_districts[0])
        self.assertIsNone((await Match.get(self.source.id)).participants[0].bases[0])

    async def test_player_scouts_one_opponent_district_at_a_time_after_opening(self):
        await self.source.set({
            "participants.1.bases.0": BaseLink(
                link="opponent", image="https://cdn.example/opponent.webp", scout_view=True).model_dump(),
            "participants.1.bases.6": BaseLink(
                link="unscouted", image="https://cdn.example/unscouted.webp").model_dump(),
        })
        await self.schedule(60)
        early = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, early)
        self.assertTrue(early.send.await_args.args[0].startswith(f"{ERROR_EMOJI} Scouting opens <t:"))

        await self.schedule(20)
        player = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, player)
        panel = player.send.await_args
        self.assertTrue(panel.kwargs["ephemeral"])
        self.assertEqual(panel.kwargs["embed"].image.url, "https://cdn.example/opponent.webp")
        district_menu, = panel.kwargs["components"]
        self.assertEqual([option.label for option in district_menu.components[0].options],
                         self.source.final_districts)

        missing = await self.select(panel, 0, 6, user=int(self.players[0]))
        missing_panel = missing.edit_origin.await_args
        self.assertIn("Goblin Mines", missing_panel.kwargs["embed"].title)
        self.assertIsNone(missing_panel.kwargs["embed"].image)
        self.assertIn("being prepared", missing_panel.kwargs["embed"].description)
        missing.send.assert_not_awaited()

        await self.source.set({"participants.1.bases.6": BaseLink(
            link="new", image="https://cdn.example/new.webp", scout_view=True).model_dump()})
        updated = await self.select(missing_panel, 0, 6, user=int(self.players[0]))
        self.assertEqual(updated.edit_origin.await_args.kwargs["embed"].image.url, "https://cdn.example/new.webp")

        outsider = self.context(5, manager=False)
        outsider.channel.id = 777
        await self.cog.match_base_scout.callback.func(self.cog, outsider)
        self.assertTrue(outsider.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        other_player = await self.select(panel, 0, 0, user=int(self.players[1]))
        self.assertTrue(other_player.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        other_player.edit_origin.assert_not_awaited()

    async def test_manager_switches_attackers_on_the_same_district_with_independent_opening_times(self):
        first = BaseLink(link="first", image="https://cdn.example/first.webp", scout_view=True)
        second = BaseLink(link="second", image="https://cdn.example/second.webp", scout_view=True)
        now = int(time.time())
        await self.source.set({"scheduled_times": [now + 10 * 60, now + 50 * 60],
                               "participants.0.bases.2": first.model_dump(),
                               "participants.1.bases.2": second.model_dump()})
        manager = self.context(manager=True)
        manager.channel.id = 777
        await self.cog.match_base_scout.callback.func(self.cog, manager)
        panel = manager.send.await_args
        self.assertEqual(manager.send.await_count, 1)
        views = panel.kwargs["components"][1].components[0].options
        self.assertEqual([option.label for option in views], ["Player 1's view", "Player 2's view"])

        district = await self.select(panel, 0, 2, user=manager.author.id, manager=True)
        district_panel = district.edit_origin.await_args
        self.assertEqual(district_panel.kwargs["embed"].image.url, second.image)
        locked = await self.select(district_panel, 1, 1, user=manager.author.id, manager=True)
        locked_panel = locked.edit_origin.await_args
        self.assertIsNone(locked_panel.kwargs["embed"].image)
        self.assertIn(f"<t:{now + 20 * 60}:R>", locked_panel.kwargs["embed"].description)

        await self.schedule(20)
        view_menu = locked_panel.kwargs["components"][1].components[0]
        unlocked, client = self.library_context(view_menu.custom_id)
        unlocked.author.has_role.side_effect = manager.author.has_role.side_effect
        unlocked.values = [view_menu.options[1].value]
        client.cache.get_channel.return_value.id = 777
        await self.cog.final_scout_select.callback.func(self.cog, unlocked)
        update = client.http.edit_interaction_message.await_args.args[0]
        self.assertEqual([embed["image"]["url"] for embed in update["embeds"]], [first.image])
        district, view = [row["components"][0] for row in update["components"]]
        self.assertEqual([option["label"] for option in district["options"] if option.get("default")],
                         ["Builder's Workshop"])
        self.assertEqual([option["label"] for option in view["options"] if option.get("default")],
                         ["Player 2's view"])
        client.http.post_followup.assert_not_awaited()

        revoked = await self.select(district_panel, 1, 1, user=manager.author.id)
        self.assertEqual(revoked.edit_origin.await_args.kwargs["embeds"], [])
        self.assertEqual(revoked.edit_origin.await_args.kwargs["components"], [])

    async def test_open_scouting_fills_missing_bases_and_captures_each_missing_scout_view_once(self):
        scouted = BaseLink(link="scouted", image="https://cdn.example/kept.webp", scout_view=True)
        await self.source.set({
            "participants.0.bases.0": scouted.model_dump(),
            "participants.0.bases.1": BaseLink(link="plain", image="layout").model_dump(),
            "participants.1.bases": [scouted.model_dump()] * 7,
        })

        async def fill(token, _attacker, _defender, return_match_data):
            match = await Match.find_one(Match.token == token)
            for index, base in enumerate(match.participants[0].bases):
                if base is None:
                    await match.set({f"participants.0.bases.{index}": BaseLink(
                        link=f"helper-{index}", image="layout").model_dump()})
            return await Match.find_one(Match.token == token)

        self.bot.fetch_user = AsyncMock(side_effect=lambda user: MagicMock(id=int(user)))
        capture = AsyncMock(side_effect=lambda _bot, link, _by: f"https://cdn.example/{link}.webp")
        with patch("elo_bot.features.finals.scouting.choose_bases", new=AsyncMock(side_effect=fill)) as choose, \
                patch("elo_bot.features.finals.scouting.store_scout_image", new=capture):
            await self.schedule(60)
            await prepare_open_scouting(self.bot)
            choose.assert_not_awaited()
            await self.schedule(20)
            await prepare_open_scouting(self.bot)
            await prepare_open_scouting(self.bot)
        bases = (await Match.get(self.source.id)).participants[0].bases
        self.assertTrue(all(base.scout_view for base in bases))
        self.assertEqual(bases[0], scouted)
        self.assertEqual(sorted(call.args[1] for call in capture.await_args_list),
                         sorted(["plain", *(f"helper-{index}" for index in range(2, 7))]))

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.game import DISTRICT_CODES_REVERSED
from elo_bot.db import BaseLayout, BaseLink, Match
from elo_bot.discord import runtime
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.finals.hosting import accept_final_start, check_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.matches import bind_thread, prepare_match, schedule_final_match
from elo_bot.features.finals import scout_images
from elo_bot.features.finals.scouting import FINAL_BASES_LOCKED, prepare_open_scouting
from elo_bot.features.matches.attack_bases import choose_bases
from elo_bot.integrations.helper_api import (
    HelperJobCreateRejected, HelperJobCreateUnavailable, HelperJobCreated, HelperTrackingFailure,
)
from tests.integration._final_ui_fixture import FinalUICase

SCOUT = "elo_bot.extensions.match.bases.store_scout_image"
BACKGROUND_SCOUT = "elo_bot.features.finals.scout_images.store_scout_image"


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

    async def check_manual_abort(self, *, war: bool, publication_fails: bool = False):
        at = int(time.time()) + 20 * 60
        if war:
            self.source = await schedule_final_match(self.source, at, player=0)
            self.source = await schedule_final_match(self.source, at, player=1)
        else:
            self.source = await schedule_final_match(self.source, at)
        await accept_final_start(self.source, "9", player=0 if war else None)
        base = BaseLink(link="saved", image="https://cdn.example/scout.webp", scout_view=True)
        await self.source.set({"old_msgs.0": 555, "participants.0.bases.0": base.model_dump(),
                               "participants.1.bases.0": base.model_dump()})
        player = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, player)
        panel = player.send.await_args
        self.assertEqual(panel.kwargs["embed"].image.url, base.image)

        ctx = self.context(component=war)
        ctx.channel.id = 777
        ctx.bot = self.bot
        ctx.message = MagicMock(id=555, embeds=[ipy.Embed(title="**Match Started**")])
        ctx.channel.fetch_message = AsyncMock(return_value=ctx.message)
        if war:
            ctx.custom_id = f"match_abort|{self.source.token}|0"
            callback = self.cog.abort_match_button
            module = "elo_bot.features.hosting.war.war_start"
        else:
            callback = self.cog.match_abort
            module = "elo_bot.features.hosting.live.live_start"
        error = ipy.errors.HTTPException(MagicMock(status=502, reason="Bad Gateway")) if publication_fails else None
        with patch(f"{module}.edit_channel_message", new=AsyncMock(side_effect=error)), \
                patch(f"{module}.mark_store_reserved", new=AsyncMock(return_value=True)), \
                patch(f"{module}.schedule_channel_name_edit"):
            if publication_fails:
                with self.assertRaises(ipy.errors.HTTPException):
                    await callback.callback.func(self.cog, ctx)
                ctx.send.assert_not_awaited()
            else:
                await callback.callback.func(self.cog, ctx)
                self.assertEqual(ctx.send.await_args.args[0], f"{SUCCESS_EMOJI} The match has been aborted.")

        saved = await Match.get(self.source.id)
        self.assertEqual(saved.scheduled_times, [at, at])
        self.assertEqual([p.bases[0] for p in saved.participants], [base, base])
        self.assertEqual(saved.times, [None, at] if war else [None, None])
        await check_final_start(saved, 0 if war else None)
        if war:
            await check_final_start(saved, 1)

        for side in (0, 1):
            scout = self.player(side)
            await self.cog.match_base_scout.callback.func(self.cog, scout)
            if side == 1 and war:
                self.assertEqual(scout.send.await_args.kwargs["embed"].image.url, base.image)
            else:
                self.assertNotIn("embed", scout.send.await_args.kwargs)
                self.assertIn("rescheduled", scout.send.await_args.args[0])

        reopened = await self.select(panel, 0, 0, user=player.author.id)
        self.assertIsNone(reopened.edit_origin.await_args.kwargs["embed"].image)
        self.assertIn("rescheduled", reopened.edit_origin.await_args.kwargs["embed"].description)

        new_time = at + 3600
        rescheduled = await schedule_final_match(saved, new_time, player=0 if war else None)
        await check_final_start(rescheduled, 0 if war else None)
        early = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, early)
        self.assertIn(f"<t:{new_time - 1800}:R>", early.send.await_args.args[0])
        with patch("elo_bot.features.finals.scout_ui.time.time", return_value=new_time - 1800):
            reopened = await self.select(panel, 0, 0, user=player.author.id)
        self.assertEqual(reopened.edit_origin.await_args.kwargs["embed"].image.url, base.image)

    async def test_manual_live_abort_hides_both_scout_views_until_rescheduling(self):
        await self.check_manual_abort(war=False)

    async def test_manual_live_abort_closes_scouting_even_if_discord_update_fails(self):
        await self.check_manual_abort(war=False, publication_fails=True)

    async def test_manual_war_abort_hides_only_that_sides_scout_view_until_rescheduling(self):
        await self.check_manual_abort(war=True)

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

    async def test_opponent_start_locks_bases_even_without_a_schedule(self):
        district = self.source.final_districts[0]

        async def capture(*_args, **_kwargs):
            await accept_final_start(self.source, "host", player=1)
            return "https://cdn.example/scout.webp"

        submitted = self.player(0)
        locked = self.player(0)
        with patch(SCOUT, new=AsyncMock(side_effect=capture)) as scout:
            await self.submit(submitted, district)
            await self.submit(locked, district)
        saved = await Match.get(self.source.id)
        self.assertEqual(saved.scheduled_times, [None, None])
        self.assertIsNone(saved.participants[0].bases[0])
        scout.assert_awaited_once()
        submitted.send.assert_awaited_once_with(f"{ERROR_EMOJI} The match changed. Your base was not saved.",
                                                ephemeral=True)
        locked.send.assert_awaited_once_with(f"{ERROR_EMOJI} Your bases are locked because your opponent has started.",
                                             ephemeral=True)

    async def test_assigning_helper_bases_starts_private_capture_before_scouting_opens(self):
        await self.schedule(60)
        district = self.source.final_districts[0]
        layout = await BaseLayout(district=district, link=self.link(district), image_url="layout",
                                  quality="Excellent", expiration_date=time.time() + 3600).insert()
        started = asyncio.Future()
        release = asyncio.Event()

        async def capture(_bot, link, _requested_by):
            saved = await Match.get(self.source.id)
            started.set_result((asyncio.current_task(), link, saved.participants[1].bases[0]))
            async with asyncio.timeout(5):
                await release.wait()
            return "https://cdn.example/helper-scout.webp"

        with patch.object(runtime, "bot", self.bot, create=True), \
                patch(BACKGROUND_SCOUT, side_effect=capture):
            async with asyncio.timeout(2):
                try:
                    await choose_bases(self.source.token, MagicMock(id=self.players[0]), MagicMock(id=self.players[1]))
                    task, link, saved = await started
                    self.assertEqual(link, layout.link)
                    self.assertEqual(saved.link, layout.link)
                finally:
                    release.set()
                await task

        ctx = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, ctx)
        self.assertNotIn("embed", ctx.send.await_args.kwargs)

        await self.schedule(20)
        ctx = self.player(0)
        await self.cog.match_base_scout.callback.func(self.cog, ctx)
        self.assertEqual(ctx.send.await_args.kwargs["embed"].image.url, "https://cdn.example/helper-scout.webp")

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

    async def test_helper_reservation_or_unconfirmed_capture_defers_both_sides(self):
        base = BaseLink(link="pending-layout", image="existing-layout")
        await self.source.set({f"participants.{side}.bases.{index}": base.model_dump()
                               for side in (0, 1) for index in (0, 1)})
        job_id = "a" * 32
        for outcome, tracking_failure in (
            (HelperJobCreateRejected(error=f"reserved_by_sequential_job:{job_id}"), None),
            (HelperJobCreateUnavailable(), None),
            (HelperJobCreated(id=job_id), HelperTrackingFailure(job_id=job_id, reason="Helper could not be reached.")),
        ):
            with (
                self.subTest(outcome=type(outcome).__name__),
                patch.object(scout_images, "_capture_retry_at", 0.0),
                patch.object(scout_images, "monotonic", return_value=100.0) as clock,
                patch.multiple("elo_bot.features.bases.screenshots",
                               HELPER_API_BASE_URL="http://127.0.0.1:1", HELPER_API_KEY="test-key"),
                patch("elo_bot.features.bases.screenshots.HelperJobCreator.create",
                      new_callable=AsyncMock, return_value=outcome) as create,
                patch("elo_bot.features.bases.screenshots.HelperJobPoller.wait",
                      new_callable=AsyncMock, return_value=tracking_failure),
                self.assertLogs(level="WARNING") as captured,
            ):
                await scout_images._capture_scout_views(self.bot, self.source.id, 0)
                await scout_images._capture_scout_views(self.bot, self.source.id, 1)
                create.assert_awaited_once()
                self.assertNotIn("pending-layout", "\n".join(captured.output))
                clock.return_value = 161.0
                await scout_images._capture_scout_views(self.bot, self.source.id, 1)
                self.assertEqual(create.await_count, 2)
        saved = await Match.get(self.source.id)
        for participant in saved.participants:
            self.assertEqual([(saved_base.image, saved_base.scout_view) for saved_base in participant.bases[:2]],
                             [("existing-layout", False)] * 2)

    async def test_scouting_retries_pending_capture_and_fills_missing_bases_at_opening(self):
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
        captures = asyncio.Queue()
        attempted = []

        async def capture(_bot, link, _by):
            attempted.append(link)
            captures.put_nowait(asyncio.current_task())
            if len(attempted) == 1:
                return None
            if len(attempted) == 2:
                raise ipy.errors.HTTPException(MagicMock(status=502, reason="Bad Gateway"))
            return f"https://cdn.example/{link}.webp"

        with patch("elo_bot.features.finals.scouting.choose_bases", new=AsyncMock(side_effect=fill)) as choose, \
                patch(BACKGROUND_SCOUT, side_effect=capture):
            await self.schedule(60)
            for failed_step in ("Helper capture", "Discord upload"):
                with self.subTest(failed_step=failed_step):
                    await prepare_open_scouting(self.bot)
                    async with asyncio.timeout(2):
                        await (await captures.get())
                    pending = (await Match.get(self.source.id)).participants[0].bases[1]
                    self.assertEqual((pending.image, pending.scout_view), ("layout", False))

            await prepare_open_scouting(self.bot)
            async with asyncio.timeout(2):
                await (await captures.get())
            choose.assert_not_awaited()
            before_opening = (await Match.get(self.source.id)).participants[0].bases
            self.assertEqual(before_opening[1].image, "https://cdn.example/plain.webp")
            self.assertEqual(before_opening[2:], [None] * 5)
            await self.schedule(20)
            await prepare_open_scouting(self.bot)
            async with asyncio.timeout(2):
                await (await captures.get())
            await prepare_open_scouting(self.bot)
        bases = (await Match.get(self.source.id)).participants[0].bases
        self.assertTrue(all(base.scout_view for base in bases))
        self.assertEqual(bases[0], scouted)
        self.assertCountEqual(attempted, ["plain"] * 3 + [f"helper-{index}" for index in range(2, 7)])

    async def test_capture_does_not_publish_an_image_for_a_replaced_base(self):
        old = BaseLink(link="old", image="old-layout")
        replacement = BaseLink(link="replacement", image="new-layout")
        field = "participants.1.bases.0"
        await self.source.set({field: old.model_dump()})
        await self.schedule(60)
        started = asyncio.Future()

        async def capture(_bot, _link, _by):
            await self.source.set({field: replacement.model_dump()})
            started.set_result(asyncio.current_task())
            return "https://cdn.example/stale.webp"

        with patch(BACKGROUND_SCOUT, side_effect=capture):
            await prepare_open_scouting(self.bot)
            async with asyncio.timeout(2):
                await (await started)
        self.assertEqual((await Match.get(self.source.id)).participants[1].bases[0], replacement)

    async def test_removing_one_base_preserves_a_concurrently_saved_scout_view(self):
        layout = BaseLink(link="keep", image="layout")
        scout = BaseLink(link="keep", image="https://cdn.example/ready.webp", scout_view=True)
        field = "participants.0.bases.0"
        await self.source.set({field: layout.model_dump(),
                               "participants.0.bases.1": BaseLink(link="remove", image="layout").model_dump()})
        await self.schedule(60)

        async def check_override(*_):
            await self.source.set({field: scout.model_dump()})
            return False

        ctx = self.player(0)
        with patch("elo_bot.extensions.match.bases.OverrideCode.is_valid", side_effect=check_override):
            await self.cog.match_base_remove.callback.func(self.cog, ctx, player=0,
                                                            district=self.source.final_districts[1])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual((await Match.get(self.source.id)).participants[0].bases[:2], [scout, None])

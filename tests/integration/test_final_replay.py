import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId

from elo_bot.core.finals import FINAL_DISTRICTS, FinalError
from elo_bot.db import BaseStore, Clan, Match, Participant
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.base_store.repository import sync_base_store_slots
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.result_drafts import create_result_draft, finish_result_draft
from elo_bot.features.finals.results import FinalResultSubmission
from elo_bot.features.hosting.sequential import service
from elo_bot.features.hosting.sequential.replay import open_replay, replay_scores
from elo_bot.features.hosting.sequential.runs import open_regular_run
from elo_bot.features.matches.resources import claim_resource_clan, free_clan, try_reserve_resource_clan
from elo_bot.integrations.helper_api import HelperJobCreated, HelperSequentialJob
from tests.integration._final_fixture import FinalScenarioCase

JOB_ID = "a" * 32


def verified(district: str, player: str, stars: int, destruction: int) -> dict:
    return {"district": district, "player": player, "status": "verified",
            "score": {"stars_gained": stars, "destruction_gained": destruction}}


class TestFinalReplay(FinalScenarioCase):
    async def reserve_foreign_slot(self) -> None:
        foreign_store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="foreign", ready=True, message_id=91)
        await Match(token="FOREIGN", matched_time=1, thread="888",
                    participants=[Participant(user_id="100", base_store=foreign_store),
                                  Participant(user_id="101")]).insert()

    async def setup_replay(self, side: int, *, availability: str = "Available") -> None:
        final = await confirm_bracket(await self.roster(8), "manager")
        self.source = await self.scheduled_pairing(final, "QF1", is_war=side != LIVE_SIDE)
        await self.source.set({"thread": "777", "host_clans": ["#CLAN", "#CLAN"],
                               "participants.0.result": ["2.70"] * 7, "participants.1.result": ["1.40"] * 7})
        await bind_thread(final, self.source, "777")
        self.final = await final_for_view()
        await Clan(tag="#CLAN", name="Asterism", availability_str=availability).insert()
        self.links = [[f"https://example.com/layout/{player}/{index}" for index in range(7)] for player in (0, 1)]
        tags = [f"#P{p.user_id}" for p in self.source.participants]
        if side == LIVE_SIDE:
            job_type = "host_sequential_live_match"
            payload = {"player_1_tag": tags[0], "player_2_tag": tags[1], "base_links_for_player_1": self.links[0],
                       "base_links_for_player_2": self.links[1]}
        else:
            job_type = "host_sequential_war_match"
            tags = [tags[side]]
            payload = {"player_tag": tags[0], "base_source": "links", "base_links": self.links[side], "slot": 1}
        self.original = await HelperSequentialRun(
            match_id=self.source.id, match_token=self.source.token, thread_id="777", final_id=str(final.id),
            final_node_id="QF1", final_contestant_revision=self.source.final_contestant_revision,
            final_attempt=self.source.final_attempt, side=side, host_clan_tag="#CLAN", player_tags=tags,
            districts=list(FINAL_DISTRICTS), create_request={"type": job_type, "requested_by": "9",
                "payload": {"host_clan_tag": "#CLAN", **payload}}, requested_by="9", state="TERMINAL", open=False,
            job_id="b" * 32, job_status="succeeded", created_at=1, progress_message_id=55,
        ).insert()

    async def press_replay(self, districts: list[str]):
        bot = MagicMock(ext={})
        cog = MatchCommands(bot)
        ctx = MagicMock(author=MagicMock(id=9), defer=AsyncMock(), send=AsyncMock(), channel_id=777, message_id=55,
                        custom_id=f"helper_seq_replay|{self.original.id}")
        helper = MagicMock(create=AsyncMock(return_value=HelperJobCreated(id=JOB_ID)))
        with (
            patch.object(service, "_pick_districts", new=AsyncMock(return_value=districts)),
            patch.object(service, "helper_client", return_value=helper),
            patch.object(service, "start_watcher"),
            patch.object(service, "publish_progress", new_callable=AsyncMock),
            patch.object(service, "progress_message", new=AsyncMock(return_value=None)),
        ):
            await cog.helper_sequential_replay.callback.func(cog, ctx)
        return await service.latest_run(self.source.id, self.original.side), helper, ctx

    async def poll(self, run: HelperSequentialRun, scores: list[dict] | None, status: str = "running",
                   *, match_commands=None) -> None:
        job = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": run.create_request["type"], "status": status, "created_at": 1,
            "started_at": 2, "finished_at": None if status == "running" else 3, "error": None,
            "result": {"stage": "WAITING", "score_status": "incomplete",
                       "match": {"players": [{"key": f"player_{i}", "tag": tag}
                                             for i, tag in enumerate(run.player_tags, 1)]},
                       "completion": {"workflow_finished": False, "districts_completed": 0,
                                      "districts_total": len(run.districts), "rounds": []},
                       "score_outcomes": scores} if scores is not None else None,
        })
        bot = MagicMock()
        bot.get_ext.return_value = match_commands
        await service.apply_read(bot, await HelperSequentialRun.get(run.id), await Match.get(self.source.id), job, None)

    async def test_successful_live_run_can_replay_two_selected_districts_and_close_with_partial_scores(self):
        await self.setup_replay(LIVE_SIDE)
        controls = service.progress_components(self.original, await Match.get(self.source.id))
        self.assertEqual([button.label for button in controls[0].components], ["Replay", "Submit Result"])
        draft = await create_result_draft(self.final, FinalResultSubmission(
            final_id=self.final.id, node_id="QF1", actor_id="9", operation_key="before-replay", expected_version=0,
            contestant_revision=self.source.final_contestant_revision, attempt=self.source.final_attempt,
            scores={p.user_id: list(p.result) for p in self.source.participants},
        ))

        run, helper, _ = await self.press_replay(["Dragon Cliffs", "Barbarian Camp"])
        self.assertEqual(run.districts, ["Barbarian Camp", "Dragon Cliffs"])
        self.assertEqual(run.create_request["payload"]["base_links_for_player_2"], [self.links[1][0], self.links[1][3]])
        helper.create.assert_awaited_once()
        self.assertIn("Replay", (await Clan.find_one({"tag": "#CLAN"})).availability_str)
        self.assertFalse(await free_clan("#CLAN", expected_thread="777"))
        self.assertEqual(await sync_base_store_slots("#CLAN"), {"#CLAN": []})
        with self.assertRaisesRegex(FinalError, "Wait for the district replay"):
            await finish_result_draft(draft)
        partial = verified("BC", "player_1", 3, 100)
        await self.poll(run, [partial])
        self.assertEqual((await Match.get(self.source.id)).participants[0].result[0], "2.70")
        pair = [partial, verified("BC", "player_2", 2, 85)]
        await self.poll(run, pair)
        await self.poll(run, [*pair, verified("DC", "player_2", 3, 100),
                              verified("BL", "player_1", 3, 100), verified("BL", "player_2", 3, 100)], "cancelled")

        saved = await Match.get(self.source.id)
        self.assertEqual([p.result[:4] for p in saved.participants], [
            ["3.100", "2.70", "2.70", "2.70"], ["2.85", "1.40", "1.40", "1.40"],
        ])
        self.assertEqual(saved.final_replay_revision, 1)
        self.assertIsNone(saved.final_replay_run_id)
        closed = await HelperSequentialRun.get(run.id)
        self.assertFalse(closed.open)
        self.assertEqual(closed.replay_replacements, {"Barbarian Camp": ["3.100", "2.85"]})
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, "Available")
        with self.assertRaisesRegex(FinalError, "Replay changed the scores"):
            await finish_result_draft(draft)

    async def test_replay_preserves_both_live_scores_when_staff_changed_either_slot(self):
        await self.setup_replay(LIVE_SIDE)
        run, _, _ = await self.press_replay(["Barbarian Camp"])
        await self.source.set({"participants.1.result.0": "2.82"})
        await self.poll(run, [verified("BC", "player_1", 3, 100), verified("BC", "player_2", 3, 100)], "succeeded")
        saved = await Match.get(self.source.id)
        self.assertEqual([p.result[0] for p in saved.participants], ["2.70", "2.82"])
        self.assertEqual((await HelperSequentialRun.get(run.id)).replay_conflicts, ["Barbarian Camp"])

    async def test_open_replay_refuses_host_clan_changes_through_command_and_claim(self):
        await self.setup_replay(LIVE_SIDE)
        await self.source.set({"participants.1.result.0": None})
        run, _, _ = await self.press_replay(["Barbarian Camp"])
        await Clan(tag="#OTHER", name="Another Clan", availability_str="Available").insert()
        bot = MagicMock(ext={})
        cog = MatchCommands(bot)
        ctx = MagicMock(author=MagicMock(id=9), defer=AsyncMock(), send=AsyncMock())
        ctx.channel.id = 777
        await cog.match_set_clan.callback.func(cog, ctx, clan="#CLAN")
        self.assertIn("before changing the host clan", ctx.send.await_args.args[0])

        reservation = await try_reserve_resource_clan("#OTHER")
        self.assertFalse(await claim_resource_clan(reservation, self.source, 0, player2_index=1, allow_current_owner=True))
        self.assertEqual((await Match.get(self.source.id)).host_clans[0], "#CLAN")
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, run.replay_clan_claim)
        self.assertEqual((await Clan.find_one({"tag": "#OTHER"})).availability_str, "Available")

    async def test_uncertain_replay_of_a_cancelled_final_cannot_queue_again(self):
        await self.setup_replay(LIVE_SIDE)
        run, _, _ = await self.press_replay(["Barbarian Camp"])
        await run.set({"state": "UNCERTAIN", "job_id": None})
        bot = MagicMock()
        ctx = MagicMock(author=MagicMock(id=9), send=AsyncMock())
        helper = MagicMock(create=AsyncMock())

        async def confirm_after_cancellation(*args, **kwargs):
            await self.final.set({"state": "CANCELLING", "active": False})
            return "seq_adopt"

        with (
            patch.object(service, "choose", new=AsyncMock(side_effect=confirm_after_cancellation)),
            patch.object(service, "helper_client", return_value=helper),
            patch.object(service, "progress_message", new=AsyncMock(return_value=None)),
        ):
            await service.adopt_uncertain(bot, ctx, run)

        helper.create.assert_not_awaited()
        self.assertFalse((await HelperSequentialRun.get(run.id)).open)
        self.assertIsNone((await Match.get(self.source.id)).final_replay_run_id)
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, "Available")

    async def test_war_replay_updates_only_its_side(self):
        await self.setup_replay(1)
        run, _, _ = await self.press_replay(["Goblin Mines"])
        with patch.object(service, "store_replay_scores", new=AsyncMock(side_effect=RuntimeError("Process stopped"))):
            with self.assertRaisesRegex(RuntimeError, "Process stopped"):
                await self.poll(run, [verified("GM", "player_1", 3, 100)])
        await self.poll(run, None, "succeeded")
        saved = await Match.get(self.source.id)
        self.assertEqual([p.result[-1] for p in saved.participants], ["2.70", "3.100"])

    async def test_replay_refuses_a_clan_taken_by_another_match(self):
        occupied = "<#888> <@101> <t:1234:R>"
        await self.setup_replay(0, availability=occupied)
        latest, helper, ctx = await self.press_replay(["Barbarian Camp"])
        self.assertEqual(latest.id, self.original.id)
        helper.create.assert_not_awaited()
        self.assertIn("Asterism", ctx.send.await_args.args[0])
        self.assertIsNone((await Match.get(self.source.id)).final_replay_run_id)
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, occupied)

    async def test_linked_live_replay_requires_both_slots_to_be_available(self):
        await self.setup_replay(LIVE_SIDE)
        await self.reserve_foreign_slot()
        latest, helper, ctx = await self.press_replay(["Barbarian Camp"])
        self.assertEqual(latest.id, self.original.id)
        helper.create.assert_not_awaited()
        self.assertIn("free base slots", ctx.send.await_args.args[0])
        self.assertIsNone((await Match.get(self.source.id)).final_replay_run_id)

    async def test_linked_war_replay_uses_a_free_slot_and_preserves_the_original_layout(self):
        await self.setup_replay(0)
        await self.reserve_foreign_slot()
        run, helper, _ = await self.press_replay(["Balloon Lagoon"])
        payload = helper.create.await_args.args[1].payload
        self.assertEqual(payload.slot, 2)
        self.assertEqual(payload.base_links, [self.links[0][1]])
        self.assertEqual(run.create_request["payload"]["slot"], 2)
        foreign = await Match.find_one({"token": "FOREIGN"})
        self.assertEqual(foreign.participants[0].base_store.reservation_id, "foreign")

    async def test_cancelled_replay_restores_its_unfinished_sides_original_clan_assignment(self):
        await self.setup_replay(0)
        old = f"<#777> <@{self.source.participants[0].user_id}> <t:1234:R>"
        await (await Clan.find_one({"tag": "#CLAN"})).set({"availability_str": old})
        await self.source.set({"participants.0.result.0": None})
        run, _, _ = await self.press_replay(["Barbarian Camp"])
        self.assertFalse(await free_clan("#CLAN", expected_thread="777"))
        await self.poll(run, None, "cancelled")
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, old)

    async def test_live_replay_keeps_the_shared_clan_when_only_one_side_finishes(self):
        await self.setup_replay(LIVE_SIDE)
        users = self.source.participants
        old = f"<#777> <@{users[0].user_id}> vs <@{users[1].user_id}> <t:1234:R>"
        await (await Clan.find_one({"tag": "#CLAN"})).set({"availability_str": old})
        await self.source.set({"participants.0.result.0": None, "participants.1.result.0": None,
                               "participants.1.result.1": None})
        run, _, _ = await self.press_replay(["Barbarian Camp"])

        async def finish(match, side, *, release_clan=True):
            if release_clan:
                await free_clan(match.host_clans[side], expected_thread="777")

        commands = MagicMock(_finish_stored_result_side=AsyncMock(side_effect=finish))
        await self.poll(run, [verified("BC", "player_1", 3, 100), verified("BC", "player_2", 3, 95)],
                        "succeeded", match_commands=commands)

        saved = await Match.get(self.source.id)
        self.assertTrue(all(saved.participants[0].result))
        self.assertIsNone(saved.participants[1].result[1])
        self.assertEqual((await Clan.find_one({"tag": "#CLAN"})).availability_str, old)

    async def test_replay_uses_the_original_held_stores_after_hosting(self):
        await self.setup_replay(LIVE_SIDE)
        await self.original.set({
            "layout_storage_ids": ["original", "original"],
            "create_request": {"type": "host_sequential_live_match_on_stored", "requested_by": "9", "payload": {
                "host_clan_tag": "#CLAN", "player_1_tag": self.original.player_tags[0],
                "player_2_tag": self.original.player_tags[1], "districts": list(FINAL_DISTRICTS),
            }},
        })
        for side in (0, 1):
            store = BaseStore(clan_tag="#CLAN", slot=f"Slot {side + 1}", reservation_id="different",
                              message_id=53, is_live=True, ready=False)
            await self.source.set({f"participants.{side}.base_store": store.model_dump()})

        latest, helper, ctx = await self.press_replay(["Balloon Lagoon"])
        self.assertEqual(latest.id, self.original.id)
        helper.create.assert_not_awaited()
        self.assertIn("original stored layouts", ctx.send.await_args.args[0])

        await self.source.set({f"participants.{side}.base_store.reservation_id": "original" for side in (0, 1)})
        run, helper, _ = await self.press_replay(["Balloon Lagoon"])
        helper.create.assert_awaited_once()
        self.assertEqual(run.create_request["type"], "host_sequential_live_match_on_stored")
        self.assertEqual(run.create_request["payload"]["districts"], ["Balloon Lagoon"])

    async def test_opposite_war_side_cannot_open_a_normal_run_during_replay_registration(self):
        await self.setup_replay(0)
        district = "Barbarian Camp"
        replay = self.original.model_copy(update={
            "id": PydanticObjectId(), "previous_run_id": self.original.id, "state": "CREATING", "open": True,
            "job_id": None, "job_status": None, "created_at": 2, "districts": [district],
            "replay_baseline": replay_scores(self.source, 0),
        })
        regular = replay.model_copy(update={
            "id": PydanticObjectId(), "previous_run_id": None, "side": 1, "replay_baseline": None,
        })
        async with asyncio.timeout(10):
            results = await asyncio.gather(open_replay(replay, self.original), open_regular_run(regular),
                                           return_exceptions=True)

        self.assertEqual(sum(result is None for result in results), 1)
        self.assertEqual(sum(isinstance(result, FinalError) for result in results), 1)
        active = await HelperSequentialRun.find({"match_id": self.source.id, "open": True}).to_list()
        self.assertEqual(len(active), 1)
        saved = await Match.get(self.source.id)
        self.assertEqual(saved.final_last_helper_run_id, active[0].id)
        self.assertEqual(saved.final_replay_run_id, replay.id if active[0].id == replay.id else None)

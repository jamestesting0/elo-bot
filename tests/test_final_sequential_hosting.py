# noqa: SIZE_OK - client, storage, ledger and lifecycle cases share one in-memory run and match store
import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import anyio
import interactions as ipy
from aiohttp import web
from beanie import PydanticObjectId
from pydantic import ValidationError

import elo_bot.features.hosting.sequential.service as service
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.finals import result_ui
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.hosting.sequential import runs, scores, ui
from elo_bot.integrations.helper_api import (
    HelperJobCreateConflict,
    HelperJobCreated,
    HelperJobCreateRejected,
    HelperJobCreateUnavailable,
    HelperSequentialClient,
    HelperSequentialJob,
    HelperSequentialJobCreateRequest,
    HelperSequentialResult,
    HelperSequentialWarPayload,
    HelperTrackingFailure,
)

JOB_ID = "0123456789abcdef0123456789abcdef"
MATCH_ID = PydanticObjectId()
CODES = ["BC", "BL", "BW", "DC", "GQ", "SP", "GM"]
LINKS = [f"https://link/{code}" for code in CODES]


def get_path(document: dict, path: str):
    value = document
    for key in path.split("."):
        if isinstance(value, list):
            value = value[int(key)]
        elif isinstance(value, dict):
            value = value.get(key)
        else:
            return None
    return value


def set_path(document: dict, path: str, value) -> None:
    *parents, last = path.split(".")
    target = document
    for key in parents:
        target = target[int(key)] if isinstance(target, list) else target[key]
    if isinstance(target, list):
        target[int(last)] = value
    else:
        target[last] = value


def matches(document: dict, query: dict) -> bool:
    for path, expected in query.items():
        actual = get_path(document, path)
        if isinstance(expected, dict) and "$in" in expected:
            if actual not in expected["$in"]:
                return False
        elif isinstance(expected, dict) and "$ne" in expected:
            if actual == expected["$ne"]:
                return False
        elif actual != expected:
            return False
    return True


class FakeCollection:
    def __init__(self, *documents: dict) -> None:
        self.documents = list(documents)

    async def update_one(self, query: dict, update: dict) -> MagicMock:
        for document in self.documents:
            if matches(document, query):
                for path, value in update.get("$set", {}).items():
                    set_path(document, path, value)
                for path, value in update.get("$inc", {}).items():
                    set_path(document, path, (get_path(document, path) or 0) + value)
                for path, value in update.get("$push", {}).items():
                    document.setdefault(path, []).extend(value["$each"])
                return MagicMock(matched_count=1, modified_count=1)
        return MagicMock(matched_count=0, modified_count=0)


def run_document(**overrides) -> dict:
    document = {
        "_id": PydanticObjectId(), "match_id": MATCH_ID, "match_token": "TOKEN", "thread_id": "55",
        "final_id": "final", "final_node_id": "QF1", "final_contestant_revision": 1, "final_attempt": 1,
        "side": LIVE_SIDE, "host_clan_tag": "#CLAN", "player_tags": ["#P1", "#P2"],
        "districts": list(FINAL_DISTRICTS),
        "create_request": {"type": "host_sequential_live_match", "requested_by": "7", "payload": {
            "host_clan_tag": "#CLAN", "base_source": "links", "player_1_tag": "#P1", "player_2_tag": "#P2",
            "base_links_for_player_1": LINKS, "base_links_for_player_2": LINKS}},
        "requested_by": "7", "state": "ACTIVE", "open": True, "job_id": JOB_ID,
        "job_status": "running", "created_at": 1.0, "events": [], "poll_failures": 0,
    }
    document.update(overrides)
    return document


def as_run(document: dict) -> HelperSequentialRun:
    fields = {key: value for key, value in deepcopy(document).items() if key != "_id"}
    return HelperSequentialRun.model_construct(id=document["_id"], **fields)


def match_document(**overrides) -> dict:
    document = {
        "_id": MATCH_ID, "final_contestant_revision": 1, "final_attempt": 1, "final_superseded": False,
        "final_result_id": None, "final_result_operation": None,
        "participants": [{"result": [None] * 7}, {"result": [None] * 7}],
    }
    document.update(overrides)
    return document


def helper_result(completed: dict[str, list[str]], outcomes: list[dict], players=("player_1", "player_2")) -> dict:
    return {
        "stage": "RUNNING", "score_status": "incomplete",
        "match": {"players": [{"key": key, "tag": f"#P{index + 1}"} for index, key in enumerate(players)]},
        "completion": {"workflow_finished": False, "districts_completed": 0, "districts_total": 7, "rounds": [
            {"districts": {key: code for key in players},
             "players": {key: {"posted": True, "completed": code in completed.get(key, []), "closed": False}
                         for key in players}}
            for code in CODES
        ]},
        "score_outcomes": outcomes,
    }


def verified(code: str, player: str, stars: int = 3, destruction: float = 100.0) -> dict:
    return {"district": code, "player": player, "status": "verified",
            "score": {"stars_gained": stars, "destruction_gained": destruction}}


class TestSequentialClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.status = 201
        self.body: dict = {"id": JOB_ID}
        app = web.Application()
        app.router.add_post("/v1/jobs", self.handle_create)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.base_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        return web.Response(status=self.status, text=json.dumps(self.body), content_type="application/json")

    async def test_create_responses_map_to_outcomes(self) -> None:
        request = HelperSequentialJobCreateRequest(
            job_type="host_sequential_war_match", requested_by="7",
            payload=HelperSequentialWarPayload(host_clan_tag="#C", base_source="stored", player_tag="#P",
                                               districts=["Barbarian Camp"], slot=1, remove_bases_after=False),
        )
        capacity = "Sequential hosting needs at least two connected devices."
        cases = [
            (201, {"id": JOB_ID}, HelperJobCreated(id=JOB_ID)),
            (409, {"error": capacity}, HelperJobCreateRejected(error=capacity)),
            (409, {"error": "dup", "existing_job_id": JOB_ID, "existing_job_status": "running"},
             HelperJobCreateConflict(error="dup", existing_job_id=JOB_ID, existing_job_status="running")),
            (400, {"error": f"reserved_by_sequential_job:{JOB_ID}"},
             HelperJobCreateRejected(error=f"reserved_by_sequential_job:{JOB_ID}")),
            (503, {}, HelperJobCreateUnavailable()),
        ]
        async with aiohttp.ClientSession() as session:
            for status, body, expected in cases:
                with self.subTest(status=status, body=body):
                    self.status, self.body = status, body
                    outcome = await HelperSequentialClient(self.base_url, "key").create(session, request)
                    self.assertEqual(outcome, expected)

    def test_payload_cannot_mix_linked_and_stored_fields(self) -> None:
        with self.assertRaises(ValidationError):
            HelperSequentialWarPayload(host_clan_tag="#C", base_source="links", player_tag="#P",
                                       base_links=["https://x"], districts=["Barbarian Camp"])


class TestScoreStorage(unittest.IsolatedAsyncioTestCase):
    async def store(self, match: dict, run: dict, result: dict) -> list[str]:
        with patch.object(scores.Match, "get_pymongo_collection", return_value=FakeCollection(match)):
            return await scores.store_verified_scores(as_run(run), HelperSequentialResult.model_validate(result))

    async def test_live_district_is_stored_only_when_both_players_are_verified(self) -> None:
        match = match_document()
        result = helper_result({}, [verified("BC", "player_1"), verified("BC", "player_2", 2, 80.0),
                                    verified("BL", "player_1")])
        await self.store(match, run_document(), result)
        self.assertEqual(match["participants"][0]["result"][:2], ["3.100", None])
        self.assertEqual(match["participants"][1]["result"][:2], ["2.80", None])

    async def test_staff_entries_and_changed_pairings_are_never_overwritten(self) -> None:
        cases = {
            "staff entry": match_document(participants=[{"result": ["1.50"] + [None] * 6},
                                                        {"result": ["0.20"] + [None] * 6}]),
            "superseded": match_document(final_superseded=True),
            "new attempt": match_document(final_attempt=2),
            "recorded": match_document(final_result_id="QF1:1"),
        }
        result = helper_result({}, [verified("BC", "player_1"), verified("BC", "player_2")])
        for name, match in cases.items():
            with self.subTest(name):
                before = json.dumps(match["participants"])
                self.assertEqual(await self.store(match, run_document(), result), [])
                self.assertEqual(json.dumps(match["participants"]), before)

    async def test_fractional_destruction_is_left_for_staff(self) -> None:
        match = match_document()
        run = run_document(side=0, player_tags=["#P1"])
        await self.store(match, run, helper_result({}, [verified("BC", "player_1", 2, 85.5)], ("player_1",)))
        self.assertEqual(match["participants"][0]["result"][0], None)


class TestLedger(unittest.TestCase):
    def test_remaining_skips_played_and_stored_districts_and_keeps_final_order(self) -> None:
        live = run_document(snapshot=helper_result({"player_1": ["BC", "BL"], "player_2": ["BC"]}, []))
        match = MagicMock(participants=[MagicMock(result=[None, None, "3.100", None, None, None, None]),
                                        MagicMock(result=[None, None, "2.90", None, None, None, None])])
        self.assertEqual(runs.remaining_districts(match, [as_run(live)]),
                         ["Balloon Lagoon", "Dragon Cliffs", "Golem Quarry", "Skeleton Park", "Goblin Mines"])

    def test_different_player_orders_keep_partly_played_districts_in_continuation(self) -> None:
        snapshot = helper_result({"player_1": ["BC", "BL"], "player_2": []}, [])
        for index, code in enumerate(("BL", "BW", "BC")):
            record = snapshot["completion"]["rounds"][index]
            record["districts"]["player_2"] = code
            record["players"]["player_2"].update(posted=index < 2, completed=index < 2)
        run = as_run(run_document(snapshot=snapshot))
        match = MagicMock(participants=[MagicMock(result=[None] * 7), MagicMock(result=[None] * 7)])
        self.assertEqual(runs.remaining_districts(match, [run]),
                         [district for district in FINAL_DISTRICTS if district != "Balloon Lagoon"])
        result = HelperSequentialResult.model_validate(snapshot)
        self.assertEqual(runs.district_states(result)["BC"], "in progress")
        self.assertEqual(runs.district_states(result)["BL"], "played")

    def test_continuation_sends_only_remaining_links_in_final_order(self) -> None:
        request = service.continuation_request(as_run(run_document()), ["Dragon Cliffs", "Goblin Mines"], "9")
        self.assertEqual(request.payload.base_links_for_player_1, [LINKS[3], LINKS[6]])
        self.assertEqual(request.payload.base_links_for_player_2, [LINKS[3], LINKS[6]])
        self.assertEqual(request.requested_by, "9")
        self.assertNotIn("base_source", request.model_dump(exclude_none=True)["payload"])

    def test_second_continuation_slices_the_shortened_request(self) -> None:
        first = service.continuation_request(as_run(run_document()), list(FINAL_DISTRICTS[3:]), "9")
        shortened = run_document(create_request=first.model_dump(by_alias=True, exclude_none=True),
                                 districts=list(FINAL_DISTRICTS[3:]))
        second = service.continuation_request(as_run(shortened), list(FINAL_DISTRICTS[5:]), "9")
        self.assertEqual(second.payload.base_links_for_player_1, LINKS[5:])

    def test_stored_live_continuation_uses_current_type_for_old_and_new_runs(self) -> None:
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                payload = {
                    "host_clan_tag": "#CLAN", "player_1_tag": "#P1", "player_2_tag": "#P2",
                    "districts": list(FINAL_DISTRICTS), "remove_bases_after": False, "attacks_first": "#P2",
                }
                job_type = "host_sequential_live_match_on_stored"
                if legacy:
                    job_type = "host_sequential_live_match"
                    payload["base_source"] = "stored"
                saved = {"type": job_type, "requested_by": "7", "payload": payload}
                run = as_run(run_document(create_request=saved))
                remaining = ["Dragon Cliffs", "Goblin Mines"]
                request = service.continuation_request(run, remaining, "9")
                self.assertEqual(request.model_dump(by_alias=True, exclude_none=True), {
                    "type": "host_sequential_live_match_on_stored", "requested_by": "9", "payload": {
                        "host_clan_tag": "#CLAN", "player_1_tag": "#P1", "player_2_tag": "#P2",
                        "districts": remaining, "remove_bases_after": False, "attacks_first": "#P2",
                    },
                })
                self.assertEqual(run.create_request, saved)


class TestLifecycle(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.document = run_document(state="CREATING", job_id=None)
        self.collection = FakeCollection(self.document)
        self.enterContext(patch.object(HelperSequentialRun, "get_pymongo_collection", return_value=self.collection))
        self.enterContext(patch.object(HelperSequentialRun, "get",
                                       new=AsyncMock(side_effect=lambda _id: as_run(self.document))))
        self.rollback = self.enterContext(patch.object(service, "rollback_first_run",
                                                       new=AsyncMock(return_value="\n\nMatch aborted.")))
        self.publish = self.enterContext(patch.object(service, "publish_progress", new=AsyncMock()))
        self.enterContext(patch.object(service, "progress_message", new=AsyncMock(return_value=None)))
        self.watch = self.enterContext(patch.object(service, "start_watcher"))
        self.ctx = MagicMock(author=MagicMock(id=7), channel=MagicMock(id=55), send=AsyncMock())

    async def test_create_outcomes_set_state_rollback_and_watching(self) -> None:
        cases = [
            (HelperJobCreated(id=JOB_ID), None, "ACTIVE", True, False),
            (HelperJobCreateRejected(error="clan_has_open_job:x"), None, "TERMINAL", False, True),
            (HelperJobCreateRejected(error="busy"), PydanticObjectId(), "TERMINAL", False, False),
            (HelperJobCreateUnavailable(), None, "UNCERTAIN", False, False),
        ]
        for outcome, previous, state, watched, rolled_back in cases:
            with self.subTest(outcome=type(outcome).__name__, continuation=previous is not None):
                self.document.update(state="CREATING", open=True, job_id=None, previous_run_id=previous)
                self.watch.reset_mock()
                self.rollback.reset_mock()
                with patch.object(runs, "is_final_match", return_value=True):
                    await service.apply_create_outcome(MagicMock(), self.ctx, as_run(self.document), outcome)
                self.assertEqual(self.document["state"], state)
                self.assertEqual(self.watch.called, watched)
                self.assertEqual(self.rollback.await_count == 1, rolled_back or previous is not None)

    async def test_unpublished_progress_still_tracks_the_accepted_job(self) -> None:
        failure = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"), "Missing")
        self.publish.side_effect = failure
        await service.apply_create_outcome(MagicMock(), self.ctx, as_run(self.document), HelperJobCreated(id=JOB_ID))
        self.assertEqual(self.document["state"], "ACTIVE")
        self.watch.assert_called_once()
        self.assertIn("/match helper recover", self.ctx.send.await_args.args[0])

    async def test_recovers_legacy_stored_live_job_without_resubmitting_its_legacy_payload(self) -> None:
        rejected = HelperJobCreateRejected(error=f"reserved_by_sequential_job:{JOB_ID}")
        payload = {
            "host_clan_tag": "#CLAN", "player_1_tag": "#P1", "player_2_tag": "#P2",
            "base_source": "stored", "districts": list(FINAL_DISTRICTS),
        }
        self.document.update(state="UNCERTAIN", create_request={
            "type": "host_sequential_live_match", "requested_by": "7", "payload": payload,
        })
        result = helper_result({}, [])
        result["match"]["base_source"] = "stored"
        existing = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None, "result": result,
        })
        client = MagicMock(create=AsyncMock(return_value=rejected), read=AsyncMock(return_value=existing))
        with (
            patch.object(service, "helper_client", return_value=client),
            patch.object(service, "choose", new=AsyncMock(return_value="seq_adopt")),
        ):
            await service.adopt_uncertain(MagicMock(), self.ctx, as_run(self.document))
        sent = client.create.await_args.args[1].model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(sent["type"], "host_sequential_live_match_on_stored")
        self.assertNotIn("base_source", sent["payload"])
        self.assertEqual((self.document["state"], self.document["job_id"]), ("ACTIVE", JOB_ID))
        self.rollback.assert_not_awaited()

    async def test_stale_replay_recovery_confirmation_cannot_queue_another_helper_job(self) -> None:
        self.document.update(state="UNCERTAIN", replay_baseline={"Barbarian Camp": ["2.70", "1.40"]})
        opened = as_run(self.document)

        async def confirm_after_another_staff_member_closed_the_run(*args, **kwargs):
            self.document.update(state="TERMINAL", open=False, job_id=JOB_ID, job_status="succeeded")
            return "seq_adopt"

        helper = MagicMock(create=AsyncMock(return_value=HelperJobCreated(id=JOB_ID)))
        with (
            patch.object(service, "choose", new=AsyncMock(side_effect=confirm_after_another_staff_member_closed_the_run)),
            patch.object(service, "helper_client", return_value=helper),
        ):
            await service.adopt_uncertain(MagicMock(), self.ctx, opened)

        helper.create.assert_not_awaited()
        self.watch.assert_not_called()
        self.assertEqual(self.document["state"], "TERMINAL")
        self.assertIn("changed", self.ctx.send.await_args.args[0])

    async def test_uncertain_replay_keeps_its_state_when_helper_reports_a_conflicting_job(self) -> None:
        self.document.update(state="UNCERTAIN", replay_baseline={"Barbarian Camp": ["2.70", "1.40"]})
        helper = MagicMock(create=AsyncMock(return_value=HelperJobCreateRejected(
            error=f"reserved_by_sequential_job:{JOB_ID}")), read=AsyncMock())
        with (
            patch.object(service, "choose", new=AsyncMock(return_value="seq_adopt")),
            patch.object(service, "validate_replay_retry", new_callable=AsyncMock),
            patch.object(service, "helper_client", return_value=helper),
        ):
            await service.adopt_uncertain(MagicMock(), self.ctx, as_run(self.document))
        helper.create.assert_awaited_once()
        helper.read.assert_not_awaited()
        self.watch.assert_not_called()
        self.assertEqual(self.document["state"], "UNCERTAIN")
        self.assertIn("Wait for it to finish", self.ctx.send.await_args.args[0])

    async def test_cancel_always_asks_for_confirmation(self) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID, snapshot=None)
        client = MagicMock(cancel=AsyncMock())
        ctx = MagicMock(author=MagicMock(id=7), send=AsyncMock(return_value=MagicMock(id=56)))
        back = MagicMock(custom_id="seq_cancel_back", edit_origin=AsyncMock())
        bot = MagicMock(wait_for_component=AsyncMock(return_value=MagicMock(ctx=back)))
        with patch.object(service, "helper_client", return_value=client):
            await service.cancel_run(bot, ctx, self.document["_id"])
        prompt = ctx.send.await_args.kwargs
        self.assertTrue(prompt["ephemeral"])
        self.assertEqual([(button["label"], button["custom_id"])
                          for button in prompt["components"].to_dict()["components"]],
                         [("Cancel", "seq_cancel_confirm"), ("Back", "seq_cancel_back")])
        back.edit_origin.assert_awaited_once_with(components=[])
        client.cancel.assert_not_awaited()

    async def test_continue_after_rollback_explains_how_to_start_again(self) -> None:
        self.document.update(state="TERMINAL", open=False, job_status="cancelled", job_id=JOB_ID)
        match = MagicMock(host_clans=[None, None], participants=[MagicMock(result=[None] * 7)] * 2)
        for side, paused, command in (
            (LIVE_SIDE, [], "/match autostart"),
            (LIVE_SIDE, [0, 1], "/match autostart"),
            (1, [], "/match war autostart"),
            (1, [1], "/match war autostart"),
        ):
            with self.subTest(side=side, paused=paused):
                run = as_run({**self.document, "side": side})
                match.final_scouting_paused_sides = paused
                with (
                    patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
                    patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
                    patch.object(service, "latest_run", new=AsyncMock(return_value=run)),
                    patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
                ):
                    await service.continue_run(MagicMock(), self.ctx, run)
                launch.assert_not_awaited()
                reply = self.ctx.send.await_args.args[0]
                self.assertIn("was aborted" if paused else "has no active host", reply)
                self.assertIn(command, reply)
                self.assertNotIn("#CLAN", reply)
                self.assertEqual(service.progress_components(run, match), [])

    async def test_continue_after_a_clan_change_displays_the_clan_names(self) -> None:
        self.document.update(state="TERMINAL", open=False, job_status="cancelled", job_id=JOB_ID)
        run = as_run(self.document)
        match = MagicMock(host_clans=["#NEW", None], participants=[MagicMock(result=[None] * 7)] * 2)
        clans = MagicMock(to_list=AsyncMock(return_value=[
            SimpleNamespace(tag="#CLAN", name="Lantern Clan"), SimpleNamespace(tag="#NEW", name="Maple Clan"),
        ]))
        with (
            patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
            patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
            patch.object(service, "latest_run", new=AsyncMock(return_value=run)),
            patch.object(service.resources.Clan, "find", return_value=clans),
            patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
        ):
            await service.continue_run(MagicMock(), self.ctx, run)
        launch.assert_not_awaited()
        reply = self.ctx.send.await_args.args[0]
        self.assertIn("**Lantern Clan**", reply)
        self.assertIn("**Maple Clan**", reply)
        self.assertNotIn("#CLAN", reply)
        self.assertNotIn("#NEW", reply)

    async def test_continue_in_the_same_clan_uses_the_existing_match_start(self) -> None:
        self.document.update(state="TERMINAL", open=False, job_status="cancelled", job_id=JOB_ID, start_message_id=55)
        run = as_run(self.document)
        match = MagicMock(host_clans=["#CLAN", None], old_msgs=[55, None],
                          participants=[MagicMock(result=[None] * 7)] * 2)
        with (
            patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
            patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
            patch.object(service, "latest_run", new=AsyncMock(return_value=run)),
            patch.object(service, "match_runs", new=AsyncMock(return_value=[run])),
            patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
        ):
            await service.continue_run(MagicMock(), self.ctx, run)
        self.assertEqual(launch.await_args.args[2].start_message_id, 55)
        self.assertEqual(launch.await_args.args[2].previous_run_id, run.id)
        self.assertEqual(service.progress_components(run, match)[0].components[0].label, "Continue")

    async def test_continue_picker_can_skip_districts_and_resume_them_from_the_original_layouts(self) -> None:
        self.document.update(state="TERMINAL", open=False, snapshot=helper_result({
            "player_1": ["BC"], "player_2": ["BC"],
        }, []))
        original = as_run(self.document)
        match = MagicMock(host_clans=["#CLAN", None], participants=[MagicMock(result=[None] * 7)] * 2)
        ctx = MagicMock(author=MagicMock(id=7), defer=AsyncMock(), send=AsyncMock(),
                        custom_id=f"helper_seq_continue|{original.id}")
        selected = SimpleNamespace(author=ctx.author, values=["Dragon Cliffs"], defer=AsyncMock(), edit_origin=AsyncMock(),
                                   custom_id="seq_continue_district")
        confirm = SimpleNamespace(defer=AsyncMock(), edit_origin=AsyncMock(), custom_id="seq_continue_confirm")

        async def select_and_confirm(*, check, **_):
            check(SimpleNamespace(ctx=selected))
            return SimpleNamespace(ctx=confirm)

        bot = MagicMock(wait_for_component=AsyncMock(side_effect=select_and_confirm))
        cog = MatchCommands(bot)
        with (
            patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
            patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
            patch.object(service, "latest_run", new=AsyncMock(side_effect=lambda *_: as_run(self.document))),
            patch.object(service, "match_runs", new=AsyncMock(side_effect=lambda *_: [original, as_run(self.document)])),
            patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
        ):
            await cog.helper_sequential_continue.callback.func(cog, ctx)
            prompt = ctx.send.await_args.kwargs
            self.assertTrue(prompt["ephemeral"])
            options = prompt["components"][0].to_dict()["components"][0]["options"]
            self.assertEqual([option["value"] for option in options if option["default"]], ["Balloon Lagoon"])
            selected.edit_origin.assert_awaited_once()
            confirm.edit_origin.assert_awaited_once_with(components=[])
            first = launch.await_args.args[2]
            self.assertEqual(first.districts, list(FINAL_DISTRICTS[3:]))
            self.assertEqual(first.request.payload.base_links_for_player_1, LINKS[3:])

            completed = ["DC", "GQ", "SP", "GM"]
            self.document.update(_id=PydanticObjectId(), previous_run_id=original.id, districts=first.districts,
                                 create_request=first.request.model_dump(by_alias=True, exclude_none=True),
                                 snapshot=helper_result({"player_1": completed, "player_2": completed}, []))
            bot.wait_for_component.side_effect = None
            bot.wait_for_component.return_value = SimpleNamespace(ctx=confirm)
            ctx.custom_id = f"helper_seq_continue|{self.document['_id']}"
            await cog.helper_sequential_continue.callback.func(cog, ctx)
            second = launch.await_args.args[2]
            self.assertEqual(second.districts, ["Balloon Lagoon", "Builder's Workshop"])
            self.assertEqual(second.request.payload.base_links_for_player_1, LINKS[1:3])
            self.assertEqual(second.request.payload.base_links_for_player_2, LINKS[1:3])

    async def test_continue_confirmation_rechecks_a_district_that_was_scored_while_the_picker_was_open(self) -> None:
        self.document.update(state="TERMINAL", open=False)
        run = as_run(self.document)
        match = MagicMock(host_clans=["#CLAN", None], participants=[
            MagicMock(result=[None] * 7), MagicMock(result=[None] * 7),
        ])
        ctx = MagicMock(author=MagicMock(id=7), defer=AsyncMock(), send=AsyncMock())

        async def confirm(**_):
            match.participants[0].result[0] = "3.100"
            match.participants[1].result[0] = "2.85"
            return SimpleNamespace(ctx=SimpleNamespace(defer=AsyncMock(), edit_origin=AsyncMock()))

        bot = MagicMock(wait_for_component=AsyncMock(side_effect=confirm))
        with (
            patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
            patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
            patch.object(service, "latest_run", new=AsyncMock(return_value=run)),
            patch.object(service, "match_runs", new=AsyncMock(return_value=[run])),
            patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
        ):
            await service.continue_from_button(bot, ctx, run.id)
        launch.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("no longer pending", ctx.send.await_args.args[0])
        self.assertEqual([participant.result[0] for participant in match.participants], ["3.100", "2.85"])

    async def test_picker_acknowledges_new_choices_and_confirmation_during_a_slow_edit(self) -> None:
        bot = ipy.Client()
        registered, editing, next_choice, confirmed, release_edit = (anyio.Event() for _ in range(5))
        author = SimpleNamespace(id=7)
        ctx = SimpleNamespace(author=author, send=AsyncMock(return_value=SimpleNamespace(id=55)))
        received = []
        wait_for = bot.wait_for

        def register(*args, **kwargs):
            wait = wait_for(*args, **kwargs)
            registered.set()
            return wait

        async def edit_selection(**kwargs):
            editing.set()
            await release_edit.wait()

        async def acknowledge_choice(**kwargs):
            next_choice.set()

        async def acknowledge_confirm(**kwargs):
            confirmed.set()

        selected = SimpleNamespace(author=author, message_id=55, custom_id="seq_continue_district",
                                   values=["Dragon Cliffs"], defer=AsyncMock(), edit_origin=edit_selection)
        next_selected = SimpleNamespace(author=author, message_id=55, custom_id="seq_continue_district",
                                        values=["Golem Quarry"], defer=acknowledge_choice, edit_origin=AsyncMock())
        confirm = SimpleNamespace(author=author, message_id=55, custom_id="seq_continue_confirm",
                                  defer=acknowledge_confirm, edit_origin=AsyncMock())

        async def pick():
            received.append(await service._pick_start_district(bot, ctx, list(FINAL_DISTRICTS)))

        with patch.object(bot, "wait_for", side_effect=register), anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(pick)
                await registered.wait()
                bot.dispatch(ipy.events.Component(ctx=selected))
                await editing.wait()
                bot.dispatch(ipy.events.Component(ctx=next_selected))
                bot.dispatch(ipy.events.Component(ctx=confirm))
                await next_choice.wait()
                await confirmed.wait()
                release_edit.set()

        self.assertEqual(received, ["Golem Quarry"])
        next_selected.edit_origin.assert_awaited_once()
        confirm.edit_origin.assert_awaited_once_with(components=[])

    async def test_cancel_finishes_rollback_before_a_pending_continue_can_relaunch(self) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID, start_message_id=55)
        match = MagicMock(host_clans=["#CLAN", None], participants=[MagicMock(result=[None] * 7)] * 2)
        cancelled = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "cancelled", "created_at": 1,
            "started_at": 2, "finished_at": 3, "error": None, "result": None,
        })
        client = MagicMock(cancel=AsyncMock(), read=AsyncMock(return_value=cancelled))
        bot = MagicMock()
        continue_ctx = MagicMock(author=MagicMock(id=7), defer=AsyncMock(), send=AsyncMock())
        rolling_back = anyio.Event()
        continue_clicked = anyio.Event()
        allow_rollback = anyio.Event()

        async def rollback(_ctx, _run):
            rolling_back.set()
            await allow_rollback.wait()
            match.host_clans[0] = None
            return "\n\nMatch aborted."

        async def press_continue():
            continue_clicked.set()
            await service.continue_from_button(bot, continue_ctx, self.document["_id"])

        self.rollback.side_effect = rollback
        with (
            patch.object(service, "choose", new=AsyncMock(return_value="seq_cancel_confirm")),
            patch.object(service, "helper_client", return_value=client),
            patch.object(service.Match, "get", new=AsyncMock(return_value=match)),
            patch.object(service, "stale_reason", new=AsyncMock(return_value=None)),
            patch.object(service, "latest_run", new=AsyncMock(side_effect=lambda *_: as_run(self.document))),
            patch.object(service, "match_runs", new=AsyncMock(return_value=[as_run(self.document)])),
            patch.object(service, "finish_stored_sides", new_callable=AsyncMock),
            patch.object(service, "progress_message", new=AsyncMock(return_value=MagicMock())),
            patch.object(service, "edit_channel_message", new_callable=AsyncMock) as edit,
            patch.object(service, "launch_run", new_callable=AsyncMock) as launch,
            anyio.fail_after(3),
        ):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(service.cancel_run, bot, self.ctx, self.document["_id"])
                await rolling_back.wait()
                tasks.start_soon(press_continue)
                await continue_clicked.wait()
                allow_rollback.set()
        self.assertEqual(self.document["state"], "TERMINAL")
        self.assertIsNone(match.host_clans[0])
        launch.assert_not_awaited()
        edit.assert_awaited_once()
        self.assertEqual(edit.await_args.kwargs["components"], [])
        self.assertIn("/match autostart", continue_ctx.send.await_args.args[0])

    async def apply(self, outcome, stale: str | None = None) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID)
        with patch.object(service, "store_verified_scores", new=AsyncMock(return_value=["Barbarian Camp P1 3.100"])) \
                as store, patch.object(service, "finish_stored_sides", new=AsyncMock()) as finish:
            await service.apply_read(MagicMock(), as_run(self.document), MagicMock(), outcome, stale)
        self.store, self.finish = store, finish

    async def test_terminal_job_stores_scores_before_closing(self) -> None:
        job = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match_on_stored", "status": "succeeded", "created_at": 1,
            "started_at": 2, "finished_at": 3, "error": None,
            "result": helper_result({"player_1": CODES, "player_2": CODES}, [verified("BC", "player_1")]),
        })
        await self.apply(job)
        self.store.assert_awaited_once()
        self.finish.assert_awaited_once()
        self.assertFalse(self.document["open"])
        self.assertEqual(self.document["job_status"], "succeeded")
        self.assertIn("district_played", [event["kind"] for event in self.document["events"]])

    async def test_tracking_failure_keeps_the_run_open_and_counts_failures(self) -> None:
        await self.apply(HelperTrackingFailure(job_id=JOB_ID, reason="Helper could not be reached."))
        await self.apply(HelperTrackingFailure(job_id=JOB_ID, reason="Helper could not be reached."))
        self.assertTrue(self.document["open"])
        self.assertEqual(self.document["poll_failures"], 2)
        self.assertEqual([event["kind"] for event in self.document["events"]], ["poll_failed"])
        description = ui.progress_embed(as_run(self.document), None).description
        self.assertIn("Helper status unavailable", description)
        self.assertIn("last attempt", description)

    async def test_cancelled_final_cancels_helper_and_closes_without_finalizing_scores(self) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID, final_id=str(PydanticObjectId()))
        source = MagicMock(final_superseded=True, final_contestant_revision=1, final_attempt=1,
                           final_result_id=None)
        running = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None, "result": None,
        })
        cancelled = running.model_copy(update={"status": "cancelled", "finished_at": 3})
        client = MagicMock(read=AsyncMock(side_effect=[running, cancelled]), cancel=AsyncMock())
        with patch.object(service.Match, "get", new=AsyncMock(return_value=source)), \
                patch.object(service.EloFinal, "get", new=AsyncMock(return_value=MagicMock(state="CANCELLED"))), \
                patch.object(service, "helper_client", return_value=client), \
                patch.object(service, "refresh_progress", new=AsyncMock()), \
                patch.object(service, "store_verified_scores", new=AsyncMock()) as store, \
                patch.object(service, "finish_stored_sides", new=AsyncMock()) as finish:
            closed = await service.sync_run(MagicMock(), self.document["_id"])
        client.cancel.assert_awaited_once()
        self.assertEqual(client.cancel.await_args.args[1], JOB_ID)
        self.assertFalse(closed.open)
        self.assertEqual(closed.state, "TERMINAL")
        self.assertEqual(closed.job_status, "cancelled")
        self.assertEqual(closed.close_reason, "The Final was cancelled.")
        store.assert_not_awaited()
        finish.assert_not_awaited()

    async def test_stale_run_stays_open_while_helper_is_still_hosting(self) -> None:
        running = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None, "result": None,
        })
        await self.apply(running, stale="The Final result was submitted.")
        self.assertTrue(self.document["open"])
        self.store.assert_not_awaited()


class TestStatus(unittest.TestCase):
    def test_progress_distinguishes_sent_attacked_and_durably_stored_districts(self) -> None:
        snapshot = helper_result(
            {"player_1": ["BC", "BW"], "player_2": ["BC", "BW"]},
            [verified("BW", "player_1"), verified("BW", "player_2")],
        )
        snapshot["stage"] = "SCORING"
        for round_ in snapshot["completion"]["rounds"][3:]:
            for player in round_["players"].values():
                player["posted"] = False
        match = MagicMock(participants=[
            MagicMock(result=["3.100", None, "3.100", None, None, None, None]),
            MagicMock(result=["2.85", None, None, None, None, None, None]),
        ])
        run = as_run(run_document(snapshot=snapshot, last_polled_at=1))
        description = ui.progress_embed(run, match).to_dict()["description"]
        self.assertIn("**Reading scores**", description)
        rows = description.splitlines()
        self.assertTrue(any(line.startswith(str(ui.SUCCESS_EMOJI)) and "Barbarian Camp" in line
                            and "Scores stored" in line and "85%" in line for line in rows))
        self.assertTrue(any("Balloon Lagoon" in line and "Challenge sent" in line for line in rows))
        workshop = next(line for line in rows if "Builder's Workshop" in line)
        self.assertIn("Attacked", workshop)
        self.assertIn("Scores pending", workshop)
        self.assertNotIn(str(ui.SUCCESS_EMOJI), workshop)
        self.assertIn("➖ Dragon Cliffs", rows)

    def test_terminal_progress_marks_attacked_district_with_no_score_for_review(self) -> None:
        snapshot = helper_result({"player_1": ["BC"]}, [], ("player_1",))
        run = as_run(run_document(snapshot=snapshot, side=1, state="TERMINAL", open=False,
                                  close_reason="Helper job failed.", districts=["Barbarian Camp"]))
        match = MagicMock(participants=[
            MagicMock(result=["3.100"] + [None] * 6),
            MagicMock(result=[None] * 7),
        ])
        description = ui.progress_embed(run, match).to_dict()["description"]
        self.assertIn("Helper job failed.", description)
        self.assertIn("Attacked", description)
        self.assertIn("Scores missing", description)
        self.assertNotIn(str(ui.SUCCESS_EMOJI), description)
        match.participants[1].result[0] = "2.85"
        description = ui.progress_embed(run, match).to_dict()["description"]
        self.assertIn("Scores stored · P2 `2⭐ 85%`", description)
        self.assertIn(str(ui.SUCCESS_EMOJI), description)

    def test_live_change_with_same_status_and_count_is_flagged(self) -> None:
        saved = helper_result({}, [])
        fresh = deepcopy(saved)
        for player in saved["completion"]["rounds"][0]["players"].values():
            player["posted"] = False
        run = as_run(run_document(snapshot=saved, job_status="running", error=None))
        live = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None,
            "result": {**fresh, "stage": "WAITING"},
        })
        embed = ui.status_embed(run, None, [], live, None, "Lantern Clan")
        live_field = next(field for field in embed.fields if field.name == "**Helper (live)**")
        self.assertIn("Differs from the saved state", live_field.value)
        overview = next(field.value for field in embed.fields if field.name == "**Run**")
        self.assertIn("Lantern Clan", overview)
        self.assertNotIn("#CLAN", overview)
        districts = next(field.value for field in embed.fields if field.name == "**Districts**")
        self.assertIn(f"{ui.CLASHING_SWORD_EMOJI} **Barbarian Camp** · Challenge sent", districts)


class TestRunList(unittest.IsolatedAsyncioTestCase):
    async def page(self, page: int, count: int = 23):
        documents = [as_run(run_document(final_node_id=f"N{index}")) for index in range(count)]
        cursor = MagicMock()
        cursor.sort.return_value.to_list = AsyncMock(return_value=documents)
        clans = MagicMock(to_list=AsyncMock(return_value=[SimpleNamespace(tag="#CLAN", name="Lantern Clan")]))
        with (
            patch.object(HelperSequentialRun, "find", return_value=cursor),
            patch.object(service.resources.Clan, "find", return_value=clans),
        ):
            return await service.list_page("all", "#CLAN", page)

    async def test_pages_cover_every_run_and_disable_arrows_at_the_ends(self) -> None:
        cases = {0: ("N0", True, False, "1/3"), 1: ("N10", False, False, "2/3"), 2: ("N20", False, True, "3/3"),
                 9: ("N20", False, True, "3/3")}
        for page, (first, left_off, right_off, label) in cases.items():
            with self.subTest(page=page):
                embed, components, total = await self.page(page)
                left, number, right = components[0].components
                self.assertEqual(total, 3)
                self.assertIn(f"**{first}**", embed.description.splitlines()[0])
                self.assertIn("**Lantern Clan**", embed.description)
                self.assertNotIn("#CLAN", embed.description)
                self.assertEqual((left.disabled, right.disabled, number.label), (left_off, right_off, label))
                self.assertTrue(right.custom_id.endswith("|all|#CLAN"))

    async def test_other_users_and_invalid_jumps_do_not_change_the_page(self) -> None:
        cog = MatchCommands(MagicMock())
        message = MagicMock(interaction=MagicMock(user=MagicMock(id=7)))
        stranger = MagicMock(author=MagicMock(id=8), message=message, custom_id="helper_runs_right|1|all|#CLAN",
                             send=AsyncMock(), edit_origin=AsyncMock())
        await cog.helper_runs_arrow.callback.func(cog, stranger)
        stranger.edit_origin.assert_not_awaited()
        self.assertIn("another user's components", stranger.send.await_args.args[0])

        owner = MagicMock(author=MagicMock(id=7), message=message, custom_id="helper_runs_jump|all|#CLAN",
                          send=AsyncMock(), edit=AsyncMock())
        with patch.object(service, "list_page", new=AsyncMock(return_value=(MagicMock(), [], 3))):
            await cog.helper_runs_jump.callback.func(cog, owner, page="9")
        owner.edit.assert_not_awaited()
        self.assertIn("between 1 and 3", owner.send.await_args.args[0])

    async def test_single_page_keeps_disabled_arrows_and_jump_button(self) -> None:
        _, components, total = await self.page(0, count=0)
        left, number, right = components[0].components
        self.assertEqual((total, left.disabled, right.disabled, number.label), (1, True, True, "1/1"))


class TestGuardAndForm(unittest.IsolatedAsyncioTestCase):
    async def test_manual_district_store_is_refused_while_a_run_is_open(self) -> None:
        cog = MatchCommands(MagicMock())
        match = MagicMock(format="elo_final_seven", final_districts=list(FINAL_DISTRICTS))
        match.get_pymongo_collection.return_value.update_one = AsyncMock()
        ctx = MagicMock(channel=MagicMock(id=55), defer=AsyncMock(), send=AsyncMock())
        with patch("elo_bot.extensions.match.result_store.Match") as model, \
                patch("elo_bot.extensions.match.result_store.result_input_error", return_value=None), \
                patch("elo_bot.extensions.match.result_store.require_source_result_role", new=AsyncMock()), \
                patch("elo_bot.extensions.match.result_store.open_run_refusal",
                      new=AsyncMock(return_value="Helper is running a sequential match here.")):
            model.find_one = AsyncMock(return_value=match)
            await cog.match_result_store.callback.func(cog, ctx, player=0, district=FINAL_DISTRICTS[0],
                                                      stars=3, destruction=100)
        match.get_pymongo_collection.return_value.update_one.assert_not_awaited()
        self.assertIn("sequential match", ctx.send.await_args.args[0])

    async def test_guard_only_queries_final_matches(self) -> None:
        with patch.object(runs, "open_runs", new=AsyncMock(return_value=[MagicMock()])) as open_runs:
            self.assertIsNone(await runs.open_run_refusal(MagicMock(format="ordinary_three", token="T"), 0))
            open_runs.assert_not_awaited()
            refusal = await runs.open_run_refusal(MagicMock(format="elo_final_seven", token="T"), 0)
        self.assertIn("/match helper recover", refusal)
        open_runs.assert_awaited_once_with("T", 0)

    async def test_result_form_prefills_missing_scores_without_moving_saved_districts(self) -> None:
        node = MagicMock(id="QF1", contestants=["1", "2"], result_id=None,
                         result_version=0, contestant_revision=1, attempt=1)
        final = MagicMock(season_number=3, player_nicknames={"1": "Alpha", "2": "Beta"}, nodes={"QF1": node})
        stored = ["3.100", None, "2.85", "", "1.44", None, "3.100"]
        source = MagicMock(final_node_id="QF1", participants=[
            MagicMock(user_id="1", result=stored.copy()), MagicMock(user_id="2", result=[None] * 7),
        ])
        ctx = MagicMock(author=MagicMock(id=9), send_modal=AsyncMock())
        with patch.object(result_ui, "require_source_result_role", new=AsyncMock(return_value=final)):
            await result_ui.show_source_result_form(ctx, source)
        modal = ctx.send_modal.await_args.args[0]
        self.assertEqual([component.value.splitlines() for component in modal.components[:2]], [
            ["3.100", "0.00", "2.85", "0.00", "1.44", "0.00", "3.100"], ["0.00"] * 7,
        ])
        self.assertEqual(source.participants[0].result, stored)
        self.assertEqual(source.participants[1].result, [None] * 7)

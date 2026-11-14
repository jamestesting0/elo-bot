# noqa: SIZE_OK - client, storage, ledger and lifecycle cases share one in-memory run and match store
import json
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import interactions as ipy
from aiohttp import web
from beanie import PydanticObjectId
from pydantic import ValidationError

import elo_bot.features.hosting.sequential.service as service
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.finals.result_ui import result_form
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

    def test_continuation_sends_only_remaining_links_in_final_order(self) -> None:
        request = service.continuation_request(as_run(run_document()), ["Dragon Cliffs", "Goblin Mines"], "9")
        self.assertEqual(request.payload.base_links_for_player_1, [LINKS[3], LINKS[6]])
        self.assertEqual(request.payload.base_links_for_player_2, [LINKS[3], LINKS[6]])
        self.assertEqual(request.requested_by, "9")

    def test_second_continuation_slices_the_shortened_request(self) -> None:
        first = service.continuation_request(as_run(run_document()), list(FINAL_DISTRICTS[3:]), "9")
        shortened = run_document(create_request=first.model_dump(by_alias=True, exclude_none=True),
                                 districts=list(FINAL_DISTRICTS[3:]))
        second = service.continuation_request(as_run(shortened), list(FINAL_DISTRICTS[5:]), "9")
        self.assertEqual(second.payload.base_links_for_player_1, LINKS[5:])


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

    async def test_cancel_always_asks_for_confirmation(self) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID, snapshot=None)
        client = MagicMock(cancel=AsyncMock())
        ctx = MagicMock(author=MagicMock(id=7), send=AsyncMock())
        with patch.object(service, "choose", new=AsyncMock(return_value="seq_cancel_back")) as choose, \
                patch.object(service, "helper_client", return_value=client):
            await service.cancel_run(MagicMock(), ctx, self.document["_id"])
        choose.assert_awaited_once()
        client.cancel.assert_not_awaited()

    async def apply(self, outcome, stale: str | None = None) -> None:
        self.document.update(state="ACTIVE", job_id=JOB_ID)
        with patch.object(service, "store_verified_scores", new=AsyncMock(return_value=["Barbarian Camp P1 3.100"])) \
                as store, patch.object(service, "finish_stored_sides", new=AsyncMock()) as finish:
            await service.apply_read(MagicMock(), as_run(self.document), MagicMock(), outcome, stale)
        self.store, self.finish = store, finish

    async def test_terminal_job_stores_scores_before_closing(self) -> None:
        job = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "succeeded", "created_at": 1,
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

    async def test_stale_run_stays_open_while_helper_is_still_hosting(self) -> None:
        running = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None, "result": None,
        })
        await self.apply(running, stale="The Final result was submitted.")
        self.assertTrue(self.document["open"])
        self.store.assert_not_awaited()


class TestStatus(unittest.TestCase):
    def test_live_change_with_same_status_and_count_is_flagged(self) -> None:
        saved = helper_result({}, [])
        run = as_run(run_document(snapshot=saved, job_status="running", error=None))
        live = HelperSequentialJob.model_validate({
            "id": JOB_ID, "type": "host_sequential_live_match", "status": "running", "created_at": 1,
            "started_at": 2, "finished_at": None, "error": None,
            "result": {**saved, "stage": "WAITING_FOR_ATTACKS"},
        })
        embed = ui.status_embed(run, None, [], live, None)
        live_field = next(field for field in embed.fields if field.name == "**Helper (live)**")
        self.assertIn("Differs from the saved state", live_field.value)


class TestRunList(unittest.IsolatedAsyncioTestCase):
    async def page(self, page: int, count: int = 23):
        documents = [as_run(run_document(final_node_id=f"N{index}")) for index in range(count)]
        cursor = MagicMock()
        cursor.sort.return_value.to_list = AsyncMock(return_value=documents)
        with patch.object(HelperSequentialRun, "find", return_value=cursor):
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

    def test_result_form_prefills_stored_scores_without_an_earlier_result(self) -> None:
        final = MagicMock(season_number=3, player_nicknames={"1": "Alpha", "2": "Beta"})
        node = MagicMock(id="QF1", contestants=["1", "2"], result_version=0, contestant_revision=1, attempt=1)
        stored = {"1": ["3.100"] * 7}
        modal = result_form(final, node, "9", stored=stored)
        self.assertEqual([component.value for component in modal.components[:2]], ["\n".join(["3.100"] * 7), None])

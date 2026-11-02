import unittest
from contextlib import suppress
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
import anyio
from beanie import PydanticObjectId
from bson import BSON
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import AutoReconnect, DuplicateKeyError, OperationFailure, PyMongoError
from pymongo.results import InsertOneResult, UpdateResult

import cogs.base_commands as commands
from database_models import BaseLayout, PendingBaseSubmission
from database_models.base_submission_models import SubmissionRejection


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestBaseSubmissionResolution(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.row = {
            "_id": "9001", "link": BASE_LINK, "image_url": "https://cdn.example/base.webp",
            "submitter_id": "123", "submitter_display": "Submitter", "builder_id": "456",
            "builder_display": "Builder", "district": "Barbarian Camp", "proposed_quality": "Good",
            "reason": None,
        }
        self.bases = {}
        self.events: list[str] = []
        self.fail_phase: str | None = None
        self.uncertain_insert = False
        for model in (PendingBaseSubmission, BaseLayout):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
        self.enterContext(patch.object(BaseLayout, "link", ExpressionField("link"), create=True))

        async def load_pending(**_kwargs):
            return deepcopy(self.row)

        async def update_pending(query, update):
            updates = update["$set"]
            phase = updates.get("resolution.phase") or updates.get("resolution", {}).get("phase")
            if phase == self.fail_phase:
                raise OperationFailure("synthetic pending write failure")
            for key, expected in query.items():
                current = self.row
                for field in key.split("."):
                    current = current.get(field) if current is not None else None
                if current != expected:
                    return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            for key, value in updates.items():
                target = self.row
                *fields, last = key.split(".")
                for field in fields:
                    target = target[field]
                target[last] = deepcopy(value)
            self.events.append(f"persist:{phase}")
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        self.pending_collection = MagicMock(
            find_one=AsyncMock(side_effect=load_pending),
            update_one=AsyncMock(side_effect=update_pending),
            delete_one=AsyncMock(side_effect=OperationFailure("synthetic delete failure")),
        )
        self.enterContext(patch.object(PendingBaseSubmission, "get_pymongo_collection",
                                       return_value=self.pending_collection))

        async def load_base(*_args, **kwargs):
            query = kwargs["filter"]
            return next((deepcopy(row) for row in self.bases.values()
                         if all(row.get(key) == value for key, value in query.items())), None)

        async def insert_base(row, **_kwargs):
            row.setdefault("_id", PydanticObjectId())
            if row["_id"] in self.bases or any(base["link"] == row["link"] for base in self.bases.values()):
                raise DuplicateKeyError("synthetic unique base constraint")
            self.bases[row["_id"]] = deepcopy(row)
            self.events.append("base:insert")
            if self.uncertain_insert:
                self.uncertain_insert = False
                raise AutoReconnect("synthetic lost insert acknowledgement")
            return InsertOneResult(row["_id"], acknowledged=True)

        self.base_collection = MagicMock(
            find_one=AsyncMock(side_effect=load_base), insert_one=AsyncMock(side_effect=insert_base),
        )
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=self.base_collection))
        self.message = MagicMock(spec=ipy.Message, id=9001)
        self.message.embeds = []

        async def edit(**kwargs):
            if "embed" in kwargs:
                self.message.embeds = [kwargs["embed"]]
            if "components" in kwargs:
                self.message.components = kwargs["components"]
            self.events.append("review:edit")

        self.message.edit = AsyncMock(side_effect=edit)
        self.submitter = MagicMock(send=AsyncMock())
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.user = SimpleNamespace(id=42)
        self.bot.fetch_user = AsyncMock(return_value=self.submitter)
        self.cog = commands.BaseCommands(self.bot)

    def context(self):
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.message = self.message
        ctx.author = SimpleNamespace(id=789)
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Member {user_id}"),
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    async def accept(self, ctx, quality="Great", builder="456") -> None:
        await self.cog.accept_submission_modal.callback.func(
            self.cog, ctx, quality=[quality], builder=[builder], note="Original decision",
        )

    async def reject(self, ctx) -> None:
        await self.cog.reject_submission_modal.callback.func(self.cog, ctx, reason="Stale rejection")

    def review_status(self) -> str:
        self.assertTrue(self.message.embeds, "The review has no confirmed terminal state")
        return next(field.value for field in self.message.embeds[0].fields if field.name == "Review Status")

    async def test_duplicate_accept_updates_status_and_preserves_existing_base(self) -> None:
        # Given a base added to the bank after submission, before Accept is clicked.
        with patch.object(commands.BaseLayout, "find_one", new=AsyncMock(return_value=object())):
            ctx = self.context()
            ctx.send_modal = AsyncMock()
            # When a moderator clicks Accept and later retries the stale modal.
            await self.cog.accept_submission_button.callback.func(self.cog, ctx)
            await self.accept(self.context())

        # Then a durable rejection explains the duplicate and the bank is untouched.
        self.assertEqual(self.row["resolution"]["phase"], "rejected")
        self.assertEqual(self.row["resolution"]["reason"], "This base link already exists in the base bank.")
        self.assertEqual(self.review_status(), "Rejected by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertIn("already exists in the base bank", next(
            field.value for field in self.message.embeds[0].fields if field.name == "Rejection Reason"
        ))
        self.assertTrue(all(button.disabled for button in self.message.components.components))
        self.assertEqual(self.events[0], "persist:rejected")
        ctx.send_modal.assert_not_awaited()
        self.base_collection.insert_one.assert_not_awaited()
        self.submitter.send.assert_awaited_once()

    async def test_duplicate_rejection_save_failure_keeps_review_retryable(self) -> None:
        # Given a duplicate discovered after the Accept modal was opened, but saving fails.
        self.fail_phase = "rejected"
        with patch.object(commands.BaseLayout, "find_one", new=AsyncMock(return_value=object())):
            ctx = self.context()
            await self.accept(ctx)
        # Then no terminal message or notification precedes the saved rejection.
        self.assertIsNone(self.row.get("resolution"))
        self.message.edit.assert_not_awaited()
        self.submitter.send.assert_not_awaited()
        self.base_collection.insert_one.assert_not_awaited()
        self.assertIn("could not confirm", ctx.send.await_args.args[0])

    async def test_duplicate_rejection_publication_failure_can_be_retried(self) -> None:
        # Given a duplicate whose rejection saves but Discord cannot publish it.
        with patch.object(commands.BaseLayout, "find_one", new=AsyncMock(return_value=object())):
            with patch.object(self.message, "edit", new=AsyncMock(side_effect=ipy.errors.HTTPException(
                MagicMock(status=500), "edit failed",
            ))):
                with self.assertRaises(ipy.errors.HTTPException):
                    await self.accept(self.context())
            # When a stale Accept retries after the publication failure.
            await self.accept(self.context())
        # Then it republishes the saved rejection, without inserting a base.
        self.assertEqual(self.row["resolution"]["phase"], "rejected")
        self.assertEqual(self.review_status(), "Rejected by **[Member 789](<https://discordapp.com/users/789>)**")
        self.base_collection.insert_one.assert_not_awaited()

    async def test_accepted_decision_survives_failed_delete_and_stale_reject(self) -> None:
        # Given: removing the pending row would fail after the base insert.
        first, stale = self.context(), self.context()

        # When: Accept finishes and an already-open Reject is later submitted.
        with suppress(PyMongoError):
            await self.accept(first)
        with suppress(PyMongoError):
            await self.reject(stale)

        # Then: the stored acceptance is irreversible and visible with one notification.
        self.assertEqual(self.review_status(), "Accepted by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertEqual(self.row["resolution"]["phase"], "accepted")
        self.assertEqual(len(self.bases), 1)
        self.submitter.send.assert_awaited_once()
        self.pending_collection.delete_one.assert_not_awaited()

    async def test_retry_after_uncertain_insert_uses_saved_id_and_original_decision(self) -> None:
        # Given: Mongo stores the base but its insert acknowledgement is lost.
        self.uncertain_insert = True
        with suppress(PyMongoError):
            await self.accept(self.context())
        self.bot.ext = {}
        self.cog = commands.BaseCommands(self.bot)

        # When: a fresh cog retries Accept with different modal selections.
        retry = self.context()
        await self.accept(retry, quality="Excellent", builder="999")

        # Then: exactly one base exists with the original decision and a terminal review.
        self.assertEqual(self.review_status(), "Accepted by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertEqual(self.row["resolution"]["phase"], "accepted")
        stored = self.bases[self.row["resolution"]["base_id"]]
        self.assertEqual((stored["quality"], stored["builder_id"]), ("Great", "456"))
        self.assertEqual(stored["fingerprint_extraction"]["status"], "pending")
        self.assertEqual(stored["fingerprint_extraction"]["source_revision"], stored["image_revision"])
        self.assertIn("Fingerprint extraction: pending", retry.send.await_args.args[0])
        self.base_collection.insert_one.assert_awaited_once()
        self.submitter.send.assert_awaited_once()

    async def test_failed_terminal_acceptance_save_never_paints_accepted(self) -> None:
        # Given: the base write succeeds but persisting Accepted fails.
        self.fail_phase = "accepted"
        ctx = self.context()

        # When: a moderator accepts the submission.
        with suppress(PyMongoError):
            await self.accept(ctx)

        # Then: the review remains incomplete and the moderator receives a failure.
        self.message.edit.assert_not_awaited()
        self.submitter.send.assert_not_awaited()
        self.assertEqual(self.row["resolution"]["phase"], "accepting")
        self.assertNotIn(str(commands.SUCCESS_EMOJI), ctx.send.await_args.args[0])

    async def test_fingerprint_status_failure_does_not_prevent_accepted_notification(self):
        await self.accept(self.context())
        submission = await PendingBaseSubmission.get("9001")
        self.submitter.send.reset_mock()
        ctx = self.context()
        with patch.object(BaseLayout, "get", new=AsyncMock(side_effect=AutoReconnect("status unavailable"))):
            await self.cog._show_submission_resolution(ctx, submission, newly_resolved=True)
        self.assertIn("was accepted", ctx.send.await_args.args[0])
        self.assertIn("temporarily unavailable", ctx.send.await_args.args[0])
        self.submitter.send.assert_awaited_once()
        self.assertEqual(self.row["resolution"]["phase"], "accepted")

    async def test_failed_rejection_save_never_paints_rejected_or_reads_base_layout(self) -> None:
        # Given: the rejected decision cannot be saved.
        self.fail_phase = "rejected"
        ctx = self.context()

        # When: a moderator rejects the submission.
        with suppress(PyMongoError):
            await self.reject(ctx)

        # Then: no terminal UI or BaseLayout operation runs before persistence.
        self.message.edit.assert_not_awaited()
        self.submitter.send.assert_not_awaited()
        self.base_collection.find_one.assert_not_awaited()
        self.base_collection.insert_one.assert_not_awaited()
        ctx.send.assert_awaited_once()

    async def test_failed_initial_pending_insert_leaves_disabled_public_controls(self) -> None:
        # Given: review posting succeeds but creating its pending row fails.
        channel = MagicMock(send=AsyncMock(return_value=self.message))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        self.bot.owner.send = AsyncMock(return_value=SimpleNamespace(
            id=111, channel=SimpleNamespace(id=222),
            attachments=[SimpleNamespace(id=333, url="https://cdn.example/base.webp")],
        ))
        self.pending_collection.insert_one = AsyncMock(side_effect=OperationFailure("synthetic insert failure"))
        ctx = self.context()
        ctx.author = MagicMock(id=123, display_name="Submitter")

        # When: the actual slash callback submits the review.
        with (
            patch.object(commands, "download_base_screenshot", new=AsyncMock(return_value=True)),
            patch.object(commands.os.path, "exists", return_value=False),
            suppress(PyMongoError),
        ):
            await self.cog.base_submit.callback.func(
                self.cog, ctx, BASE_LINK, SimpleNamespace(url="https://cdn.example/source.webp"), "Good",
            )

        # Then: an orphan post cannot accept moderation input or announce success.
        buttons = channel.send.await_args.kwargs["components"].components
        self.assertTrue(all(button.disabled for button in buttons))
        self.assertTrue(all(str(commands.SUCCESS_EMOJI) not in call.args[0] for call in ctx.send.await_args_list))

    async def test_accept_and_reject_in_separate_cogs_cannot_reverse_claimed_acceptance(self) -> None:
        # Given: acceptance is saved but its base insert acknowledgement was lost.
        self.uncertain_insert = True
        await self.accept(self.context())
        self.base_collection.find_one.reset_mock()
        self.base_collection.insert_one.reset_mock()
        self.bot.ext = {}
        self.cog = commands.BaseCommands(self.bot)

        # When: a fresh cog receives an already-open Reject modal.
        await self.reject(self.context())

        # Then: the acceptance claim survives without any BaseLayout access by Reject.
        self.assertEqual(self.row["resolution"]["phase"], "accepting")
        self.base_collection.find_one.assert_not_awaited()
        self.base_collection.insert_one.assert_not_awaited()
        self.message.edit.assert_not_awaited()
        self.submitter.send.assert_not_awaited()

    async def test_terminal_acceptance_replay_repaints_original_reviewer_without_notifying_again(self) -> None:
        # Given: acceptance completed before a process restart.
        await self.accept(self.context(), builder="42")
        self.bot.ext = {}
        self.cog = commands.BaseCommands(self.bot)
        ctx = self.context()
        ctx.author.id = 999

        # When: another moderator submits Accept again with changed values.
        await self.accept(ctx, quality="Excellent", builder="999")

        # Then: the persisted moderator decision and no-builder sentinel remain unchanged.
        self.assertEqual(self.review_status(), "Accepted by **[Member 789](<https://discordapp.com/users/789>)**")
        self.assertEqual(self.row["resolution"]["quality"], "Great")
        self.assertIsNone(next(iter(self.bases.values()))["builder_id"])
        self.base_collection.insert_one.assert_awaited_once()
        self.submitter.send.assert_awaited_once()

    async def test_two_stale_models_compete_for_one_durable_resolution(self) -> None:
        # Given: both reviewers loaded the same legacy row before either decided.
        first = await PendingBaseSubmission.get("9001")
        second = await PendingBaseSubmission.get("9001")
        original = SubmissionRejection(reviewer_id="789", reason="First decision")
        later = SubmissionRejection(reviewer_id="999", reason="Second decision")

        # When: stale models try to claim different decisions.
        first_won = await first.claim_resolution(original)
        second_won = await second.claim_resolution(later)

        # Then: the conditional update keeps only the first decision and round-trips through BSON.
        self.assertEqual((first_won, second_won), (True, False))
        restored = PendingBaseSubmission.model_validate(BSON(BSON.encode(self.row)).decode())
        self.assertEqual(restored.resolution, original)
        self.assertIsNone(second.resolution)

    async def test_legacy_explicit_null_resolution_can_be_claimed(self) -> None:
        # Given: a legacy row has a literal null decision instead of a missing field.
        self.row["resolution"] = None
        submission = await PendingBaseSubmission.get("9001")
        decision = SubmissionRejection(reviewer_id="789", reason="Reviewed")

        # When: the first moderator claims its resolution.
        won = await submission.claim_resolution(decision)

        # Then: the null/missing-compatible claim persists the complete rejected decision.
        self.assertTrue(won)
        self.assertEqual(self.row["resolution"], decision.model_dump())

    async def test_two_accept_retries_racing_at_insert_create_one_base_and_one_notification(self) -> None:
        # Given: two independent cogs resume the same durable accepting decision.
        self.fail_phase = "accepted"
        await self.accept(self.context())
        self.bases.clear()
        self.fail_phase = None
        self.base_collection.insert_one.reset_mock()
        arrived = 0
        ready = anyio.Event()
        original_load = self.base_collection.find_one.side_effect

        async def simultaneous_load(**kwargs):
            nonlocal arrived
            snapshot = await original_load(**kwargs)
            if snapshot is None:
                arrived += 1
                if arrived == 2:
                    ready.set()
                await ready.wait()
            return snapshot

        self.base_collection.find_one.side_effect = simultaneous_load
        self.bot.ext = {}
        other_cog = commands.BaseCommands(self.bot)

        async def retry_other() -> None:
            await other_cog.accept_submission_modal.callback.func(
                other_cog, self.context(), quality=["Excellent"], builder=["999"],
            )

        # When: both retries observe the base as absent before they insert.
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.accept, self.context())
                tasks.start_soon(retry_other)

        # Then: duplicate-key recovery shares the saved base and only the transition winner notifies.
        self.assertEqual(len(self.bases), 1)
        self.assertEqual(self.row["resolution"]["phase"], "accepted")
        self.assertEqual(self.base_collection.insert_one.await_count, 2)
        self.submitter.send.assert_awaited_once()

import unittest
from contextlib import suppress
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import PyMongoError

import cogs.helper_commands as helper_commands
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import STAFF_ROLE
from elobot.db import Helper


class TestHelperMutationState(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Helper, "_document_settings", DocumentSettings(
            name="helpers", use_state_management=True,
        ))
        settings.start()
        self.addCleanup(settings.stop)
        self.helper = Helper.model_construct(
            id=PydanticObjectId(), user_id="222", points=4, accounts=["#EXISTING"],
        )
        self.helper._save_state()
        self.persisted = {
            "_id": self.helper.id, "user_id": "222", "points": 9,
            "history": [], "accounts": ["#EXISTING", "#CONCURRENT"],
        }
        self.trace = []
        self.collection = MagicMock()
        self.collection.find_one_and_update = AsyncMock(side_effect=self.persist_update)
        collection_patch = patch.object(Helper, "get_pymongo_collection", return_value=self.collection)
        collection_patch.start()
        self.addCleanup(collection_patch.stop)
        model = MagicMock()
        model.find_one = AsyncMock(return_value=self.helper)
        model_patch = patch.object(helper_commands, "Helper", model)
        model_patch.start()
        self.addCleanup(model_patch.stop)
        tags_patch = patch.object(helper_commands, "extract_player_tags", new=AsyncMock(return_value=["#TAG"]))
        tags_patch.start()
        self.addCleanup(tags_patch.stop)
        pairs_patch = patch.object(helper_commands, "generate_account_helper_pairs", new=AsyncMock(return_value={}))
        pairs_patch.start()
        self.addCleanup(pairs_patch.stop)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock(side_effect=self.capture_response)
        self.member = SimpleNamespace(
            id=222, mention="<@222>", roles=[SimpleNamespace(id=STAFF_ROLE)], has_role=lambda _role: True,
        )
        self.cog = helper_commands.HelperCommands(MagicMock())

    async def capture_response(self, content: str, **_kwargs) -> None:
        self.trace.append("success" if str(SUCCESS_EMOJI) in content else "response")

    async def persist_update(self, _query, update, **_kwargs):
        self.trace.append("write")
        self.persisted.update(deepcopy(update.get("$set", {})))
        for field, amount in update.get("$inc", {}).items():
            self.persisted[field] += amount
        for field, values in update.get("$addToSet", {}).items():
            additions = values.get("$each", []) if isinstance(values, dict) else [values]
            self.persisted[field] += [value for value in additions if value not in self.persisted[field]]
        for field, value in update.get("$pull", {}).items():
            self.persisted[field] = [item for item in self.persisted[field] if item != value]
        return deepcopy(self.persisted)

    async def test_link_failure_never_announces_linked_account(self) -> None:
        # Given: Mongo rejects the account update.
        self.collection.find_one_and_update.side_effect = PyMongoError("injected write failure")
        # When: the registered command handles that failure.
        with suppress(PyMongoError):
            await self.cog.helper_account_link.callback.func(self.cog, self.ctx, self.member, "#TAG")
        # Then: no response says that the account was linked.
        responses = [call.args[0] for call in self.ctx.send.await_args_list]
        self.assertFalse(any(str(SUCCESS_EMOJI) in response for response in responses))
        self.assertTrue(any(str(ERROR_EMOJI) in response for response in responses))
        self.assertNotIn("#TAG", self.persisted["accounts"])

    async def test_link_persists_before_success_and_preserves_concurrent_fields(self) -> None:
        # Given: a points award and another linked account postdate the loaded snapshot.
        # When: another account is linked through the registered callback.
        await self.cog.helper_account_link.callback.func(self.cog, self.ctx, self.member, "#TAG")
        # Then: the confirmed link follows its write and preserves those independent changes.
        self.assertLess(self.trace.index("write"), self.trace.index("success"))
        self.assertEqual(self.persisted["points"], 9)
        self.assertEqual(self.persisted["accounts"], ["#EXISTING", "#CONCURRENT", "#TAG"])

    async def test_point_adjustments_preserve_new_accounts_and_use_current_balance(self) -> None:
        # Given: the loaded helper predates a points award and account link.
        for command, expected in (("helper_point_add", 11), ("helper_point_remove", 7)):
            with self.subTest(command=command):
                self.persisted["points"] = 9
                self.persisted["accounts"] = ["#EXISTING", "#CONCURRENT"]
                # When: an operator applies a relative adjustment.
                callback = getattr(self.cog, command)
                await callback.callback.func(self.cog, self.ctx, self.member, 2)
                # Then: only the requested delta changes the current stored balance.
                self.assertEqual(self.persisted["points"], expected)
                self.assertEqual(self.persisted["accounts"], ["#EXISTING", "#CONCURRENT"])

    async def test_unlink_preserves_an_account_added_after_snapshot(self) -> None:
        # Given: another account was linked after this helper was loaded.
        # When: the requested old account is removed.
        await self.cog.helper_account_unlink.callback.func(self.cog, self.ctx, self.member, "#EXISTING")
        # Then: the unrelated account and points award remain stored.
        self.assertEqual(self.persisted["accounts"], ["#CONCURRENT"])
        self.assertEqual(self.persisted["points"], 9)

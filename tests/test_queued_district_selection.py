import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField, LinkInfo, LinkTypes
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

from elo_bot.extensions import match as commands
from elo_bot.db import Match, Participant, Player, Queue, ServerState
from elo_bot.db.player_models import QueueDistrictSelection
from tests._patch_sites import patch_sites
import elo_bot.features.matches.lifecycle as lifecycle_module
import elo_bot.extensions.match as match_module
import elo_bot.features.queue.matching as matching_module
import elo_bot.features.queue.state as state_module
import elo_bot.extensions.match.lifecycle as match_lifecycle_module
import elo_bot.extensions.match.queue as match_queue_module
import elo_bot.extensions.match.results as match_results_module


DISTRICTS = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]


class TestQueuedDistrictSelection(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Queue, Player, Match):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
                state_management_replace_objects=getattr(model.Settings, "state_management_replace_objects", False),
            )))
        for model, field in ((Queue, "tier"), (Player, "user_id"), (Match, "token")):
            self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=SimpleNamespace(name="players")))
        self.enterContext(patch.object(Queue, "_link_fields", {
            "players": LinkInfo(field_name="players", lookup_field_name="players", document_class=Player,
                                link_type=LinkTypes.LIST),
        }))
        self.enterContext(patch_sites(('elo_bot.extensions.match.queue.QUEUE_OPERATION_LOCK', 'elo_bot.extensions.match.lifecycle.QUEUE_OPERATION_LOCK', 'elo_bot.features.queue.policy.QUEUE_OPERATION_LOCK', 'elo_bot.features.queue.matching.QUEUE_OPERATION_LOCK', 'elo_bot.features.matches.lifecycle.QUEUE_OPERATION_LOCK'), anyio.Lock()))
        self.players = {
            user_id: Player.model_construct(id=user_id, user_id=user_id, tag="#TAG", elo=1000, highest_elo=1000)
            for user_id in ("111", "222", "333", "444")
        }
        self.queues = {tier: Queue.model_construct(id=PydanticObjectId(), tier=tier) for tier in match_queue_module.QUEUE_TIERS}
        self.matches = {}
        self.events = []
        self.fail_queue = False
        self.fail_queue_tier = None
        self.fail_match = False
        self.fail_insert = False
        self.pause_queue = False
        self.write_started = anyio.Event()
        self.release_write = anyio.Event()
        self.users = {user_id: MagicMock(id=int(user_id), mention=f"<@{user_id}>", send=AsyncMock())
                      for user_id in self.players}
        self.bot = MagicMock(user=MagicMock(id=commands.BOT_ID), owner=MagicMock(id=999),
                             fetch_user=AsyncMock(side_effect=lambda uid: self.users[str(uid)]))
        self.main_guild = MagicMock(fetch_member=AsyncMock(
            side_effect=lambda uid: MagicMock(id=int(uid), display_name=f"Player {uid}"),
        ))
        self.bot.fetch_guild = AsyncMock(return_value=self.main_guild)
        self.cog = match_module.MatchCommands(self.bot)
        self.enterContext(patch.object(Queue, "find_one", new=AsyncMock(
            side_effect=lambda query, **kwargs: self.snapshot(self.queues[query["tier"]]),
        )))
        self.enterContext(patch.object(Player, "find_one", new=AsyncMock(
            side_effect=lambda query, **kwargs: self.snapshot(self.players[query["user_id"]]),
        )))
        self.enterContext(patch.object(ServerState, "current", new=AsyncMock(
            return_value=SimpleNamespace(queue_disabled=False),
        )))
        self.enterContext(patch('elo_bot.features.queue.matching.get_last_opponent', new=AsyncMock(return_value=None)))
        self.enterContext(patch_sites(('elo_bot.features.matches.lifecycle.get_base_quality', 'elo_bot.features.matches.attack_bases.get_base_quality'), return_value=("Good", "Good")))

        async def persist_queue(document, updates, **kwargs):
            if self.fail_queue or document.tier == self.fail_queue_tier:
                raise OperationFailure("queue write failed")
            if self.pause_queue:
                self.pause_queue = False
                self.write_started.set()
                await self.release_write.wait()
            for field, value in updates.items():
                if field == "players":
                    value = [self.snapshot(self.players[reference.id]) for reference in value]
                if field == "district_selections":
                    value = {uid: QueueDistrictSelection.model_validate(selection) for uid, selection in value.items()}
                setattr(self.queues[document.tier], field, deepcopy(value))
            self.events.append("queue-write")
            return document

        async def insert_match(document, **kwargs):
            if self.fail_insert:
                raise OperationFailure("match insert failed")
            document.id = PydanticObjectId()
            self.matches[document.token] = self.snapshot(document)
            self.events.append("match-insert")
            return document

        async def persist_match(document, updates, **kwargs):
            if self.fail_match:
                raise OperationFailure("match write failed")
            for field, value in updates.items():
                if "." in field:
                    parts = field.split(".")
                    target = self.matches[document.token]
                    for part in parts[:-1]:
                        target = target[int(part)] if part.isdigit() else getattr(target, part)
                    if parts[-1].isdigit():
                        target[int(parts[-1])] = deepcopy(value)
                    else:
                        setattr(target, parts[-1], deepcopy(value))
                    continue
                if field == "participants":
                    value = [Participant.model_validate(participant) for participant in value]
                setattr(self.matches[document.token], field, deepcopy(value))
            self.events.append("match-write")
            return document

        async def find_match(query, **kwargs):
            if "token" in query:
                match = self.matches.get(query["token"])
                return self.snapshot(match) if match else None
            identity = query["participants"]["$elemMatch"]
            return next((self.snapshot(match) for match in self.matches.values()
                         if any(p.user_id == identity["user_id"] and p.queue_attempt_id == identity["queue_attempt_id"]
                                for p in match.participants)), None)

        def find_matches(query):
            return MagicMock(to_list=AsyncMock(return_value=[self.snapshot(match) for match in self.matches.values()
                             if any(p.user_id == query["participants.user_id"] for p in match.participants)]))

        async def confirm_match(bot, token):
            self.assertTrue(all(participant.confirmed for participant in self.matches[token].participants))
            self.matches[token].thread = "9001"
            self.events.append("thread-created")
            return "9001"

        self.enterContext(patch.object(Queue, "set", new=persist_queue))
        self.enterContext(patch.object(Match, "insert", new=insert_match))
        self.enterContext(patch.object(Match, "set", new=persist_match))
        self.enterContext(patch.object(Match, "find_one", new=AsyncMock(side_effect=find_match)))
        self.enterContext(patch.object(Match, "find", side_effect=find_matches))
        self.confirm_thread = self.enterContext(patch_sites(('elo_bot.extensions.match.lifecycle.confirm_match', 'elo_bot.features.matches.lifecycle.confirm_match'), new=AsyncMock(
            side_effect=confirm_match,
        )))

    @staticmethod
    def snapshot(document):
        copy = document.model_copy(deep=True)
        copy._save_state()
        return copy

    def context(self, user_id="111", custom_id=None, values=None):
        ctx = MagicMock(spec=ipy.ComponentContext if custom_id else ipy.SlashContext)
        ctx.author = MagicMock(spec=ipy.Member, id=int(user_id))
        ctx.author.has_role.return_value = False
        ctx.channel = MagicMock(id=1234)
        ctx.guild = None  # match confirmation and reveal buttons are pressed in DMs
        ctx.bot = self.bot
        ctx.message = MagicMock(components=[], embeds=[ipy.Embed(title="**Queue**")], edit=AsyncMock(), content="")
        ctx.custom_id = custom_id
        ctx.kwargs = {}
        ctx.args = []
        ctx.values = values or []
        ctx.defer = AsyncMock()
        ctx.delete = AsyncMock()
        ctx.edit = AsyncMock()
        ctx.edit_origin = AsyncMock()

        async def send(*args, **kwargs):
            self.events.append("send:" + (str(args[0]) if args else ""))
            return MagicMock(id=700)

        ctx.send = AsyncMock(side_effect=send)
        return ctx

    async def invoke(self, command, ctx, **kwargs):
        callback = getattr(self.cog, command).callback
        await getattr(callback, "func", callback)(self.cog, ctx, **kwargs)

    def seed(self, user_id="111", *, tier=3, districts=None, confirmed=False):
        queue = self.queues[tier]
        queue.players.append(self.snapshot(self.players[user_id]))
        queue.entered_at[user_id] = 3_000_000_000.0
        selection = QueueDistrictSelection(attempt_id=f"ATTEMPT{user_id}", districts=districts or [None] * 3,
                                           confirmed=confirmed)
        queue.district_selections[user_id] = selection
        return selection

    def action(self, selection, action="confirm", *, user_id="111", index=None, values=None):
        identity = f"queued_{action}|{user_id}|{selection.attempt_id}"
        return self.context(user_id, identity + (f"|{index}" if index is not None else ""), values)

    def http_context(self, selection, action="cancel", *, message_id=701):
        self.bot.cache.get_member.return_value = self.context().author
        self.bot.app.id = 999
        payloads = {
            message_id: {
                "id": str(message_id), "channel_id": "1234", "flags": 64,
                "content": "You are now added to the match-making queue.",
                "components": [row.to_dict() for row in match_queue_module.queue_district_components(
                    "111", selection, match_queue_module.get_district_options(True, True),
                )],
            },
        }

        async def edit_interaction(*args, **kwargs):
            payload = kwargs.get("payload", args[0] if args else None)
            target = kwargs.get("message_id", "@original")
            target = max(payloads) + 1 if target == "@original" else int(target)
            payloads.setdefault(target, {"id": str(target), "channel_id": "1234", "flags": 64})
            payloads[target].update(deepcopy(payload))
            self.events.append(f"display:{target}")
            return deepcopy(payloads[target])

        async def initial_response(payload, *args, **kwargs):
            return None if payload["type"] == 5 else await edit_interaction(payload["data"])

        self.bot.http.post_initial_response = AsyncMock(side_effect=initial_response)
        self.bot.http.edit_interaction_message = AsyncMock(side_effect=edit_interaction)
        self.bot.http.post_followup = AsyncMock(side_effect=edit_interaction)
        self.bot.http.edit_message = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404)))
        cached = {}

        def place_message(data):
            message = ipy.Message.from_dict(deepcopy(data), self.bot)
            cached[int(message.id)] = message
            return message

        def get_message(channel, mid):
            return cached[int(mid)] if int(mid) in cached else place_message(payloads[int(mid)])

        self.bot.cache.get_message.side_effect = get_message
        self.bot.cache.place_message_data.side_effect = place_message
        ctx = ipy.ComponentContext(self.bot)
        ctx.id, ctx.token, ctx.author_id = 800, "offline-queue-click", 111
        ctx.channel_id, ctx.guild_id, ctx.message_id = 1234, 50, message_id
        ctx.custom_id, ctx.values = f"queued_{action}|111|{selection.attempt_id}", []
        return ctx, payloads

    async def pair(self, tier=3):
        async with match_queue_module.QUEUE_OPERATION_LOCK:
            await matching_module.match_players(self.bot, tier, 250)
        return next(reversed(self.matches.values()))

    async def test_unmatched_response_has_three_single_selects_and_familiar_buttons(self):
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        payload = ctx.send.await_args.kwargs
        rows = payload["components"]
        self.assertEqual(len(rows), 4)
        self.assertIn("Optionally", payload["embed"].description)
        self.assertEqual([row.components[0].max_values for row in rows[:3]], [1, 1, 1])
        self.assertEqual([[option.value for option in row.components[0].options] for row in rows[:3]],
                         match_queue_module.get_district_options(True, True))
        self.assertEqual([button.label for button in rows[3].components], ["Cancel", "Confirm"])
        self.assertEqual([button.style for button in rows[3].components],
                         [ipy.ButtonStyle.DANGER, ipy.ButtonStyle.SUCCESS])
        self.assertTrue(self.queues[3].district_selections["111"].attempt_id)
        self.assertLess(self.events.index("queue-write"), next(i for i, event in enumerate(self.events)
                                                             if event.startswith("send:")))

    async def test_existing_queue_button_uses_optional_picker(self):
        ctx = self.context(custom_id="match_queue")
        await self.invoke("match_queue_button", ctx)
        self.assertEqual(len(ctx.send.await_args.kwargs["components"]), 4)

    async def test_registered_queue_callback_serializes_ephemeral_picker_through_mocked_http(self):
        self.bot.cache.get_member.return_value = self.context().author
        self.bot.app.id = 999
        self.bot.http.post_initial_response = AsyncMock()
        self.bot.http.edit_interaction_message = AsyncMock(return_value={"id": "701", "channel_id": "1234"})
        ctx = ipy.SlashContext(self.bot)
        ctx.id = 700
        ctx.token = "local-probe"
        ctx.author_id = 111
        await self.invoke("match_queue", ctx)
        defer_payload = self.bot.http.post_initial_response.await_args.args[0]
        self.assertEqual(defer_payload["type"], 5)
        self.assertEqual(defer_payload["data"]["flags"], 64)
        payload = self.bot.http.edit_interaction_message.await_args.args[0]
        self.assertEqual(payload["flags"], 64)
        self.assertEqual(payload["embeds"][0]["title"], "**Match-making Queue**")
        rows = payload["components"]
        self.assertEqual(len(rows), 4)
        self.assertEqual([row["components"][0]["max_values"] for row in rows[:3]], [1, 1, 1])
        selection = self.queues[3].district_selections["111"]
        self.assertEqual(rows[3]["components"][1]["custom_id"], f"queued_confirm|111|{selection.attempt_id}")

        click, messages = self.http_context(selection)
        self.events.clear()
        await self.invoke("queued_district_action", click)
        self.bot.http.edit_message.assert_not_awaited()
        self.assertEqual(messages[701]["components"], [])
        self.assertFalse(self.queues[3].players)
        self.assertFalse(self.queues[3].district_selections)
        self.assertIn("removed from the match-making queue", messages[702]["content"])
        self.assertLess(self.events.index("queue-write"), self.events.index("display:701"))
        dequeue = self.context()
        await self.invoke("match_dequeue", dequeue)
        self.assertIn("not currently in", dequeue.send.await_args.args[0])

    async def test_ephemeral_confirm_saves_choices_and_keeps_the_picker_editable(self):
        selection = self.seed(districts=DISTRICTS)
        ctx, messages = self.http_context(selection, "confirm")
        await self.invoke("queued_district_action", ctx)
        self.bot.http.edit_message.assert_not_awaited()
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertFalse(any(component.get("disabled", False) for row in messages[701]["components"]
                             for component in row["components"]))
        self.assertIn("Districts saved", messages[702]["content"])
        self.assertIn("change them until an opponent is found", messages[702]["content"])

    async def test_dequeue_removes_registered_discord_user_from_every_queue_tier(self):
        self.players["111"].id = "registration-id"
        for tier in match_queue_module.QUEUE_TIERS:
            self.seed(tier=tier)
        ctx = self.context()
        await self.invoke("match_dequeue", ctx)
        self.assertIn("removed from the match-making queue", ctx.send.await_args.args[0])
        for queue in self.queues.values():
            self.assertFalse(queue.players)
            self.assertFalse(queue.entered_at)
            self.assertFalse(queue.district_selections)

    async def test_matched_ephemeral_cancel_retires_the_exact_picker_and_confirmation_prompt(self):
        for choice in ("confirm", "cancel"):
            with self.subTest(choice=choice):
                selection = self.seed()
                self.seed("222")
                match = await self.pair()
                ctx, messages = self.http_context(selection)

                async def reply(**kwargs):
                    response = ipy.ComponentContext(self.bot)
                    response.id, response.token, response.author_id = 801, "offline-cancel-reply", 111
                    response.channel_id, response.guild_id, response.message_id = 1234, 50, int(kwargs["messages"])
                    response.custom_id = choice
                    return SimpleNamespace(ctx=response)

                async def delete(captured, **kwargs):
                    self.matches.pop(captured.token)
                    return True

                self.bot.wait_for_component = AsyncMock(side_effect=reply)
                with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(side_effect=delete)), \
                        patch.object(Player, "set", new=AsyncMock()):
                    await self.invoke("queued_district_action", ctx)
                self.bot.http.edit_message.assert_not_awaited()
                self.assertTrue(all(component["disabled"] for row in messages[702]["components"]
                                    for component in row["components"]))
                if choice == "confirm":
                    self.assertNotIn(match.token, self.matches)
                    self.assertTrue(all(component["disabled"] for row in messages[701]["components"]
                                        for component in row["components"]))
                    self.assertIn("successfully canceled", messages[703]["content"])
                else:
                    self.assertIn(match.token, self.matches)
                    self.assertFalse(messages[701]["components"][3]["components"][0]["disabled"])
                    self.assertIn("action has been canceled", messages[703]["content"])

    async def test_presets_preselect_only_eligible_primary_and_remain_editable(self):
        player = self.players["111"]
        player.district_presets = ["Wizard Valley", "Golem Quarry", "Capital Peak"]
        player.secondary_district_presets = list(DISTRICTS)
        player.elo = 1200
        player.capital_peak_enabled = False
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        payload = ctx.send.await_args.kwargs
        self.assertIn("preset is loaded", payload["embed"].description)
        rows = payload["components"]
        self.assertEqual([[option.value for option in row.components[0].options if option.default]
                          for row in rows[:3]], [[], ["Golem Quarry"], []])
        selection = self.queues[3].district_selections["111"]
        select_ctx = self.action(selection, "district", index=1, values=["Balloon Lagoon"])
        await self.invoke("queued_district_action", select_ctx)
        self.assertEqual(self.queues[3].district_selections["111"].districts[1], "Balloon Lagoon")
        self.assertEqual(player.district_presets, ["Wizard Valley", "Golem Quarry", "Capital Peak"])
        self.assertEqual(player.secondary_district_presets, DISTRICTS)
        self.assertFalse(self.queues[3].district_selections["111"].confirmed)
        select_ctx.message.edit.assert_not_awaited()

    async def test_selection_then_confirm_keeps_this_attempt_editable_for_reconfirmation(self):
        selection = self.seed()
        for index, district in enumerate(DISTRICTS):
            await self.invoke("queued_district_action", self.action(selection, "district", index=index, values=[district]))
        confirm = self.action(selection)
        await self.invoke("queued_district_action", confirm)
        saved = self.queues[3].district_selections["111"]
        self.assertTrue(saved.confirmed)
        self.assertEqual(saved.districts, DISTRICTS)
        rows = confirm.message.edit.await_args.kwargs["components"]
        self.assertFalse(any(component.disabled for row in rows for component in row.components))
        self.assertIn("change them until an opponent is found", confirm.send.await_args.args[0])
        await self.invoke("queued_district_action", self.action(selection))
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        edit = self.action(selection, "district", index=0, values=["Skeleton Park"])
        await self.invoke("queued_district_action", edit)
        updated = ["Skeleton Park", *DISTRICTS[1:]]
        self.assertEqual(self.queues[3].district_selections["111"].districts, updated)
        self.assertFalse(self.queues[3].district_selections["111"].confirmed)
        self.assertIn("Press Confirm", edit.send.await_args.args[0])
        await self.invoke("queued_district_action", self.action(selection))
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertEqual(self.queues[3].district_selections["111"].districts, updated)
        self.assertEqual(len(self.queues[3].players), 1)

    async def test_reselected_draft_is_not_auto_confirmed_if_pairing_precedes_reconfirmation(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        await self.invoke("queued_district_action", self.action(selection, "district", index=0,
                                                               values=["Skeleton Park"]))
        match = await self.pair()
        self.assertEqual(match.participants[0].districts, ["Skeleton Park", *DISTRICTS[1:]])
        self.assertFalse(match.participants[0].confirmed)
        self.assertFalse(match.participants[0].queue_districts_locked)
        ctx = self.action(selection, "district", index=1, values=["Golem Quarry"])
        await self.invoke("queued_district_action", ctx)
        self.assertEqual(self.matches[match.token].participants[0].districts[1], "Golem Quarry")

    async def test_reconfirmed_choices_lock_only_when_an_opponent_is_found(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        await self.invoke("queued_district_action", self.action(selection, "district", index=0,
                                                               values=["Skeleton Park"]))
        await self.invoke("queued_district_action", self.action(selection))
        match = await self.pair()
        expected = ["Skeleton Park", *DISTRICTS[1:]]
        self.assertEqual(match.participants[0].districts, expected)
        self.assertTrue(match.participants[0].confirmed)
        self.assertTrue(match.participants[0].queue_districts_locked)
        ctx = self.action(selection, "district", index=0, values=[DISTRICTS[0]])
        await self.invoke("queued_district_action", ctx)
        self.assertIn("already confirmed", ctx.send.await_args.args[0])
        self.assertEqual(self.matches[match.token].participants[0].districts, expected)

    async def test_failed_reselection_preserves_the_saved_confirmation_and_choices(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        self.fail_queue = True
        ctx = self.action(selection, "district", index=0, values=["Skeleton Park"])
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertEqual(self.queues[3].district_selections["111"].districts, DISTRICTS)
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertIn("could not be saved", ctx.send.await_args.args[0])
        ctx.message.edit.assert_not_awaited()

    async def test_reselection_serializes_before_pairing_without_confirming_the_changed_draft(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        self.pause_queue = True

        async def reselect():
            await self.invoke("queued_district_action", self.action(selection, "district", index=0,
                                                                   values=["Skeleton Park"]))

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(reselect)
                await self.write_started.wait()
                tasks.start_soon(self.pair)
                await anyio.wait_all_tasks_blocked()
                self.release_write.set()
        participant = next(iter(self.matches.values())).participants[0]
        self.assertEqual(participant.districts, ["Skeleton Park", *DISTRICTS[1:]])
        self.assertFalse(participant.confirmed)
        self.assertFalse(participant.queue_districts_locked)

    async def test_invalid_values_and_incomplete_confirm_never_write_success(self):
        selection = self.seed()
        for values in ([], ["Barbarian Camp", "Skeleton Park"], ["Barbarian Camp"] * 2, ["Dragon Cliffs"]):
            ctx = self.action(selection, "district", index=0, values=values)
            await self.invoke("queued_district_action", ctx)
            self.assertIn("not available", ctx.send.await_args.args[0])
        for index in range(3):
            self.queues[3].district_selections["111"].districts = list(DISTRICTS)
            self.queues[3].district_selections["111"].districts[index] = None
            ctx = self.action(selection)
            await self.invoke("queued_district_action", ctx)
            self.assertIn("all three", ctx.send.await_args.args[0])
            self.assertFalse(self.queues[3].district_selections["111"].confirmed)
        self.assertNotIn("queue-write", self.events)

    async def test_wrong_owner_cannot_modify_or_retire_picker(self):
        selection = self.seed()
        for action in ("confirm", "cancel", "district"):
            ctx = self.action(selection, action, index=0 if action == "district" else None, values=["Skeleton Park"])
            ctx.author.id = 222
            await self.invoke("queued_district_action", ctx)
            self.assertIn("other user's", ctx.send.await_args.args[0])
            ctx.message.edit.assert_not_awaited()
        self.assertEqual(self.queues[3].district_selections["111"].districts, [None] * 3)
        self.assertNotIn("queue-write", self.events)

    async def test_locked_cancel_removes_exact_entry_and_stale_controls_cannot_touch_requeue(self):
        old = self.seed(districts=DISTRICTS, confirmed=True)
        await self.invoke("queued_district_action", self.action(old, "cancel"))
        self.assertFalse(self.queues[3].players)
        self.assertFalse(self.queues[3].district_selections)
        await self.invoke("match_queue", self.context())
        new = self.queues[3].district_selections["111"].model_copy(deep=True)
        self.assertNotEqual(new.attempt_id, old.attempt_id)
        for action in ("confirm", "cancel", "district"):
            ctx = self.action(old, action, index=0 if action == "district" else None, values=["Skeleton Park"])
            await self.invoke("queued_district_action", ctx)
            self.assertIn("no longer active", ctx.send.await_args.args[0])
        self.assertEqual(self.queues[3].district_selections["111"], new)
        self.assertEqual(len(self.queues[3].players), 1)

    async def test_slash_dequeue_invalidates_the_optional_controls(self):
        selection = self.seed()
        await self.invoke("match_dequeue", self.context())
        ctx = self.action(selection)
        await self.invoke("queued_district_action", ctx)
        self.assertIn("no longer active", ctx.send.await_args.args[0])
        self.assertEqual(self.queues[3].district_selections, {})

    async def test_choices_survive_reload_promotion_and_interrupted_transfer(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        async with match_queue_module.QUEUE_OPERATION_LOCK:
            await state_module.move_queue_tier(3, 2, 3_000_000_001.0)
            await state_module.move_queue_tier(2, 1, 3_000_000_002.0)
            await state_module.promote_tier1_aged_players(self.snapshot(self.queues[0]), self.snapshot(self.queues[1]),
                                                     3_000_000_002.0 + 86401)
        restored = QueueDistrictSelection.model_validate(self.queues[0].district_selections["111"].model_dump())
        self.assertEqual(restored, selection)
        self.assertTrue(all(not self.queues[tier].district_selections for tier in (1, 2, 3)))
        ctx = self.action(selection, "cancel")
        await self.invoke("queued_district_action", ctx)
        self.assertIn("removed", ctx.send.await_args.args[0])

    async def test_both_locked_choices_transfer_before_dequeue_and_create_thread(self):
        selections = [self.seed(uid, districts=DISTRICTS, confirmed=True) for uid in ("111", "222")]
        match = await self.pair()
        self.assertEqual([p.queue_attempt_id for p in match.participants], [s.attempt_id for s in selections])
        self.assertTrue(all(p.confirmed and p.queue_districts_locked for p in match.participants))
        self.assertEqual(match.thread, "9001")
        self.assertFalse(self.queues[3].players)
        self.assertEqual(self.queues[3].district_selections, {})
        self.assertLess(self.events.index("match-insert"), self.events.index("queue-write"))
        self.confirm_thread.assert_awaited_once()

    async def test_one_confirmed_and_skip_selection_players_still_match(self):
        self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        match = await self.pair()
        self.assertEqual([p.confirmed for p in match.participants], [True, False])
        self.assertIsNone(match.thread)
        self.assertFalse(self.queues[3].players)
        rows = self.users["222"].send.await_args.kwargs["components"]
        self.assertTrue(all(row.components[0].max_values == 1 for row in rows[:3]))
        self.assertEqual([button.label for button in rows[3].components], ["Cancel", "Confirm"])
        self.confirm_thread.assert_not_awaited()

    async def test_unavailable_locked_districts_use_random_same_group_without_secondary_presets(self):
        self.players["111"].secondary_district_presets = list(DISTRICTS)
        self.players["222"].elo = 1200
        self.players["222"].capital_peak_enabled = False
        self.seed(districts=["Wizard Valley", "Golem Quarry", "Capital Peak"], confirmed=True)
        self.seed("222")
        with patch.object(lifecycle_module.random, "choice", side_effect=["Skeleton Park", "Goblin Mines"]) as random_choice:
            match = await self.pair()
        self.assertEqual(match.participants[0].districts, ["Skeleton Park", "Golem Quarry", "Goblin Mines"])
        self.assertEqual([call.args[0] for call in random_choice.call_args_list],
                         [["Barbarian Camp", "Skeleton Park"], ["Dragon Cliffs", "Goblin Mines"]])
        self.assertTrue(match.participants[0].confirmed)

    async def test_eligible_locked_districts_are_preserved(self):
        self.seed(districts=["Wizard Valley", "Golem Quarry", "Capital Peak"], confirmed=True)
        self.seed("222")
        with patch.object(lifecycle_module.random, "choice") as random_choice:
            match = await self.pair()
        self.assertEqual(match.participants[0].districts, ["Wizard Valley", "Golem Quarry", "Capital Peak"])
        random_choice.assert_not_called()

    async def test_pairing_before_queue_confirm_preserves_complete_preset_auto_confirmation(self):
        self.players["111"].district_presets = list(DISTRICTS)
        self.seed(districts=["Skeleton Park", "Golem Quarry", "Goblin Mines"])
        self.seed("222")
        match = await self.pair()
        self.assertTrue(match.participants[0].confirmed)
        self.assertEqual(match.participants[0].districts, DISTRICTS)
        ctx = self.action(QueueDistrictSelection(attempt_id="ATTEMPT111", districts=DISTRICTS))
        await self.invoke("queued_district_action", ctx)
        self.assertIn("already confirmed", ctx.send.await_args.args[0])
        ctx.message.edit.assert_awaited_once_with(components=[], context=ctx)

    async def test_draft_choices_transfer_without_unrequested_confirmation(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        match = await self.pair()
        self.assertFalse(match.participants[0].confirmed)
        self.assertEqual(match.participants[0].districts, DISTRICTS)
        ctx = self.action(selection)
        await self.invoke("queued_district_action", ctx)
        self.assertTrue(self.matches[match.token].participants[0].confirmed)
        self.assertEqual(self.matches[match.token].thread, "9001")
        self.assertIn("9001", ctx.send.await_args.args[0])

    async def test_transitioned_selector_uses_current_match_availability_and_old_picker(self):
        selection = self.seed()
        self.players["222"].elo = 1200
        self.seed("222")
        match = await self.pair()
        invalid = self.action(selection, "district", index=0, values=["Wizard Valley"])
        await self.invoke("queued_district_action", invalid)
        self.assertIn("not available for this match", invalid.send.await_args.args[0])
        valid = self.action(selection, "district", index=0, values=["Skeleton Park"])
        await self.invoke("queued_district_action", valid)
        self.assertEqual(self.matches[match.token].participants[0].districts[0], "Skeleton Park")
        old = self.context(custom_id=f"district_select|{match.token}|1", values=["Golem Quarry"])
        await self.invoke("district_select", old)
        self.assertEqual(self.matches[match.token].participants[0].districts[1], "Golem Quarry")

    async def test_instant_match_has_no_optional_picker(self):
        self.seed("222")
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        self.assertIn("match has been found", ctx.send.await_args.args[0])
        self.assertNotIn("components", ctx.send.await_args.kwargs)
        self.assertNotIn("embed", ctx.send.await_args.kwargs)
        self.assertIsNone(next(iter(self.matches.values())).participants[0].queue_attempt_id)
        self.assertFalse(self.queues[3].players)

    async def test_queue_response_rechecks_pairing_before_publication(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222")
        await self.pair()
        ctx = self.context()
        await self.cog._send_queue_district_picker(ctx, selection, "queued")
        self.assertIn("match has been found", ctx.send.await_args.args[0])
        self.assertNotIn("components", ctx.send.await_args.kwargs)

    async def test_queue_response_rechecks_dequeue_before_publication(self):
        selection = self.seed()
        await self.invoke("match_dequeue", self.context())
        ctx = self.context()
        await self.cog._send_queue_district_picker(ctx, selection, "queued")
        self.assertIn("no longer active", ctx.send.await_args.args[0])
        self.assertNotIn("components", ctx.send.await_args.kwargs)

    async def test_beta_and_automatic_queue_preserve_their_entry_paths(self):
        self.bot.user.id = commands.BOT_ID + 1
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        self.assertEqual(len(ctx.send.await_args.kwargs["components"]), 4)
        self.assertIn("111", self.queues[1].district_selections)
        self.players["222"].elo = 2000
        outcome = await match_queue_module.queue_player(self.bot, self.context("222").author)
        self.assertEqual(outcome.status, match_queue_module.QueueOutcomeStatus.BETA_QUEUED)
        self.assertIsNone(outcome.selection)
        self.assertNotIn("222", self.queues[1].district_selections)

    async def test_failed_queue_writes_never_announce_selection_confirmation_or_dequeue(self):
        selection = self.seed(districts=DISTRICTS)
        self.fail_queue = True
        for action in ("district", "confirm", "cancel"):
            ctx = self.action(selection, action, index=0 if action == "district" else None, values=["Skeleton Park"])
            with self.assertLogs(level="ERROR"):
                await self.invoke("queued_district_action", ctx)
            self.assertIn("could not be saved", ctx.send.await_args.args[0])
            ctx.message.edit.assert_not_awaited()
            self.assertFalse(self.queues[3].district_selections["111"].confirmed)
            self.assertEqual(self.queues[3].district_selections["111"].districts, DISTRICTS)
            self.assertTrue(self.queues[3].players)

    async def test_failed_confirmation_display_retains_committed_confirmation(self):
        selection = self.seed(districts=DISTRICTS)
        ctx = self.action(selection)
        ctx.message.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertTrue(self.queues[3].players)
        self.assertIn("districts are saved", ctx.send.await_args.args[0])
        self.assertIn("display could not be updated", ctx.send.await_args.args[0])

    async def test_failed_queue_publication_retains_durable_entry(self):
        ctx = self.context()
        ctx.send.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.invoke("match_queue", ctx)
        self.assertTrue(self.queues[3].players)
        self.assertIn("111", self.queues[3].district_selections)

    async def test_failed_match_insert_preserves_confirmed_choices_and_queue(self):
        self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        self.fail_insert = True
        with self.assertRaises(OperationFailure):
            await self.pair()
        self.assertFalse(self.matches)
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertEqual(len(self.queues[3].players), 2)
        self.users["111"].send.assert_not_awaited()

    async def test_match_publication_failure_retains_transferred_choices(self):
        self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        self.users["111"].send.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.pair()
        match = next(iter(self.matches.values()))
        self.assertTrue(match.participants[0].confirmed)
        self.assertEqual(match.participants[0].districts, DISTRICTS)
        self.assertFalse(self.queues[3].players)

    async def test_confirm_serializes_before_pairing_and_cannot_lose_choices(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        self.pause_queue = True

        async def confirm():
            await self.invoke("queued_district_action", self.action(selection))

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(confirm)
                await self.write_started.wait()
                tasks.start_soon(self.pair)
                await anyio.wait_all_tasks_blocked()
                self.assertFalse(self.matches)
                self.release_write.set()
        match = next(iter(self.matches.values()))
        self.assertTrue(all(p.confirmed for p in match.participants))
        self.assertEqual(match.thread, "9001")

    async def test_cancel_serializes_before_pairing_and_preserves_other_player(self):
        selection = self.seed()
        self.seed("222")
        self.pause_queue = True

        async def cancel():
            await self.invoke("queued_district_action", self.action(selection, "cancel"))

        async def scheduled_matching():
            callback = self.cog.create_matches.callback
            await getattr(callback, "func", callback)(self.cog)

        self.enterContext(patch.object(ServerState, "update_fields", new=AsyncMock()))

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(cancel)
                await self.write_started.wait()
                tasks.start_soon(scheduled_matching)
                await anyio.wait_all_tasks_blocked()
                self.release_write.set()
        self.assertFalse(self.matches)
        self.assertEqual([player.user_id for player in self.queues[2].players], ["222"])
        self.assertNotIn("111", self.queues[2].district_selections)

    async def test_started_or_newer_matches_cannot_be_affected_by_old_queue_controls(self):
        selection = self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        match = await self.pair()
        before = match.model_dump()
        for action in ("confirm", "cancel", "district"):
            ctx = self.action(selection, action, index=0 if action == "district" else None, values=["Skeleton Park"])
            await self.invoke("queued_district_action", ctx)
            self.assertIn("already started", ctx.send.await_args.args[0])
        self.assertEqual(match.model_dump(), before)
        match.participants[0].queue_attempt_id = "NEWATTEMPT"
        ctx = self.action(selection, "cancel")
        await self.invoke("queued_district_action", ctx)
        self.assertIn("no longer active", ctx.send.await_args.args[0])
        self.assertIn(match.token, self.matches)

    async def test_transitioned_cancel_uses_existing_prompt_cooldown_and_bookkeeping(self):
        selection = self.seed()
        self.seed("222")
        match = await self.pair()
        ctx = self.action(selection, "cancel")
        response = self.context(custom_id="confirm")
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=response))
        self.players["111"].last_cancel = 0

        async def delete(captured, **kwargs):
            self.assertTrue(kwargs["pre_thread"])
            self.matches.pop(captured.token)
            self.events.append("match-delete")
            return True

        async def save_player(document, updates, **kwargs):
            for field, value in updates.items():
                setattr(self.players[document.user_id], field, value)
            self.events.append("player-write")
            return document

        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(side_effect=delete)), \
                patch.object(Player, "set", new=save_player):
            await self.invoke("queued_district_action", ctx)
        self.assertIn("Are you sure", ctx.send.await_args.args[0])
        self.assertIn("once in a week", ctx.send.await_args.args[0])
        self.assertNotIn(match.token, self.matches)
        self.assertTrue(self.players["222"].priority)
        self.assertTrue(self.players["111"].last_cancel)
        self.assertIn("successfully canceled", response.send.await_args.args[0])
        self.users["222"].send.assert_awaited()
        self.assertLess(self.events.index("match-delete"), self.events.index("player-write"))

    async def test_complete_presets_are_loaded_editable_and_overridden_only_on_queue_confirm(self):
        self.players["111"].district_presets = list(DISTRICTS)
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        rows = ctx.send.await_args.kwargs["components"]
        self.assertEqual([[option.value for option in row.components[0].options if option.default]
                          for row in rows[:3]], [[district] for district in DISTRICTS])
        self.assertFalse(any(row.components[0].disabled for row in rows))
        selection = self.queues[3].district_selections["111"]
        await self.invoke("queued_district_action", self.action(selection, "district", index=0,
                                                                 values=["Skeleton Park"]))
        await self.invoke("queued_district_action", self.action(selection))
        self.seed("222")
        match = await self.pair()
        self.assertEqual(match.participants[0].districts, ["Skeleton Park", "Balloon Lagoon", "Dragon Cliffs"])
        self.assertTrue(match.participants[0].confirmed)
        self.assertEqual(self.players["111"].district_presets, DISTRICTS)

    async def test_locked_choices_do_not_randomize_unused_permanent_presets(self):
        self.players["111"].district_presets = ["Wizard Valley", "Balloon Lagoon", "Capital Peak"]
        self.players["222"].elo = 1200
        self.players["222"].capital_peak_enabled = False
        self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        with patch.object(lifecycle_module.random, "choice") as random_choice:
            match = await self.pair()
        self.assertEqual(match.participants[0].districts, DISTRICTS)
        random_choice.assert_not_called()

    async def test_immediate_pairing_transfers_queued_opponents_locked_choices(self):
        selection = self.seed("222", districts=DISTRICTS, confirmed=True)
        self.players["111"].district_presets = list(DISTRICTS)
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        match = next(iter(self.matches.values()))
        self.assertEqual(match.participants[1].queue_attempt_id, selection.attempt_id)
        self.assertTrue(match.participants[1].queue_districts_locked)
        self.assertEqual(match.thread, "9001")
        self.assertNotIn("components", ctx.send.await_args.kwargs)

    async def test_four_player_trigger_keeps_picker_for_unmatched_requester(self):
        for uid in ("222", "333", "444"):
            self.players[uid].elo = 1210
            self.seed(uid)
        ctx = self.context()
        await self.invoke("match_queue", ctx)
        self.assertEqual(len(self.matches), 1)
        self.assertIn("added to the match-making queue", ctx.send.await_args.args[0])
        self.assertEqual(len(ctx.send.await_args.kwargs["components"]), 4)
        self.assertIn("111", self.queues[2].district_selections)
        self.assertFalse(any(p.user_id == "111" for match in self.matches.values() for p in match.participants))

    async def test_failed_join_does_not_publish_picker_or_success(self):
        self.fail_queue = True
        ctx = self.context()
        with self.assertRaises(OperationFailure):
            await self.invoke("match_queue", ctx)
        ctx.send.assert_not_awaited()
        self.assertFalse(self.queues[3].players)
        self.assertFalse(self.queues[3].district_selections)

    async def test_cancel_display_failure_does_not_restore_committed_dequeue(self):
        selection = self.seed()
        ctx, messages = self.http_context(selection)
        edit_interaction = self.bot.http.edit_interaction_message.side_effect

        async def fail_picker_edit(*args, **kwargs):
            if kwargs.get("message_id") == 701:
                raise ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
            return await edit_interaction(*args, **kwargs)

        self.bot.http.edit_interaction_message.side_effect = fail_picker_edit
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertFalse(self.queues[3].players)
        self.assertFalse(self.queues[3].district_selections)
        self.bot.http.edit_message.assert_not_awaited()
        self.assertTrue(messages[701]["components"])
        self.assertIn("removed from the match-making queue", messages[702]["content"])
        self.assertIn("display could not be updated", messages[702]["content"])

    async def test_failed_match_confirmation_never_claims_confirmation(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222")
        match = await self.pair()
        self.fail_match = True
        ctx = self.action(selection)
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertFalse(self.matches[match.token].participants[0].confirmed)
        self.assertIn("could not be saved", ctx.send.await_args.args[0])
        self.confirm_thread.assert_not_awaited()

    async def test_thread_publication_failure_keeps_confirmation_and_reports_retry(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        match = await self.pair()
        self.confirm_thread.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        ctx = self.action(selection)
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertTrue(self.matches[match.token].participants[0].confirmed)
        self.assertTrue(self.matches[match.token].participants[0].queue_districts_locked)
        self.assertIn("thread publication is incomplete and will be retried", ctx.send.await_args.args[0])

    async def test_missing_channel_reports_incomplete_publication_after_confirmation(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        match = await self.pair()
        self.confirm_thread.side_effect = match_lifecycle_module.MatchChannelUnavailable("unavailable")
        ctx = self.action(selection)
        with self.assertLogs(level="ERROR"):
            await self.invoke("queued_district_action", ctx)
        self.assertTrue(self.matches[match.token].participants[0].confirmed)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("thread publication is incomplete and will be retried", ctx.send.await_args.args[0])

    async def test_lingering_queue_after_match_insert_routes_controls_to_match(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222")
        self.fail_queue = True
        with self.assertRaises(OperationFailure):
            await self.pair()
        self.fail_queue = False
        match = next(iter(self.matches.values()))
        ctx = self.action(selection)
        await self.invoke("queued_district_action", ctx)
        self.assertTrue(self.matches[match.token].participants[0].confirmed)
        self.assertFalse(self.queues[3].district_selections["111"].confirmed)
        self.assertIn("waiting for your opponent", ctx.send.await_args.args[0])

    async def test_queued_cancel_clears_duplicate_copies_of_the_exact_attempt(self):
        selection = self.seed()
        self.queues[2] = self.snapshot(self.queues[3])
        self.queues[2].tier = 2
        ctx = self.action(selection, "cancel")
        await self.invoke("queued_district_action", ctx)
        self.assertTrue(all(not queue.players and not queue.district_selections for queue in self.queues.values()))
        self.assertIn("removed", ctx.send.await_args.args[0])

    async def test_match_cancellation_preserves_weekly_cooldown(self):
        selection = self.seed()
        self.seed("222")
        match = await self.pair()
        self.players["111"].last_cancel = commands.datetime.now().timestamp()
        ctx = self.action(selection, "cancel")
        await self.invoke("queued_district_action", ctx)
        self.assertIn("once every week", ctx.send.await_args.args[0])
        self.assertIn(match.token, self.matches)
        self.bot.wait_for_component.assert_not_called()

    async def test_declining_match_cancellation_keeps_match_and_has_no_penalty(self):
        selection = self.seed()
        self.seed("222")
        match = await self.pair()
        ctx = self.action(selection, "cancel")
        response = self.context(custom_id="cancel")
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=response))
        await self.invoke("queued_district_action", ctx)
        self.assertIn(match.token, self.matches)
        self.assertIsNone(self.players["111"].last_cancel)
        self.assertFalse(self.players["222"].priority)
        self.assertIn("action has been canceled", response.send.await_args.args[0])

    async def test_cancel_prompt_cannot_delete_match_confirmed_while_waiting(self):
        selection = self.seed(districts=DISTRICTS)
        self.seed("222", districts=DISTRICTS, confirmed=True)
        match = await self.pair()
        cancel_ctx = self.action(selection, "cancel")
        response = self.context(custom_id="confirm")

        async def confirm_during_prompt(**kwargs):
            await self.invoke("queued_district_action", self.action(selection))
            return SimpleNamespace(ctx=response)

        self.bot.wait_for_component = AsyncMock(side_effect=confirm_during_prompt)
        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(return_value=False)) as delete:
            await self.invoke("queued_district_action", cancel_ctx)
        self.assertEqual(self.matches[match.token].thread, "9001")
        self.assertIn("changed", response.send.await_args.args[0])
        self.assertIsNone(self.players["111"].last_cancel)
        self.assertFalse(self.players["222"].priority)
        delete.assert_awaited_once()

    async def test_old_match_picker_cannot_change_locked_queue_districts(self):
        self.seed(districts=DISTRICTS, confirmed=True)
        self.seed("222")
        match = await self.pair()
        ctx = self.context(custom_id=f"district_select|{match.token}|0", values=["Skeleton Park"])
        await self.invoke("district_select", ctx)
        self.assertIn("already locked", ctx.send.await_args.args[0])
        self.assertEqual(match.participants[0].districts, DISTRICTS)

    async def test_existing_post_match_confirm_remains_usable_after_optional_picker_is_ignored(self):
        self.seed(districts=DISTRICTS)
        self.seed("222")
        match = await self.pair()
        ctx = self.context(custom_id=f"match_confirm|{match.token}")
        await self.invoke("match_confirm_button", ctx)
        self.assertTrue(match.participants[0].confirmed)
        self.assertIn("waiting for your opponent", ctx.send.await_args.args[0])
        self.bot.fetch_guild.assert_awaited_once_with(match_results_module.MAIN_GUILD)
        opponent = next(field for field in ctx.message.embeds[0].fields if field.name == "**Opponent**")
        self.assertEqual(opponent.value, "**[Player 222](<https://discordapp.com/users/222>)**")

    async def test_reveal_opponent_in_dm_names_the_opponent_from_the_main_server(self):
        self.seed()
        self.seed("222")
        match = await self.pair()
        ctx = self.context(custom_id=f"reveal_opponent|{match.token}")
        await self.invoke("reveal_opponent_button", ctx)
        self.bot.fetch_guild.assert_awaited_once_with(match_results_module.MAIN_GUILD)
        opponent = next(field for field in ctx.message.embeds[0].fields if field.name == "**Opponent**")
        self.assertEqual(opponent.value, "**[Player 222](<https://discordapp.com/users/222>)**")
        self.assertIn("opponent is now revealed", ctx.send.await_args.args[0])

    async def test_unconfirmed_match_with_incomplete_choices_rejects_queue_confirm(self):
        selection = self.seed(districts=[DISTRICTS[0], None, None])
        self.seed("222")
        match = await self.pair()
        ctx = self.action(selection)
        await self.invoke("queued_district_action", ctx)
        self.assertIn("all three", ctx.send.await_args.args[0])
        self.assertFalse(match.participants[0].confirmed)

    async def test_normal_automatic_queueing_does_not_create_optional_selection(self):
        outcome = await match_queue_module.queue_player(self.bot, self.context().author)
        self.assertEqual(outcome.status, match_queue_module.QueueOutcomeStatus.QUEUED)
        self.assertIsNone(outcome.selection)
        self.assertEqual(self.queues[3].district_selections, {})

    async def test_stale_cancel_prompt_does_not_cancel_a_newer_match(self):
        selection = self.seed()
        self.seed("222")
        original = await self.pair()
        ctx = self.action(selection, "cancel")
        response = self.context(custom_id="confirm")

        async def remove_before_prompt_returns(**kwargs):
            self.matches.pop(original.token)
            newer = original.model_copy(deep=True)
            newer.token = "NEWER"
            newer.id = PydanticObjectId()
            self.matches[newer.token] = newer
            return SimpleNamespace(ctx=response)

        self.bot.wait_for_component = AsyncMock(side_effect=remove_before_prompt_returns)
        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock()) as delete:
            await self.invoke("queued_district_action", ctx)
        delete.assert_not_awaited()
        self.assertIn("NEWER", self.matches)
        self.assertIn("changed", response.send.await_args.args[0])
        self.assertIsNone(self.players["111"].last_cancel)
        self.assertFalse(self.players["222"].priority)

    async def test_interrupted_tier_move_retry_preserves_later_confirmed_destination(self):
        selection = self.seed(districts=DISTRICTS)
        self.fail_queue_tier = 3
        with self.assertRaises(OperationFailure):
            await state_module.move_queue_tier(3, 2, 3_000_000_001.0)
        self.fail_queue_tier = None
        await self.invoke("queued_district_action", self.action(selection))
        self.assertTrue(self.queues[2].district_selections["111"].confirmed)
        await state_module.move_queue_tier(3, 2, 3_000_000_002.0)
        self.assertTrue(self.queues[2].district_selections["111"].confirmed)
        self.assertEqual(self.queues[2].district_selections["111"].attempt_id, selection.attempt_id)
        self.assertFalse(self.queues[3].district_selections)

    async def test_cancellation_after_queue_removal_failure_clears_both_exact_memberships(self):
        selection = self.seed()
        self.seed("222")
        self.fail_queue = True
        with self.assertRaises(OperationFailure):
            await self.pair()
        self.fail_queue = False
        match = next(iter(self.matches.values()))
        ctx = self.action(selection, "cancel")
        response = self.context(custom_id="confirm")
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=response))

        async def delete(captured, **kwargs):
            self.matches.pop(captured.token)
            return True

        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(side_effect=delete)), \
                patch.object(Player, "set", new=AsyncMock()):
            await self.invoke("queued_district_action", ctx)
        self.assertNotIn(match.token, self.matches)
        self.assertFalse(self.queues[3].players)
        self.assertFalse(self.queues[3].district_selections)
        self.assertIn("successfully canceled", response.send.await_args.args[0])

    async def test_failed_cancellation_bookkeeping_has_no_success_notification(self):
        selection = self.seed()
        self.seed("222")
        match = await self.pair()
        for user in self.users.values():
            user.send.reset_mock()
        response = self.context(custom_id="confirm")
        self.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=response))
        ctx = self.action(selection, "cancel")

        async def delete(captured, **kwargs):
            self.matches.pop(captured.token)
            return True

        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(side_effect=delete)), \
                patch.object(Player, "set", new=AsyncMock(side_effect=OperationFailure("priority save failed"))):
            with self.assertLogs(level="ERROR"):
                await self.invoke("queued_district_action", ctx)
        self.assertNotIn(match.token, self.matches)
        self.users["111"].send.assert_not_awaited()
        self.users["222"].send.assert_not_awaited()
        self.assertIn("match was removed", response.send.await_args.args[0])
        self.assertIn("opponent priority failed", response.send.await_args.args[0])
        self.assertNotIn("successfully", response.send.await_args.args[0])

    async def test_two_players_queue_edits_serialize_without_overwriting_choices(self):
        first = self.seed(districts=DISTRICTS)
        second = self.seed("222")
        self.pause_queue = True

        async def confirm_first():
            await self.invoke("queued_district_action", self.action(first))

        async def select_second():
            await self.invoke("queued_district_action", self.action(second, "district", user_id="222",
                                                                     index=0, values=["Skeleton Park"]))

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(confirm_first)
                await self.write_started.wait()
                tasks.start_soon(select_second)
                await anyio.wait_all_tasks_blocked()
                self.release_write.set()
        self.assertTrue(self.queues[3].district_selections["111"].confirmed)
        self.assertEqual(self.queues[3].district_selections["222"].districts, ["Skeleton Park", None, None])

    async def test_automatic_expiry_retires_lingering_queue_attempt_before_deleting_match(self):
        selection = self.seed()
        self.seed("222")
        self.fail_queue = True
        with self.assertRaises(OperationFailure):
            await self.pair()
        self.fail_queue = False
        match = next(iter(self.matches.values()))

        async def delete(captured, **kwargs):
            self.assertFalse(self.queues[3].players)
            self.assertFalse(self.queues[3].district_selections)
            self.matches.pop(captured.token)
            return True

        with patch_sites(('elo_bot.extensions.match.lifecycle.delete_unreserved_match', 'elo_bot.features.matches.lifecycle.delete_unreserved_match'), new=AsyncMock(side_effect=delete)):
            await match_lifecycle_module.cancel_match(self.bot, match.token)
        ctx = self.action(selection)
        await self.invoke("queued_district_action", ctx)
        self.assertIn("no longer active", ctx.send.await_args.args[0])
        self.assertFalse(self.matches)


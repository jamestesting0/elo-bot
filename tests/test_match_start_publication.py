from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.features.hosting.live import live_start
import cogs.match_commands as match_commands
from elobot.features.base_store import storage as base_store_storage
from elobot.features.hosting.war import war_start
from elobot.features.matches.messages import MatchMessagePublicationError, mark_match_message_ended
from elobot.discord.embeds import embed_edit_payload
from interactions.models.discord.message import process_message_payload
from elobot.db import Match


from tests._match_publication_fixture import MatchPublicationStateMixin

class TestMatchStartPublication(MatchPublicationStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_war_binding_failure_never_publishes_started_controls(self) -> None:
        # Given: the committed war start cannot bind its Discord message in MongoDB.
        request = self.war_request()
        self.collection.update_one.side_effect = PyMongoError("binding failed")
        # When: the real completion path reaches the failed collection boundary.
        with self.assertRaises(PyMongoError):
            await war_start.complete_war_start(self.bot, self.ctx, request)
        # Then: Discord never receives a successful Started message or active controls.
        initial = self.bot.http.create_message.await_args.args[0]
        self.assertEqual(initial["embeds"][0]["title"], "**Match**")
        self.assertEqual(initial["embeds"][0]["description"], "Updating…")
        self.assertEqual(initial["components"], [])
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.bot.http.pin_message.assert_not_awaited()
        self.ctx.send.assert_not_awaited()
        self.assertEqual(self.match.times, [1000, None])
        self.assertEqual(self.match.old_msgs, [None, None])

    async def test_live_binding_failure_never_publishes_started(self) -> None:
        # Given: the committed live start cannot bind its Discord message in MongoDB.
        request = self.live_request()
        self.collection.update_one.side_effect = PyMongoError("binding failed")
        # When: the real completion path reaches the failed collection boundary.
        with self.assertRaises(PyMongoError):
            await live_start.complete_live_start(self.bot, self.ctx, request)
        # Then: its public thread message remains incomplete without confirming success.
        initial = self.bot.http.create_message.await_args.args[0]
        self.assertEqual(initial["embeds"][0]["title"], "**Match**")
        self.assertEqual(initial["embeds"][0]["description"], "Updating…")
        self.assertEqual(initial["components"], [])
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.bot.http.pin_message.assert_not_awaited()
        self.ctx.send.assert_not_awaited()
        self.assertEqual(self.match.times, [1000, None])
        self.assertEqual(self.match.old_msgs, [None, None])

    async def test_war_success_enables_bound_controls_and_preserves_run_details(self) -> None:
        # Given: the committed war side still owns its original run.
        request = self.war_request()
        # When: the SDK publishes its message through the Match boundary.
        message = await war_start.complete_war_start(self.bot, self.ctx, request)
        # Then: final controls are enabled only after their message binding commits.
        self.assertEqual(self.trace, ["send", "bind", "bind", "edit", "pin", "bind"])
        self.assertEqual(self.persisted["old_msgs.0"], int(message.id))
        self.assertEqual(self.match.old_msgs, [43, None])
        self.assertEqual(self.match.get_saved_state()["old_msgs"], [43, None])
        payload = self.bot.http.edit_message.await_args.args[0]
        self.assertEqual(payload["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(payload["embeds"][0]["description"], "")
        self.assertEqual(payload["embeds"][0]["fields"][1]["value"], "**[Host](<https://discordapp.com/users/999>)**")
        self.assertEqual([component["custom_id"] for row in payload["components"] for component in row["components"]],
                         ["host_clan_select|TOKEN|0", "match_host|TOKEN|0", "match_result_store|TOKEN|0",
                          "war_spectate|TOKEN|0|1000", "match_abort|TOKEN|0"])
        updates = self.collection.update_one.await_args_list[1].args[1]["$set"]
        self.assertEqual(updates["old_msgs.0"], 43)
        self.assertEqual(updates["message_publications.0"]["message_id"], 43)
        self.assertEqual(updates["message_publications.0"]["embeds"], payload["embeds"])
        self.assertEqual(updates["message_publications.0"]["components"], payload["components"])
        self.assertIsNone(self.collection.update_one.await_args_list[1].args[0]["old_msgs.0"])
        base_store_storage.clear_participant_base_store.assert_not_awaited()

    async def test_replaced_war_run_cannot_acquire_delayed_message_binding(self) -> None:
        # Given: the run changes while Discord creates the pending message.
        async def replace_during_send(*args, **kwargs):
            message = await self.create_message(*args, **kwargs)
            self.persisted.update({"times.0": 2000, "old_msgs.0": 88})
            self.match.times[0], self.match.old_msgs[0] = 2000, 88
            return message

        self.bot.http.create_message.side_effect = replace_during_send
        # When: the delayed completion attempts to bind its original run.
        with self.assertRaises(MatchMessagePublicationError):
            await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        # Then: the replacement remains intact and the stale UI has no active controls.
        self.assertEqual(self.persisted["old_msgs.0"], 88)
        self.assertEqual(self.match.old_msgs[0], 88)
        self.assertEqual(self.collection.update_one.await_args.args[0]["times.0"], 1000)
        self.assertEqual(self.collection.update_one.await_args_list[1].args[0]["old_msgs.0"], None)
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        war_start.match_commands.send_bases.assert_not_awaited()
        self.bot.http.pin_message.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_live_binding_checks_both_sides_of_original_run(self) -> None:
        # Given: only the live opponent side changes before the binding write.
        request = self.live_request()
        self.persisted["times.1"] = 2000
        # When: the original live start tries to publish.
        with self.assertRaises(MatchMessagePublicationError):
            await live_start.complete_live_start(self.bot, self.ctx, request)
        # Then: neither side's binding is overwritten and no success is returned.
        self.assertIsNone(self.persisted["old_msgs.0"])
        self.assertEqual(self.collection.update_one.await_args.args[0]["times.1"], None)
        self.assertEqual(self.message_data["components"], [])
        war_start.match_commands.send_bases.assert_not_awaited()

    async def test_live_success_preserves_attacking_order_without_releasing_slots(self) -> None:
        # Given: the committed live match has an existing public announcement.
        request = self.live_request()
        self.match.msg = "80"
        self.persisted["msg"] = "80"
        public = MagicMock(id=80, _channel_id=90, components=[], embeds=[ipy.Embed(fields=[ipy.EmbedField("Players", "100 vs 200"),
                                                  ipy.EmbedField("Time", "1000")])], edit=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=MagicMock(id=90, fetch_message=AsyncMock(return_value=public)))
        with patch.object(live_start.match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.match))), \
                patch.object(live_start, "schedule_channel_name_edit"):
            # When: the live completion publishes its bound thread message.
            message = await live_start.complete_live_start(self.bot, self.ctx, request)
        # Then: the SDK retains attacking order and final state before normal cleanup.
        self.assertEqual(self.message_data["content"], "<@100> (Attacking First)\n<@200>")
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(public.edit.await_args.kwargs["components"][0].to_dict(), {
            "type": 1,
            "components": [{
                "type": 2, "style": 2, "label": "Spectate",
                "emoji": {"name": "👀", "animated": False, "available": True},
                "custom_id": "live_spectate|TOKEN|1000", "disabled": False, "url": None, "sku_id": None,
            }],
        })
        self.assertEqual(self.persisted["old_msgs.0"], int(message.id))
        self.assertEqual(self.trace, ["send", "bind", "bind", "edit", "pin", "bind", "bind", "bind"])
        live_start.match_commands.clear_match_base_stores.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_new_public_spectate_control_waits_for_message_binding(self) -> None:
        request = self.live_request()
        public = MagicMock(id=80, edit=AsyncMock())
        channel = MagicMock(id=90, send=AsyncMock(return_value=public))
        self.bot.fetch_channel = AsyncMock(return_value=channel)

        async def bind_public(query, update):
            if "msg" in update["$set"]:
                public.edit.assert_not_awaited()
            return await self.update_message_binding(query, update)

        with (
            patch.object(live_start.match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.match))),
            patch.object(live_start.match_commands, "choose_tip", new=AsyncMock(return_value="Tip")),
            patch.object(self.collection, "update_one", new=AsyncMock(side_effect=bind_public)),
            patch.object(live_start, "schedule_channel_name_edit"),
        ):
            await live_start.complete_live_start(self.bot, self.ctx, request)
        self.assertEqual(self.match.msg, "80")
        self.assertEqual(channel.send.await_args.kwargs["components"], [])
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(public.edit.await_args.kwargs["components"][0].components[0].custom_id,
                         "live_spectate|TOKEN|1000")

    async def test_failed_public_binding_does_not_enable_spectate(self) -> None:
        request = self.live_request()
        public = MagicMock(id=80, edit=AsyncMock())
        channel = MagicMock(id=90, send=AsyncMock(return_value=public))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        async def fail_public_binding(query, update):
            if "msg" in update["$set"]:
                raise PyMongoError("binding failed")
            return await self.update_message_binding(query, update)

        with (
            patch.object(live_start.match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.match))),
            patch.object(live_start, "schedule_channel_name_edit"),
            patch.object(live_start.match_commands, "choose_tip", new=AsyncMock(return_value="Tip")),
            patch.object(self.collection, "update_one", new=AsyncMock(side_effect=fail_public_binding)),
        ):
            with self.assertRaises(PyMongoError):
                await live_start.complete_live_start(self.bot, self.ctx, request)
        self.assertIsNone(self.match.msg)
        self.assertEqual(channel.send.await_args.kwargs["components"], [])
        self.assertEqual(public.edit.await_args.kwargs["components"], [])
        self.assertEqual(public.edit.await_args.kwargs["embed"].title, "**Match Update Incomplete**")
        self.assertIsNone(self.persisted["msg"])

    async def test_binding_lost_during_final_edit_retires_started_controls(self) -> None:
        # Given: a replacement wins during the final Discord edit.
        async def replace_during_edit(*args, **kwargs):
            message = await self.edit_message(*args, **kwargs)
            self.persisted.update({"times.0": 2000, "old_msgs.0": 88})
            return message

        self.bot.http.edit_message.side_effect = replace_during_edit
        # When: publication verifies the run after its final edit and pin.
        with self.assertRaises(MatchMessagePublicationError):
            await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        # Then: the stale controls are retired before any caller success or cleanup.
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.assertEqual(self.persisted["old_msgs.0"], 88)
        self.ctx.send.assert_not_awaited()
        base_store_storage.clear_participant_base_store.assert_not_awaited()

    async def test_discord_final_edit_failure_preserves_durable_start(self) -> None:
        # Given: binding succeeds but Discord rejects the final active message.
        async def reject_active_edit(payload, *args, **kwargs):
            if payload["components"]:
                raise ipy.errors.HTTPException(response=MagicMock(status=500))
            return await self.edit_message(payload, *args, **kwargs)

        self.bot.http.edit_message.side_effect = reject_active_edit
        # When: the real SDK reports that publication failure.
        with self.assertRaises(ipy.errors.HTTPException):
            await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        # Then: only UI state is retired; the committed start and binding remain.
        self.assertEqual(self.persisted["times.0"], 1000)
        self.assertEqual(self.persisted["hosts.0"], "999")
        self.assertEqual(self.persisted["host_clans.0"], "#CLAN")
        self.assertEqual(self.persisted["old_msgs.0"], 43)
        self.assertEqual(self.message_data["components"], [])
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Update Incomplete**")
        self.assertEqual(self.collection.update_one.await_count, 2)
        base_store_storage.clear_participant_base_store.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_binding_failure_preserves_previous_message(self) -> None:
        # Given: a prior scheduled message remains available until its replacement binds.
        self.match.old_msgs[0] = 77
        self.persisted["old_msgs.0"] = 77
        self.collection.update_one.side_effect = PyMongoError("binding failed")
        # When: the replacement cannot be persisted.
        with self.assertRaises(PyMongoError):
            await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        # Then: the previously bound message remains available and is not deleted.
        self.assertEqual(self.persisted["old_msgs.0"], 77)
        self.bot.http.get_message.assert_not_awaited()
        self.bot.http.delete_message.assert_not_awaited()

    async def test_finishing_side_clears_stale_publication_description(self) -> None:
        self.enterContext(patch("elobot.discord.message_edits._now", side_effect=range(100)))
        # Given: a real Discord message still contains the old placeholder.
        for title in ("**Match Started**", "**Match Ended**"):
            for description in ("Updating…", "Updating...", "Loading…", "Loading..."):
                with self.subTest(title=title, description=description):
                    self.message_data["embeds"] = [{
                        "title": title, "description": description,
                        "fields": [{"name": "Host", "value": "<@999>", "inline": False}],
                    }]
                    self.bot.cache.place_message_data(deepcopy(self.message_data))
                    self.match.old_msgs[0] = 43
                    cog = MagicMock(bot=self.bot)
                    # When: completed results update the initiation message.
                    with (
                        patch.object(Match, "find_one", new=AsyncMock(return_value=self.match)),
                        patch.object(match_commands, "free_clan", new_callable=AsyncMock),
                        patch.object(match_commands, "remove_helper_task", new_callable=AsyncMock),
                        patch.object(match_commands, "autoqueue_completed_match_participants", new_callable=AsyncMock),
                    ):
                        await match_commands.MatchCommands._finish_stored_result_side(cog, self.match, 0)
                    # Then: both newly ended and already-ended messages lose the placeholder.
                    final = self.message_data["embeds"][0]
                    self.assertEqual(final["title"], "**Match Ended**")
                    self.assertFalse(final.get("description"))
                    self.assertEqual(final["fields"], [{"name": "Host", "value": "<@999>", "inline": False}])


    def test_embed_edit_payload_explicitly_clears_only_empty_descriptions(self) -> None:
        for description in (None, "", "Keep this instruction."):
            with self.subTest(description=description):
                embed = ipy.Embed(title="Match Started", description=description)
                payload = process_message_payload(embeds=embed_edit_payload(embed))
                self.assertEqual(payload["embeds"][0]["description"], description or "")
                self.assertEqual(embed.description, description)

    async def test_ending_message_preserves_useful_description_and_retries_failed_edit(self) -> None:
        self.message_data["embeds"] = [{"title": "**Match Started**", "description": "Keep this instruction."}]
        message = self.bot.cache.place_message_data(deepcopy(self.message_data))
        self.bot.http.edit_message.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        with self.assertRaises(ipy.errors.HTTPException):
            await mark_match_message_ended(message)
        self.assertEqual(message.embeds[0].title, "**Match Started**")
        self.bot.http.edit_message.side_effect = self.edit_message
        await mark_match_message_ended(message)
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Ended**")
        self.assertEqual(self.message_data["embeds"][0]["description"], "Keep this instruction.")

    async def test_committed_final_payload_can_restore_failed_discord_publication(self) -> None:
        # Given: the final edit failed after the exact payload was committed.
        self.bot.http.edit_message.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        with self.assertRaises(ipy.errors.HTTPException):
            await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        message = self.bot.cache.place_message_data(deepcopy(self.message_data))
        self.bot.http.edit_message.side_effect = self.edit_message
        from elobot.features.matches.publication_state import restore_match_publication
        # When: recovery replays the still-current publication.
        restored = await restore_match_publication(message, self.match, 0)
        # Then: exact final instructions and controls survive the failed initial edit.
        self.assertTrue(restored)
        self.assertEqual(self.message_data["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(self.message_data["embeds"][0]["description"], "")
        self.assertTrue(self.message_data["components"])

    async def test_replay_rejects_changed_durable_run(self) -> None:
        # Given: another run replaced this publication after the model was loaded.
        message = await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        self.persisted["times.0"] = 2000
        self.bot.http.edit_message.reset_mock()
        from elobot.features.matches.publication_state import restore_match_publication
        # When: recovery attempts the stale model's final payload.
        restored = await restore_match_publication(message, self.match, 0)
        # Then: stale controls are never republished.
        self.assertFalse(restored)
        self.bot.http.edit_message.assert_not_awaited()

    async def test_legacy_document_initializes_array_before_binding_final_intent(self) -> None:
        # Given: an old document has no persisted publication field.
        self.match.model_fields_set.discard("message_publications")
        # When: its first final message is bound.
        await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        # Then: Mongo receives array initialization before any dotted slot update.
        calls = self.collection.update_one.await_args_list
        self.assertEqual(calls[0].args, (
            {"_id": self.match.id, "message_publications": {"$exists": False}},
            {"$set": {"message_publications": [None, None]}},
        ))
        self.assertIn("message_publications.0", calls[1].args[1]["$set"])

    async def test_missing_array_is_initialized_even_when_model_field_is_set(self) -> None:
        self.assertIn("message_publications", self.match.model_fields_set)
        self.assertNotIn("message_publications", self.persisted)
        await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        self.assertEqual(self.collection.update_one.await_args_list[0].args, (
            {"_id": self.match.id, "message_publications": {"$exists": False}},
            {"$set": {"message_publications": [None, None]}},
        ))

    async def test_publication_saved_state_does_not_rewrite_other_side(self) -> None:
        # Given: one side publishes while the other side remains independently writable.
        await war_start.complete_war_start(self.bot, self.ctx, self.war_request())
        self.match.hosts[1] = "other-host"
        # When: Beanie calculates its subsequent ordinary state update.
        changes = self.match.get_changes()
        # Then: the already committed intent and the opponent's slot are not rewritten.
        self.assertEqual(changes, {"hosts.1": "other-host"})

    async def test_replay_rejects_other_live_side_change(self) -> None:
        # Given: a live payload was bound with both participants' original run details.
        from elobot.features.matches.messages import MatchMessageState, publish_match_message
        self.live_request()
        message = await publish_match_message(
            self.ctx, self.match, MatchMessageState.capture(self.match, 0), embed=ipy.Embed(title="**Match Started**"),
        )
        self.match.times[1] = 2000
        self.bot.http.edit_message.reset_mock()
        from elobot.features.matches.publication_state import restore_match_publication
        # When: recovery considers the old live payload.
        restored = await restore_match_publication(message, self.match, 0)
        # Then: a changed opponent run also disqualifies that payload.
        self.assertFalse(restored)
        self.bot.http.edit_message.assert_not_awaited()

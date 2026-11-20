from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import interactions as ipy
from anyio import fail_after, sleep_forever
from pymongo.errors import OperationFailure

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.db.timestamp_models import TimestampDraft, TimestampPost, TimestampPreference
from elo_bot.discord.contexts import ComponentsV2ModalContext
from elo_bot.extensions.timestamp import TimestampCommands
from tests.integration._timestamp_fixture import MESSAGE_PERMISSIONS, TimestampMongoCase, timestamp


class TestTimestampCommands(TimestampMongoCase):
    async def test_first_use_refuses_to_post_until_zone_selected_and_saves_replacement(self):
        await self.slash("in two hours")
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"],
                         ipy.MessageFlags.EPHEMERAL)
        error = self.http.edit_interaction_message.await_args.args[0]["content"]
        self.assertTrue(error.startswith(str(ERROR_EMOJI)))
        self.assertIn("`country`", error)
        self.http.execute_webhook.assert_not_awaited()
        self.assertIsNone(await TimestampPreference.get(9))

        autocomplete = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [
            {"name": "country", "type": 3, "value": "Germany", "focused": True},
        ]})
        await self.cog.timestamp.autocomplete_callbacks["country"].func(self.cog, autocomplete)
        choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
        berlin = next(choice for choice in choices if "Berlin" in choice["name"])

        async def verify_saved_before_post(*args, **kwargs):
            self.assertEqual((await TimestampPreference.get(9)).zone, "Europe/Berlin")
            return self.webhook_message(*args, **kwargs)

        self.http.execute_webhook.side_effect = verify_saved_before_post
        await self.slash("tomorrow at 19", berlin["value"])
        self.assert_public_timestamp(timestamp(17, 18))
        self.http.delete_interaction_message.assert_awaited_once()

        self.http.execute_webhook.side_effect = self.webhook_message
        await self.slash("tomorrow at 19", "America/Los_Angeles")
        self.assertEqual((await TimestampPreference.get(9)).zone, "America/Los_Angeles")
        self.assertEqual(await TimestampPreference.count(), 1)
        self.http.execute_webhook.reset_mock()
        await self.slash("tomorrow at 19")
        self.assert_public_timestamp(timestamp(18, 3))

    async def test_seven_upcoming_options_match_private_picker_and_only_post_once(self):
        await TimestampPreference(id=9, zone="Europe/Berlin").insert()
        ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [
            {"name": "when", "type": 3, "value": "19", "focused": True},
        ]})
        await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
        choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
        self.assertEqual([option["value"].split("|")[1] for option in choices],
                         [str(int(datetime(2026, 11, day, 18, tzinfo=UTC).timestamp())) for day in range(17, 24)])

        first = await self.slash("19")
        menu = self.http.edit_interaction_message.await_args.args[0]["components"][0]["components"][0]
        self.assertEqual([option["label"] for option in menu["options"]], [choice["name"] for choice in choices])
        self.http.execute_webhook.assert_not_awaited()
        restarted = TimestampCommands(ipy.Client())
        await restarted.choose_timestamp.callback.func(restarted, self.choose(menu, 6, user=10))
        self.http.execute_webhook.assert_not_awaited()
        await restarted.choose_timestamp.callback.func(restarted, self.choose(menu, 6))
        self.assert_public_timestamp(timestamp(23, 18))
        success = self.http.edit_interaction_message.await_args.args[0]
        self.assertTrue(success["content"].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(success["components"], [])
        self.assertIsNone(await TimestampDraft.get(int(first.id)))
        await restarted.choose_timestamp.callback.func(restarted, self.choose(menu, 6))
        self.assert_public_timestamp(timestamp(23, 18))
        self.assertEqual(await TimestampPost.owner_for_message(self.post_id), 9)

    async def test_discord_permission_failure_keeps_saved_zone_and_reports_failure_privately(self):
        self.http.execute_webhook.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        await self.slash("tomorrow at 19", "Europe/Berlin")
        self.assertEqual((await TimestampPreference.get(9)).zone, "Europe/Berlin")
        self.assertEqual(self.http.post_initial_response.await_args.args[0]["data"]["flags"],
                         ipy.MessageFlags.EPHEMERAL)
        self.assertTrue(self.http.edit_interaction_message.await_args.args[0]["content"].startswith(str(ERROR_EMOJI)))
        self.http.delete_interaction_message.assert_not_awaited()

    async def test_webhook_does_not_bypass_thread_send_permissions(self):
        self.use_private_thread()
        for permissions, locked in ((ipy.Permissions.SEND_MESSAGES, False), (MESSAGE_PERMISSIONS, True)):
            with self.subTest(permissions=permissions, locked=locked):
                self.member_permissions = permissions
                self.channel.locked = locked
                await self.slash("tomorrow at 19", "Europe/Berlin")
                self.http.execute_webhook.assert_not_awaited()
                self.parent.fetch_webhooks.assert_not_awaited()
                self.assertEqual(await TimestampPost.count(), 0)
                receipt = self.http.edit_interaction_message.await_args.args[0]["content"]
                self.assertTrue(receipt.startswith(str(ERROR_EMOJI)))

    async def test_private_thread_webhook_sends_exact_profile_and_tracks_the_returned_post_before_ack(self):
        self.use_private_thread()
        foreign = ipy.Webhook(MagicMock(), id=99, type=ipy.WebhookTypes.INCOMING, application_id="99",
                             name="Elo Timestamp", token="foreign-token")
        self.parent.fetch_webhooks.side_effect = [[foreign], [foreign, self.webhook]]
        self.parent.create_webhook.return_value = self.webhook

        async def check_attribution(*_args):
            self.assertEqual(await TimestampPost.owner_for_message(self.post_id), 9)

        self.http.delete_interaction_message.side_effect = check_attribution
        for _ in range(2):
            await self.slash("tomorrow at 19", "Europe/Berlin")

        for call in self.http.execute_webhook.await_args_list:
            webhook_id, _, payload, wait, thread_id, _ = call.args
            self.assertEqual((webhook_id, wait, thread_id), (43, True, 77))
            self.assertEqual(payload["content"], timestamp(17, 18))
            self.assertEqual(payload["username"], "James (Finals)")
            self.assertEqual(payload["avatar_url"], "https://cdn.discordapp.com/avatars/9/example.png")
        self.assertEqual(self.http.execute_webhook.await_count, 2)
        self.parent.create_webhook.assert_awaited_once()
        record = await TimestampPost.get(self.post_id)
        self.assertEqual(record.expires_at.replace(tzinfo=UTC), ipy.Snowflake(self.post_id).created_at + timedelta(days=7))

    async def test_attribution_failure_rolls_back_the_thread_message_and_replies_privately(self):
        self.use_private_thread()
        error = OperationFailure("write rejected")
        for remove_error, outcome in ((None, "removed"), (TimeoutError(), "Check the channel")):
            with self.subTest(outcome=outcome):
                self.http.execute_webhook.reset_mock()
                self.http.request.side_effect = remove_error
                with patch.object(TimestampPost, "insert", side_effect=error):
                    await self.slash("tomorrow at 19", "Europe/Berlin")
                self.assert_public_timestamp(timestamp(17, 18))
                route = self.http.request.await_args.args[0]
                self.assertEqual((route.method, route.params["message_id"]), ("DELETE", self.post_id))
                self.assertEqual(self.http.request.await_args.kwargs["params"], {"thread_id": 77})
                receipt = self.http.edit_interaction_message.await_args.args[0]["content"]
                self.assertTrue(receipt.startswith(str(ERROR_EMOJI)))
                self.assertIn(outcome, receipt)
                self.assertEqual(await TimestampPost.count(), 0)
                self.http.delete_interaction_message.assert_not_awaited()

    async def test_first_use_autocomplete_shows_setup_then_resolves_same_input_with_country(self):
        when = {"name": "when", "type": 3, "value": "day 1", "focused": True}
        for country in ([], [{"name": "country", "type": 3, "value": "Europe/Berlin"}]):
            ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [when, *country]})
            await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
            choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
            self.assertEqual(len(choices), 1)
            if not country:
                self.assertIn("country", choices[0]["name"])
                self.assertEqual(choices[0]["value"], "in 1 day")
            else:
                self.assertIn("17 Nov", choices[0]["name"])
                self.assertEqual(choices[0]["value"].split("|")[1], timestamp(17, 20).split(":")[1])
        self.assertIsNone(await TimestampPreference.get(9))

    async def test_raw_spelling_guess_requires_confirmation_before_publication(self):
        await self.slash("tommorow at 19", "Europe/Berlin")
        self.http.execute_webhook.assert_not_awaited()
        error = self.http.edit_interaction_message.await_args.args[0]["content"]
        self.assertIn("`tomorrow at 19`", error)

    async def test_clock_autocomplete_works_with_and_without_country_selected(self):
        when = {"name": "when", "type": 3, "value": "18:00", "focused": True}
        ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [when]})
        await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
        choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
        self.assertIn("country", choices[0]["name"])
        self.assertEqual(choices[0]["value"], "18:00")

        country = {"name": "country", "type": 3, "value": "Europe/Berlin"}
        ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [when, country]})
        await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
        choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
        self.assertEqual([item["value"].split("|")[1] for item in choices],
                         [timestamp(day, 17).split(":")[1] for day in range(17, 24)])

    async def test_stalled_time_zone_lookup_still_returns_autocomplete_guidance(self):
        ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [
            {"name": "when", "type": 3, "value": "18:00", "focused": True},
        ]})
        async def stalled_lookup(_owner):
            await sleep_forever()

        with (patch("elo_bot.extensions.timestamp.saved_zone", side_effect=stalled_lookup),
              patch("elo_bot.extensions.timestamp._AUTOCOMPLETE_LOOKUP_SECONDS", 0.001), fail_after(1)):
            await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
        choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
        self.assertIn("country", choices[0]["name"])
        self.assertEqual(choices[0]["value"], "18:00")

    async def test_calendar_period_autocomplete_sends_the_exact_days_to_discord(self):
        for phrase, expected in (("next day", ["2026-11-17"]),
                                 ("next week", [f"2026-11-{day}" for day in range(23, 30)])):
            with self.subTest(phrase=phrase):
                ctx = self.context(ipy.AutocompleteContext, {"id": "42", "name": "timestamp", "options": [
                    {"name": "when", "type": 3, "value": phrase, "focused": True},
                    {"name": "country", "type": 3, "value": "Europe/Berlin"},
                ]})
                await self.cog.timestamp.autocomplete_callbacks["when"].func(self.cog, ctx)
                choices = self.http.post_initial_response.await_args.args[0]["data"]["choices"]
                self.assertEqual([choice["value"] for choice in choices],
                                 [f"d|{day}|Europe/Berlin" for day in expected])

    async def test_date_only_input_opens_time_modal_and_invalid_time_keeps_draft(self):
        first = await self.slash("next Thursday", "Europe/Berlin")
        button = self.http.edit_interaction_message.await_args.args[0]["components"][0]["components"][0]
        ctx = self.context(ipy.ComponentContext, {"custom_id": button["custom_id"], "component_type": 2})
        await self.cog.choose_timestamp.callback.func(self.cog, ctx)
        modal = self.http.post_initial_response.await_args.args[0]
        self.assertEqual(modal["type"], 9)
        self.http.execute_webhook.assert_not_awaited()

        for clock in ("25:00", "19:30"):
            ctx = self.context(ComponentsV2ModalContext, {"custom_id": modal["data"]["custom_id"], "components": [
                {"type": 1, "components": [{"type": 4, "custom_id": "clock", "value": clock}]},
            ]})
            await self.cog.enter_clock.callback.func(self.cog, ctx, **ctx.responses)
            if clock == "25:00":
                self.http.execute_webhook.assert_not_awaited()
                self.assertIsNotNone(await TimestampDraft.get(int(first.id)))
        self.assert_public_timestamp(timestamp(26, 18, 30))
        self.assertIsNone(await TimestampDraft.get(int(first.id)))

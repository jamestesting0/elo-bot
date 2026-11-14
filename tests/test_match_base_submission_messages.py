import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import PyMongoError

from elobot.extensions.match import MatchCommands
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.db import BaseStore, Match, Participant
from tests._patch_sites import patch_sites

BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3ASUBMITTED"


class TestMatchBaseSubmissionMessages(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="existing")
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="123",
            times=[3_000_000_000, 3_000_000_000],
            final_districts=["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"],
            participants=[Participant(user_id="111"), Participant(user_id="222", base_store=self.store)],
        )
        self.match._save_state()
        database_set = patch.object(Match, "set", new=AsyncMock(return_value=self.match))
        self.database_set = database_set.start()
        self.addCleanup(database_set.stop)
        match_model = patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
        match_model.start().find_one = AsyncMock(return_value=self.match)
        self.addCleanup(match_model.stop)
        base_model = patch_sites(('elobot.extensions.match.bases.BaseLayout', 'elobot.features.matches.attack_bases.BaseLayout', 'elobot.features.results.recording.BaseLayout'))
        base_model.start().find_one = AsyncMock(return_value=None)
        self.addCleanup(base_model.stop)
        self.thread = MagicMock(id=123)
        self.thread.fetch_pinned_messages = AsyncMock(return_value=[])
        self.thread.fetch_messages = AsyncMock(return_value=[])

        async def history(*, limit):
            self.assertEqual(limit, 0)
            for message in self.thread.fetch_messages.return_value:
                yield message

        self.thread.history = history
        self.bot = MagicMock()
        self.bot.user.id = 999
        self.bot.owner.id = 999_999
        self.bot.fetch_channel = AsyncMock(return_value=self.thread)
        self.cog = MatchCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.ComponentContext)
        self.ctx.author.id = 111
        self.ctx.author.mention = "<@111>"
        self.ctx.author.display_name = "Player"
        self.ctx.channel = self.thread
        self.ctx.custom_id = "submit_bases|TOKEN"
        self.ctx.send_modal = AsyncMock()
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock()

    @staticmethod
    def message(*, with_embed: bool = True) -> MagicMock:
        embed = ipy.Embed(fields=[ipy.EmbedField(name=str(i), value="value") for i in range(4)])
        message = MagicMock(
            id=456, embeds=[embed] if with_embed else [], pin=AsyncMock(), edit=AsyncMock(), pinned=False,
            components=[ipy.ActionRow(ipy.Button(
                style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id="submit_button|TOKEN",
            ))],
        )
        message.author.id = 999
        return message

    def assert_bases_saved(self) -> None:
        self.database_set.assert_awaited_once()
        updates = self.database_set.await_args.args[0]
        self.assertEqual(set(updates), {"participants.0.bases.0"})
        self.assertEqual(updates["participants.0.bases.0"]["link"], BASE_LINK)
        self.assertEqual(self.match.participants[1].base_store, self.store)

    async def test_button_opens_modal_when_thread_is_empty(self) -> None:
        # Given: neither pins nor recent history contain a message.
        # When: the participant opens base submission through its current callback binding.
        await self.cog.submit_bases_button.callback.func(self.cog, self.ctx)

        # Then: the editable modal opens without announcing or persisting a submission.
        self.ctx.send_modal.assert_awaited_once()
        modal = self.ctx.send_modal.await_args.args[0]
        self.assertEqual([field.custom_id for field in modal.components], self.match.final_districts)
        self.ctx.send.assert_not_awaited()
        self.database_set.assert_not_awaited()

    async def test_button_opens_modal_when_selected_message_has_no_embed(self) -> None:
        # Given: the only history message is plain text.
        message = self.message(with_embed=False)
        self.thread.fetch_messages.return_value = [message]

        # When: the participant opens base submission.
        await self.cog.submit_bases_button.callback.func(self.cog, self.ctx)

        # Then: the modal opens without pinning or editing the unrelated message.
        self.ctx.send_modal.assert_awaited_once()
        message.pin.assert_not_awaited()
        message.edit.assert_not_awaited()

    async def test_submissions_save_bases_and_report_missing_thread_message(self) -> None:
        for modal in (True, False):
            with self.subTest(modal=modal):
                # Given: no match message can be updated after persistence.
                self.match.participants[0].bases = [None, None, None]
                self.match._save_state()
                self.database_set.reset_mock()
                self.ctx.send.reset_mock()

                # When: the participant submits through the modal or slash command.
                if modal:
                    self.ctx.custom_id = "submit_bases_modal|TOKEN"
                    await self.cog.submit_bases_modal.callback.func(
                        self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK},
                    )
                else:
                    await self.cog.match_base_submit.callback.func(self.cog, self.ctx, base_link=BASE_LINK)

                # Then: the base is saved without releasing storage, and publication is explicitly incomplete.
                self.assert_bases_saved()
                self.ctx.send.assert_awaited_once()
                content = self.ctx.send.await_args.args[0]
                self.assertIn("saved", content)
                self.assertIn("match message", content)
                self.assertIn(str(ERROR_EMOJI), content)
                self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])

    async def test_submissions_do_not_pin_or_edit_a_text_only_message(self) -> None:
        # Given: the selected pinned message has no embed.
        message = self.message(with_embed=False)
        self.thread.fetch_pinned_messages.return_value = [message]
        self.ctx.custom_id = "submit_bases_modal|TOKEN"

        # When: the participant submits a valid base.
        await self.cog.submit_bases_modal.callback.func(self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK})

        # Then: saving succeeds, while the unrelated message remains untouched.
        self.assert_bases_saved()
        message.pin.assert_not_awaited()
        message.edit.assert_not_awaited()
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])

    async def test_modal_updates_pinned_and_unpinned_embeds_after_saving(self) -> None:
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                # Given: an existing match announcement is available.
                message = self.message()
                message.pinned = pinned
                self.thread.fetch_pinned_messages.return_value = [message] if pinned else []
                self.thread.fetch_messages.return_value = [message]
                self.match.participants[0].bases = [None, None, None]
                self.match._save_state()
                self.database_set.reset_mock()
                self.ctx.custom_id = "submit_bases_modal|TOKEN"
                events = []
                self.database_set.side_effect = lambda *_args, event_log=events, **_kwargs: (
                    event_log.append("save") or self.match
                )
                message.edit.side_effect = lambda event_log=events, **_kwargs: event_log.append("edit")

                # When: the participant submits a valid base.
                await self.cog.submit_bases_modal.callback.func(self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK})

                # Then: publication follows persistence, and only an unpinned announcement is pinned.
                self.assert_bases_saved()
                self.assertEqual(events, ["save", "edit"])
                self.assertEqual(message.pin.await_count, 0 if pinned else 1)
                self.assertEqual(message.embeds[0].fields[4].value,
                                 "- **[Player](<https://discordapp.com/users/111>)** has submitted bases.")
                self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_submissions_preserve_existing_notes_for_the_same_user_id(self) -> None:
        for modal in (True, False):
            for mention in (
                    "<@111>", "<@!111>", "[Old Name](<https://discordapp.com/users/111>)",
                    "**[Old Name](<https://discordapp.com/users/111>)**",
            ):
                with self.subTest(modal=modal, mention=mention):
                    message = self.message()
                    note = f"- {mention} has submitted bases."
                    message.embeds[0].add_field(name="**Notes**", value=note)
                    self.thread.fetch_pinned_messages.return_value = [message]
                    self.match.participants[0].bases = [None, None, None]
                    self.match._save_state()
                    if modal:
                        self.ctx.custom_id = "submit_bases_modal|TOKEN"
                        await self.cog.submit_bases_modal.callback.func(
                            self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK},
                        )
                    else:
                        await self.cog.match_base_submit.callback.func(self.cog, self.ctx, base_link=BASE_LINK)
                    self.assertEqual(message.embeds[0].fields[4].value, note)

    async def test_failed_save_does_not_publish_or_announce_submission(self) -> None:
        # Given: persistence fails while a match announcement is available.
        message = self.message()
        self.thread.fetch_pinned_messages.return_value = [message]
        self.database_set.side_effect = PyMongoError("write failed")
        self.ctx.custom_id = "submit_bases_modal|TOKEN"

        # When: the participant submits a valid base.
        with self.assertRaises(PyMongoError):
            await self.cog.submit_bases_modal.callback.func(self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK})

        # Then: the failed write leaves no success response or publication work.
        self.bot.fetch_channel.assert_not_awaited()
        message.pin.assert_not_awaited()
        message.edit.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_failed_publication_preserves_saved_bases_and_storage(self) -> None:
        message = self.message()
        self.thread.fetch_pinned_messages.return_value = [message]
        message.edit.side_effect = ipy.errors.HTTPException(response=SimpleNamespace(status=500, reason="failed"))
        self.ctx.custom_id = "submit_bases_modal|TOKEN"

        with self.assertRaises(ipy.errors.HTTPException):
            await self.cog.submit_bases_modal.callback.func(self.cog, self.ctx, **{"Barbarian Camp": BASE_LINK})

        self.assert_bases_saved()
        self.ctx.send.assert_not_awaited()

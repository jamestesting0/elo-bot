from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.db import BaseLink, BaseStore, Match
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view, request_cancellation
from elo_bot.features.finals.matches import bind_thread, prepare_match
from elo_bot.features.finals.recovery import recover_final
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalGateMatchWrites(FinalUICase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(self.final, "9")
        self.source = await prepare_match(self.final, "QF1")
        self.store = BaseStore(clan_tag="#OWNED", slot="Slot 1", reservation_id="a" * 16, ready=True)
        await self.source.set({"thread": "777", "participants.0.base_store": self.store.model_dump(),
            "participants.0.bases.0": BaseLink(link="fixture-base", image="fixture-image").model_dump()})
        await bind_thread(self.final, self.source, "777")
        self.match_cog = MatchCommands(self.bot)
        self.bot.owner.id = 9

    async def test_cancelled_single_base_remove_keeps_bases_reservations_and_rejects_the_action(self):
        await request_cancellation(await final_for_view(), "Fixture cancellation")
        await recover_final(await final_for_view())
        before = (await Match.get(self.source.id)).model_dump()
        ctx = self.context(user=1)
        ctx.channel.id = 777
        with patch("elo_bot.extensions.match.bases.OverrideCode.is_valid", new=AsyncMock(return_value=False)):
            await self.match_cog.match_base_remove.callback.func(self.match_cog, ctx, player=0,
                                                               district=self.source.final_districts[0])
        current = await Match.get(self.source.id)
        self.assertEqual(current.model_dump(), before)
        self.assertEqual(current.participants[0].base_store, self.store)
        self.assertEqual(ctx.send.await_args.args[0], f"{ERROR_EMOJI} This Final was cancelled.")
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_cancellation_during_base_removal_prevents_the_actual_write(self):
        collection = Match.get_pymongo_collection()
        commit = collection.update_one

        async def cancel_before_commit(query, update, **kwargs):
            await request_cancellation(await final_for_view(), "Fixture cancellation")
            await recover_final(await final_for_view())
            return await commit(query, update, **kwargs)

        ctx = self.context(user=1)
        ctx.channel.id = 777
        with patch("elo_bot.extensions.match.bases.OverrideCode.is_valid", new=AsyncMock(return_value=False)), \
                patch.object(collection, "update_one", new=cancel_before_commit):
            await self.match_cog.match_base_remove.callback.func(self.match_cog, ctx, player=0,
                                                               district=self.source.final_districts[0])
        current = await Match.get(self.source.id)
        self.assertTrue(current.final_superseded)
        self.assertEqual(current.participants[0].bases[0].link, "fixture-base")
        self.assertEqual(current.participants[0].base_store, self.store)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_cancelled_and_superseded_match_commands_leave_every_field_unchanged(self):
        link = "https://link.clashofclans.com/?action=OpenLayout&id=TH15%3ACC%3A1%3AAAAAA"
        actions = [
            ("match_base_remove", {"player": 0, "district": self.source.final_districts[0]}),
            ("match_base_remove_all", {"player": 0, "override_code": "override"}),
            ("match_base_set", {"player": 0, "base_link": link}),
            ("match_base_submit", {"base_link": link}),
            ("match_set_host", {"user": MagicMock(id=8)}),
            ("match_set_clan", {"clan": "#CLAN"}),
            ("match_result_clear", {"player": 0}),
            ("match_result_store", {"player": 0, "district": self.source.final_districts[0],
                                    "stars": 3, "destruction": 100}),
            ("match_result_autostore", {"player": 0}),
            ("match_base_store", {"player": 0, "clan": "#CLAN", "slot": "Slot 1"}),
            ("match_base_autostore", {"player": 0}),
            ("match_abort", {}),
            ("match_schedule", {"timestamp": "1700000000"}),
            ("match_war_schedule", {"player": 0, "timestamp": "1700000000"}),
        ]
        await self.source.set({"final_superseded": True})
        for cancelled in (False, True):
            if cancelled:
                await request_cancellation(await final_for_view(), "Fixture cancellation")
                await recover_final(await final_for_view())
            before = (await Match.get(self.source.id)).model_dump()
            edition = (await final_for_view()).model_dump()
            for name, arguments in actions:
                with self.subTest(cancelled=cancelled, command=name):
                    ctx = self.context()
                    ctx.channel.id = 777
                    await getattr(self.match_cog, name).callback.func(self.match_cog, ctx, **arguments)
                    self.assertEqual((await Match.get(self.source.id)).model_dump(), before)
                    self.assertEqual((await final_for_view()).model_dump(), edition)
                    self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                    if cancelled:
                        self.assertEqual(ctx.send.await_args.args[0], f"{ERROR_EMOJI} This Final was cancelled.")
            self.bot.owner.send.assert_not_called()

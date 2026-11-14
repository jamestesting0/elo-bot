import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elobot.extensions.match import MatchCommands
from elobot.db import Match, Participant
from tests._patch_sites import patch_sites
import elobot.features.matches.resources as resources_module


class TestWarAbort(unittest.IsolatedAsyncioTestCase):
    async def test_abort_started_side_cas_filters_exact_lifecycle_and_message(self) -> None:
        collection = MagicMock()
        collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        match = Match.model_construct(
            token="TOKEN",
            matched_time=1,
            is_war=True,
            times=[1000, 900],
            hosts=["100", "200"],
            host_clans=["#FIRST", "#SECOND"],
            persistent_host_clans=["#PREFERRED", "#SECOND"],
            old_msgs=[777, 888],
            participants=[],
        )
        match.id = PydanticObjectId()
        settings = MagicMock(use_state_management=True, state_management_save_previous=False, keep_nulls=True)

        with patch.object(Match, "get_pymongo_collection", return_value=collection), \
                patch.object(Match, "get_settings", return_value=settings):
            aborted = await match.abort_started_war_side(0, 1000, "100", "#FIRST", 777)

        self.assertTrue(aborted)
        query, update = collection.update_one.await_args.args
        self.assertEqual(query["_id"], match.id)
        self.assertEqual(query["is_war"], True)
        self.assertEqual(query["times.0"], 1000)
        self.assertEqual(query["hosts.0"], "100")
        self.assertEqual(query["host_clans.0"], "#FIRST")
        self.assertEqual(query["old_msgs.0"], 777)
        self.assertNotIn("times.1", query)
        self.assertEqual(
            update["$set"],
            {"times.0": None, "hosts.0": None, "host_clans.0": None},
        )
        self.assertEqual(match.times[1], 900)
        self.assertEqual(match.old_msgs, [777, 888])
        self.assertEqual(match.persistent_host_clans, ["#PREFERRED", "#SECOND"])

    async def test_abort_war_start_cas_miss_has_no_destructive_side_effects(self) -> None:
        from elobot.features.hosting.war.war_start import WarAbort, abort_war_start

        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.channel.edit = AsyncMock()
        message = MagicMock(id=777, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        match = MagicMock(
            times=[1000, None],
            hosts=["100", None],
            host_clans=["#CLAN", None],
            old_msgs=[777, None],
        )
        match.abort_started_war_side = AsyncMock(return_value=False)

        with patch("elobot.features.base_store.storage.clear_participant_base_store", new_callable=AsyncMock) as clear, \
                patch_sites(('elobot.extensions.match.result_store.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free:
            aborted = await abort_war_start(ctx, WarAbort(match=match, player=0, message=message))

        self.assertFalse(aborted)
        clear.assert_not_awaited()
        ctx.channel.edit.assert_not_awaited()
        message.delete.assert_not_awaited()
        message.edit.assert_not_awaited()
        free.assert_not_awaited()

    async def test_start_war_persists_real_state_before_finalizer(self) -> None:
        from elobot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war

        # Given an unstarted war side with an old clan assignment.
        events: list[tuple[str, str | None, str | None]] = []
        match = MagicMock(
            token="TOKEN",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=["#OLD", None],
            persistent_host_clans=["#PREFERRED", None],
            model_fields_set={"persistent_host_clans"},
            old_msgs=[None, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )

        async def persist(_query, _update):
            events.append(("write", match.hosts[0], match.host_clans[0]))
            return MagicMock(matched_count=1)

        match.get_pymongo_collection.return_value.update_one = AsyncMock(side_effect=persist)
        host = MagicMock(id=100)
        clan = MagicMock(tag="#AVAILABLE")
        ctx = MagicMock(spec=ipy.SlashContext)
        message = MagicMock(id=777)

        async def record_finalize(*_args):
            events.append(("finalize", match.hosts[0], match.host_clans[0]))
            return message

        with (
                patch_sites(('elobot.features.base_store.match_adapters.Match.find_one', 'elobot.extensions.match.lifecycle.Match.find_one', 'elobot.extensions.match.bases.Match.find_one', 'elobot.extensions.match.base_store.Match.find_one', 'elobot.extensions.match.results.Match.find_one', 'elobot.extensions.match.result_store.Match.find_one', 'elobot.extensions.match.hosting.Match.find_one', 'elobot.extensions.match.settings.Match.find_one', 'elobot.extensions.match.dashboards.Match.find_one', 'elobot.features.queue.state.Match.find_one', 'elobot.features.matches.occupancy.Match.find_one', 'elobot.features.matches.resources.Match.find_one', 'elobot.features.matches.lifecycle.Match.find_one', 'elobot.features.matches.attack_bases.Match.find_one', 'elobot.features.results.recording.Match.find_one'), new=AsyncMock(
                    return_value=match,
                )),
                patch_sites(('elobot.extensions.match.dashboards.Clan.find_one', 'elobot.features.matches.resources.Clan.find_one'), new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"),
                )),
                patch_sites(('elobot.extensions.match.result_store.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free,
                patch('elobot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy,
                patch("elobot.features.hosting.war.war_start.complete_war_start", new=AsyncMock(side_effect=record_finalize)) as finalize,
        ):
            # When the war side starts.
            result = await start_war(
                MagicMock(),
                ctx,
                WarStart(
                    match=match,
                    player=0,
                    host=host,
                    clan=clan,
                    coc_client=MagicMock(),
                    reply=SelfHostedWarStartReply(),
                ),
            )

        # Then clan occupancy is updated after persistence and before finalization.
        self.assertIs(result, message)
        self.assertEqual(
            events,
            [
                ("write", None, "#OLD"),
                ("finalize", "100", "#AVAILABLE"),
            ],
        )
        update = match.get_pymongo_collection.return_value.update_one.await_args.args[1]["$set"]
        self.assertEqual(update["hosts.0"], "100")
        self.assertEqual(update["host_clans.0"], "#AVAILABLE")
        self.assertEqual(update["persistent_host_clans.0"], "#AVAILABLE")
        self.assertIsInstance(update["times.0"], int)
        self.assertEqual(match.hosts[0], "100")
        self.assertEqual(match.host_clans[0], "#AVAILABLE")
        self.assertEqual(match.persistent_host_clans[0], "#AVAILABLE")
        free.assert_awaited_once_with("#OLD", expected_thread=str(match.thread))
        occupy.assert_awaited_once_with(match, 0)
        self.assertEqual(finalize.await_args.args[2].clan.tag, "#AVAILABLE")

    async def test_start_war_persistence_failure_skips_occupancy_and_finalizer(self) -> None:
        from elobot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war

        # Given an unstarted war side whose durable state write fails.
        match = MagicMock(
            token="TOKEN",
            is_war=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=["#OLD", None],
            persistent_host_clans=["#PREFERRED", None],
            model_fields_set={"persistent_host_clans"},
            old_msgs=[None, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        match.get_pymongo_collection.return_value.update_one = AsyncMock(side_effect=RuntimeError("match write failed"))
        start = WarStart(
            match=match,
            player=0,
            host=MagicMock(id=100),
            clan=MagicMock(tag="#AVAILABLE"),
            coc_client=MagicMock(),
            reply=SelfHostedWarStartReply(),
        )

        with (
                patch_sites(('elobot.features.base_store.match_adapters.Match.find_one', 'elobot.extensions.match.lifecycle.Match.find_one', 'elobot.extensions.match.bases.Match.find_one', 'elobot.extensions.match.base_store.Match.find_one', 'elobot.extensions.match.results.Match.find_one', 'elobot.extensions.match.result_store.Match.find_one', 'elobot.extensions.match.hosting.Match.find_one', 'elobot.extensions.match.settings.Match.find_one', 'elobot.extensions.match.dashboards.Match.find_one', 'elobot.features.queue.state.Match.find_one', 'elobot.features.matches.occupancy.Match.find_one', 'elobot.features.matches.resources.Match.find_one', 'elobot.features.matches.lifecycle.Match.find_one', 'elobot.features.matches.attack_bases.Match.find_one', 'elobot.features.results.recording.Match.find_one'), new=AsyncMock(
                    return_value=match,
                )),
                patch_sites(('elobot.extensions.match.dashboards.Clan.find_one', 'elobot.features.matches.resources.Clan.find_one'), new=AsyncMock(
                    return_value=MagicMock(availability_str="Available"),
                )),
                patch_sites(('elobot.extensions.match.result_store.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free,
                patch('elobot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy,
                patch("elobot.features.hosting.war.war_start.complete_war_start", new_callable=AsyncMock) as finalize,
        ):
            # When the initial match write fails, then no occupancy or finalization follows.
            with self.assertRaisesRegex(RuntimeError, "match write failed"):
                await start_war(MagicMock(), MagicMock(spec=ipy.SlashContext), start)

        self.assertFalse(match.is_war)
        self.assertIsNone(match.times[0])
        self.assertIsNone(match.hosts[0])
        self.assertEqual(match.host_clans[0], "#OLD")
        self.assertEqual(match.persistent_host_clans[0], "#PREFERRED")
        match.get_pymongo_collection.return_value.update_one.assert_awaited_once()
        free.assert_not_awaited()
        occupy.assert_not_awaited()
        finalize.assert_not_awaited()

    async def test_start_war_returns_none_when_resource_clan_claim_is_unavailable(self) -> None:
        from elobot.features.hosting.war.war_start import SelfHostedWarStartReply, WarStart, start_war

        # Given a war start whose selected clan became unavailable before its claim.
        match = MagicMock(
            token="TOKEN",
            thread="101",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            old_msgs=[None, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        match.set = AsyncMock()
        start = WarStart(
            match=match,
            player=0,
            host=MagicMock(id=100),
            clan=MagicMock(tag="#BUSY"),
            coc_client=MagicMock(),
            reply=SelfHostedWarStartReply(),
        )

        with (
                patch_sites(('elobot.features.base_store.match_adapters.Match.find_one', 'elobot.extensions.match.lifecycle.Match.find_one', 'elobot.extensions.match.bases.Match.find_one', 'elobot.extensions.match.base_store.Match.find_one', 'elobot.extensions.match.results.Match.find_one', 'elobot.extensions.match.result_store.Match.find_one', 'elobot.extensions.match.hosting.Match.find_one', 'elobot.extensions.match.settings.Match.find_one', 'elobot.extensions.match.dashboards.Match.find_one', 'elobot.features.queue.state.Match.find_one', 'elobot.features.matches.occupancy.Match.find_one', 'elobot.features.matches.resources.Match.find_one', 'elobot.features.matches.lifecycle.Match.find_one', 'elobot.features.matches.attack_bases.Match.find_one', 'elobot.features.results.recording.Match.find_one'), new=AsyncMock(
                    return_value=match,
                )),
                patch_sites(('elobot.extensions.match.dashboards.Clan.find_one', 'elobot.features.matches.resources.Clan.find_one'), new=AsyncMock(
                    return_value=MagicMock(availability_str="Occupied"),
                )),
                patch("elobot.features.hosting.war.war_start.complete_war_start", new_callable=AsyncMock) as finalize,
        ):
            # When the start reaches the ordinary claim-failure outcome.
            result = await start_war(MagicMock(), MagicMock(spec=ipy.SlashContext), start)

        # Then the caller receives None and no UI finalization follows.
        self.assertIsNone(result)
        match.set.assert_not_awaited()
        finalize.assert_not_awaited()
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#BUSY"].locked())
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("TOKEN", 0)].locked())

    @patch("elobot.features.hosting.war.war_start.abort_war_start", new_callable=AsyncMock)
    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_abort_match_button_delegates_to_shared_war_abort(self, mock_match, abort) -> None:
        bot = MagicMock()
        cog = MatchCommands(bot)
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = "match_abort|TOKEN|0"
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**")]
        match = MagicMock(
            is_autowin=False,
            hosts=["999", None],
            participants=[MagicMock(user_id="100", result=[None] * 3), MagicMock(user_id="200", result=[None] * 3)],
        )
        mock_match.find_one = AsyncMock(return_value=match)
        abort.return_value = True

        await cog.abort_match_button.callback(ctx)

        abort.assert_awaited_once()
        abort_ctx, request = abort.await_args.args
        self.assertIs(abort_ctx, ctx)
        self.assertIs(request.match, match)
        self.assertEqual(request.player, 0)
        self.assertIs(request.message, ctx.message)

if __name__ == "__main__":
    unittest.main()

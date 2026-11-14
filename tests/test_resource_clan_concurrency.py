import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import elobot.extensions.match as match_commands
from elobot.extensions.match import MatchCommands


class TestResourceClanConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands._RESOURCE_CLAN_RESERVATIONS.set({})
        match_commands._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})

    def reservation_api(self):
        reserve = getattr(match_commands, "try_reserve_resource_clan", None)
        self.assertIsNotNone(reserve, "process-wide per-clan reservation API is absent")
        return reserve

    @staticmethod
    def component_context(custom_id: str) -> MagicMock:
        ctx = MagicMock()
        ctx.custom_id = custom_id
        ctx.values = ["#A"]
        ctx.author.id = 999
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.message.edit = AsyncMock()
        ctx.message.embeds = [MagicMock(title="**Match Started**", fields=[MagicMock(), MagicMock(), MagicMock()])]
        return ctx

    async def test_locked_clan_is_unavailable_while_different_clan_remains_independent(self) -> None:
        # Given a reservation held for clan A.
        reserve = self.reservation_api()
        first = await reserve("#A")
        self.assertIsNotNone(first)

        try:
            # When callers try clan A and unrelated clan B.
            duplicate = await reserve("#A")
            independent = await reserve("#B")

            # Then A is rejected immediately while B remains independently usable.
            self.assertIsNone(duplicate)
            self.assertIsNotNone(independent)
            independent.release()
        finally:
            first.release()

    async def test_stale_release_does_not_free_newer_match_owner(self) -> None:
        # Given a clan whose durable availability belongs to a newer thread.
        clan = SimpleNamespace(availability_str="<#202> <@2> now", update=AsyncMock())

        with patch.object(match_commands.Clan, "find_one", new=AsyncMock(return_value=clan)):
            # When cleanup for the older thread tries to free it.
            released = await match_commands.free_clan("#A", expected_thread="101")

        # Then the newer owner remains untouched and the tag lock is released.
        self.assertFalse(released)
        clan.update.assert_not_awaited()
        lock = match_commands.RESOURCE_CLAN_LOCKS["#A"]
        self.assertFalse(lock.locked())

    async def test_claim_exception_and_early_release_do_not_leak_tag_lock(self) -> None:
        # Given a reserved clan and a durable match write that raises.
        reserve = self.reservation_api()
        claim = getattr(match_commands, "claim_resource_clan", None)
        self.assertIsNotNone(claim, "locked durable resource-clan claim API is absent")
        reservation = await reserve("#A")
        clan = SimpleNamespace(tag="#A", availability_str="Available", update=AsyncMock())
        match = MagicMock(
            token="MATCH",
            thread="101",
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            participants=[MagicMock(user_id="1"), MagicMock()],
        )
        match.set = AsyncMock(side_effect=RuntimeError("write failed"))

        with patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)), \
                patch.object(match_commands.Clan, "find_one", new=AsyncMock(return_value=clan)):
            # When the locked claim raises during its durable update.
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                await claim(reservation, match, 0, updates={"is_war": True})

        # Then both exception cleanup and a separate early release leave the lock reusable.
        self.assertFalse(match_commands.RESOURCE_CLAN_LOCKS["#A"].locked())
        self.assertFalse(match_commands.RESOURCE_MATCH_SIDE_LOCKS[("MATCH", 0)].locked())
        early = await reserve("#A")
        early.release()
        self.assertFalse(match_commands.RESOURCE_CLAN_LOCKS["#A"].locked())

    async def test_locked_host_clan_select_and_match_set_clan_cannot_overwrite(self) -> None:
        # Given clan A reserved by another allocation while its document still says Available.
        reservation = await self.reservation_api()("#A")
        clan_a = SimpleNamespace(tag="#A", name="Alpha", availability_str="Available")
        clan_b = SimpleNamespace(tag="#B", name="Bravo", availability_str="Available")
        match = MagicMock(
            token="TOKEN",
            thread="101",
            is_war=False,
            is_autowin=False,
            times=[100, None],
            hosts=["999", None],
            host_clans=["#OLD", None],
            old_msgs=[123, None],
            participants=[MagicMock(result=[]), MagicMock(result=[])],
        )
        match.set = AsyncMock()
        bot = MagicMock()
        bot.coc_client.get_clan = AsyncMock(return_value=clan_a)
        cog = MatchCommands(bot)
        select_ctx = self.component_context("host_clan_select|TOKEN|0")
        select_ctx.message.id = 123
        set_ctx = self.component_context("unused")
        set_ctx.channel.id = 101

        try:
            with patch.object(match_commands, "Match") as match_model, \
                    patch.object(match_commands, "Clan") as clan_model:
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When both previously unguarded selectors target locked clan A.
                await cog.host_clan_select.callback.func(cog, select_ctx)
                await cog.match_set_clan.callback.func(cog, set_ctx, clan="#A")
        finally:
            reservation.release()

        # Then neither selector overwrites the match and both list only unlocked clan B.
        match.set.assert_not_awaited()
        for ctx in (select_ctx, set_ctx):
            response = ctx.send.await_args.args[0]
            self.assertIn("Available clans:", response)
            self.assertIn("- Bravo (#B)", response)
            self.assertNotIn("- Alpha (#A)", response)

    async def test_locked_explicit_war_start_rejects_with_unlocked_alternatives(self) -> None:
        # Given an explicit automatic start targeting locked clan A with unlocked clan B available.
        reservation = await self.reservation_api()("#A")
        clan_a = SimpleNamespace(tag="#A", name="Alpha", availability_str="Available")
        clan_b = SimpleNamespace(tag="#B", name="Bravo", availability_str="Available")
        match = MagicMock(
            thread="101",
            is_war=True,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            participants=[MagicMock(user_id="1", result=[]), MagicMock(user_id="2", result=[])],
        )
        bot = MagicMock()
        bot.owner.id = 123
        bot.user.id = 0
        bot.coc_client.get_clan = AsyncMock(return_value=clan_a)
        cog = MatchCommands(bot)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 101
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )

        try:
            with patch.object(match_commands, "Match") as match_model, \
                    patch.object(match_commands, "Clan") as clan_model, \
                    patch.object(match_commands, "is_raid_weekend", return_value=False), \
                    patch.object(match_commands, "is_clan_available", new=AsyncMock(return_value=False)), \
                    patch.object(match_commands.helper_host, "host_war_automatically", new_callable=AsyncMock) as host:
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When the explicit start callback tries to allocate clan A.
                await cog.match_war_autostart.callback.func(cog, ctx, player=0, clan="#A")
        finally:
            reservation.release()

        # Then it rejects before Helper and reports only unlocked clan B.
        host.assert_not_awaited()
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Bravo (#B)", response)
        self.assertNotIn("- Alpha (#A)", response)

    async def test_locked_explicit_live_start_rejects_before_base_slot_checks(self) -> None:
        # Given a valid live start targeting locked clan A with unlocked clan B available.
        reservation = await self.reservation_api()("#A")
        clan_a = SimpleNamespace(tag="#A", name="Alpha", availability_str="Available")
        clan_b = SimpleNamespace(tag="#B", name="Bravo", availability_str="Available")
        match = MagicMock(
            thread="101",
            is_war=False,
            is_autowin=False,
            times=[None, None],
            participants=[MagicMock(user_id="1", result=[]), MagicMock(user_id="2", result=[])],
        )
        bot = MagicMock()
        bot.owner.id = 123
        bot.user.id = 0
        bot.coc_client.get_player = AsyncMock(side_effect=[MagicMock(town_hall=3), MagicMock(town_hall=3)])
        bot.coc_client.get_clan = AsyncMock(return_value=clan_a)
        cog = MatchCommands(bot)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 101

        try:
            with patch.object(match_commands, "Match") as match_model, \
                    patch.object(match_commands, "Player") as player_model, \
                    patch.object(match_commands, "Clan") as clan_model, \
                    patch.object(match_commands, "is_raid_weekend", return_value=False), \
                    patch.object(
                        match_commands,
                        "get_start_base_store_slot_availability",
                        new_callable=AsyncMock,
                    ) as slots:
                match_model.find_one = AsyncMock(return_value=match)
                player_model.find_one = AsyncMock(side_effect=[MagicMock(tag="#P1"), MagicMock(tag="#P2")])
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When the explicit live-start callback tries to allocate clan A.
                await cog.match_start.callback.func(cog, ctx, clan="#A")
        finally:
            reservation.release()

        # Then the whole-clan rejection wins without changing base-slot error semantics.
        slots.assert_not_awaited()
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Bravo (#B)", response)
        self.assertNotIn("- Alpha (#A)", response)


if __name__ == "__main__":
    unittest.main()

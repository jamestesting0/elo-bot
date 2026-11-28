from hashlib import sha256
import time
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.ids import FINAL_CHANNEL, MAIN_GUILD, STAFF_CHANNEL
from elo_bot.core.finals import FINAL_PICK_WINDOW
from elo_bot.db import EloFinal
from elo_bot.db.final_invitation_models import FinalInvitation
from elo_bot.features.finals.bracket import automatic_pairs
from elo_bot.features.finals.invitations import accept_invitation, invite_players
from elo_bot.features.finals.lifecycle import (
    add_entrant, confirm_lineup, remove_entrant, reset_bracket, save_pairing_draft, set_nickname, start_final,
)
from elo_bot.features.finals.opponent_picks import pick_prompt_key, pick_turn, save_pick
from elo_bot.features.finals.registration_posts import lineup_embed, registration_posts_pending, sync_registration_posts
from tests.integration._final_ui_fixture import FinalUICase


class TestFinalOpponentPicks(FinalUICase):
    async def fill_roster(self):
        await self.final.delete()
        self.final = await start_final(MAIN_GUILD, FINAL_CHANNEL)
        self.channel.__class__ = ipy.GuildText
        self.dm = MagicMock(spec=ipy.DM, id=78, send=self.channel.send, fetch_message=self.channel.fetch_message,
                            fetch_messages=self.channel.fetch_messages)
        self.bot.cache.fetch_dm_channel = AsyncMock(return_value=self.dm)
        self.bot.fetch_channel.side_effect = lambda channel_id: self.dm if channel_id == self.dm.id else self.channel
        for user_id in range(1, 8):
            self.final = await add_entrant(self.final, str(user_id), f"Player {user_id}")
        await sync_registration_posts(self.bot, self.final)
        self.channel.send.assert_not_awaited()

        entrant = MagicMock(id=8, display_name="Player 8", send=AsyncMock(return_value=MagicMock(id=89)))
        entrant.has_role.return_value = False
        await invite_players(self.final, {"8": entrant}, "9")
        invitation = await FinalInvitation.find_one({"user_id": "8"})
        answer = await accept_invitation(invitation.id, entrant)
        self.assertEqual((answer.state, answer.entrants), ("accepted", 8))
        self.final = await EloFinal.get(self.final.id)

    async def choose(self, user, opponent):
        latest = await EloFinal.get(self.final.id)
        index = pick_turn(latest)
        prompt = self.posts[self.prompt_message(latest, index).dm_message_id]
        select = self.context(user, manager=False, component=True)
        select.custom_id = prompt.payload["components"][0].components[0].custom_id
        select.values = [opponent]
        await self.cog.final_opponent_pick.callback.func(self.cog, select)
        confirm = self.context(user, manager=False, component=True)
        confirm.custom_id = select.edit_origin.await_args.kwargs["components"][1].components[0].custom_id
        await self.cog.final_opponent_pick.callback.func(self.cog, confirm)
        self.assertIn("Opponent", confirm.edit_origin.await_args.kwargs["embed"].description)
        self.assertEqual(confirm.edit_origin.await_args.kwargs["components"], [])
        return confirm.custom_id

    def prompt_message(self, final, index):
        return final.opponent_pick_prompts[pick_prompt_key(final.opponent_picks.generation, index)]

    async def test_full_roster_waits_for_manager_to_confirm_before_publishing_or_opening_bracket(self):
        await self.fill_roster()
        await set_nickname(self.final, "2", "Final nickname")
        await self.cog.recover_pending_finals.callback.func(self.cog)
        self.channel.send.assert_not_awaited()
        self.bot.cache.fetch_dm_channel.assert_not_awaited()
        saved = await EloFinal.get(self.final.id)
        self.assertIsNone(pick_turn(saved))
        bracket = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, bracket)
        self.assertIn("Confirm the lineup", bracket.send.await_args.args[0])
        self.assertIsNone((await EloFinal.get(saved.id)).draft_owner)

        dashboard = self.context()
        await self.cog.final_dashboard.callback.func(self.cog, dashboard)
        buttons = dashboard.send.await_args.kwargs["components"][0].components
        self.assertEqual([button.label for button in buttons if button.label], ["Confirm Lineup"])
        confirm = self.context(component=True)
        confirm.custom_id = buttons[0].custom_id
        confirm.guild.fetch_member.side_effect = lambda user: MagicMock(id=user, display_name=f"Updated Player {user}")
        now = int(time.time())
        with patch("elo_bot.features.finals.lifecycle.time", MagicMock(time=lambda: now)):
            await self.cog.final_dashboard_action.callback.func(self.cog, confirm)
        saved = await EloFinal.get(saved.id)
        self.assertEqual(saved.opponent_picks.turns[0].deadline, now + FINAL_PICK_WINDOW)
        self.assertIsNotNone(saved.lineup_message_id)
        lineup = self.posts[saved.lineup_message_id].payload["embed"]
        self.assertIn("**#1** **[Updated Player 1](<https://discordapp.com/users/1>)**", lineup.description.splitlines())
        self.assertIn("**#2** **[Final nickname](<https://discordapp.com/users/2>)**", lineup.description.splitlines())
        self.assertIsNotNone(self.prompt_message(saved, 0).dm_message_id)
        buttons = confirm.edit_origin.await_args.kwargs["components"][0].components
        self.assertEqual([button.label for button in buttons if button.label], ["Create Bracket"])
        self.assertEqual(self.channel.send.await_count, 2)

        duplicate = self.context(component=True)
        duplicate.custom_id = confirm.custom_id
        await self.cog.final_dashboard_action.callback.func(self.cog, duplicate)
        self.assertEqual(self.channel.send.await_count, 2)
        self.assertEqual((await EloFinal.get(saved.id)).opponent_picks, saved.opponent_picks)

    async def test_roster_change_during_publication_refreshes_the_confirmation_receipt(self):
        await self.fill_roster()
        dashboard = self.context()
        await self.cog.final_dashboard.callback.func(self.cog, dashboard)
        confirm = self.context(component=True)
        confirm.custom_id = dashboard.send.await_args.kwargs["components"][0].components[0].custom_id

        async def remove_before_publication(bot, confirmed):
            await remove_entrant(confirmed, "8")
            return await sync_registration_posts(bot, confirmed)

        with patch("elo_bot.extensions.final.dashboard.sync_registration_posts", side_effect=remove_before_publication):
            await self.cog.final_dashboard_action.callback.func(self.cog, confirm)
        payload = confirm.edit_origin.await_args.kwargs
        self.assertIn("changed", payload["content"])
        self.assertEqual(payload["components"][0].components[0].label, "Confirm Lineup")
        self.channel.send.assert_not_awaited()

    async def test_stale_confirmation_cannot_approve_a_changed_roster_and_short_lineup_keeps_byes(self):
        await self.fill_roster()
        dashboard = self.context()
        await self.cog.final_dashboard.callback.func(self.cog, dashboard)
        stale = self.context(component=True)
        stale.custom_id = dashboard.send.await_args.kwargs["components"][0].components[0].custom_id
        await remove_entrant(self.final, "8")
        await remove_entrant(await EloFinal.get(self.final.id), "7")
        await self.cog.final_dashboard_action.callback.func(self.cog, stale)
        self.channel.send.assert_not_awaited()
        self.assertIn("changed", stale.edit_origin.await_args.kwargs["content"])

        buttons = stale.edit_origin.await_args.kwargs["components"][0].components
        self.assertEqual([button.label for button in buttons if button.label], ["Confirm Lineup", "Add", "Invite"])
        confirm = self.context(component=True)
        confirm.custom_id = buttons[0].custom_id
        await self.cog.final_dashboard_action.callback.func(self.cog, confirm)
        saved = await EloFinal.get(self.final.id)
        self.assertEqual(self.channel.send.await_count, 1)
        self.bot.cache.fetch_dm_channel.assert_not_awaited()
        self.assertEqual(automatic_pairs(saved)["QF1"], ["1", None])
        bracket = self.context(component=True)
        bracket.custom_id = confirm.edit_origin.await_args.kwargs["components"][0].components[0].custom_id
        await self.cog.final_dashboard_action.callback.func(self.cog, bracket)
        self.assertIsNotNone((await EloFinal.get(saved.id)).draft_owner)

        member = MagicMock(id=7, display_name="Player 7", send=AsyncMock(return_value=MagicMock(id=89)))
        member.has_role.return_value = False
        await invite_players(await EloFinal.get(saved.id), {"7": member}, "9")
        invitation = await FinalInvitation.find_one({"user_id": "7"})
        self.bot.fetch_guild = AsyncMock(return_value=MagicMock(fetch_member=AsyncMock(return_value=member)))
        fetch_channel = self.bot.fetch_channel.side_effect
        staff = MagicMock(send=AsyncMock())
        self.bot.fetch_channel.side_effect = lambda channel_id: (
            staff if channel_id == STAFF_CHANNEL else fetch_channel(channel_id))
        accept = self.context(user=7, manager=False, component=True)
        accept.custom_id = f"finalinvite|accept|{invitation.id}"
        await self.cog.final_invitation_answer.callback.func(self.cog, accept)
        refilled = await EloFinal.get(saved.id)
        self.assertEqual(len(refilled.entrant_ids), 7)
        self.assertEqual(refilled.lineup_message_id, saved.lineup_message_id)
        self.assertEqual(self.posts[saved.lineup_message_id].edit.await_args.kwargs["embed"].description,
                         "Lineup pending confirmation.")

    async def test_picks_advance_in_order_and_timeout_randomly_assigns_only_an_available_opponent(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        await sync_registration_posts(self.bot, self.final)
        initial = await EloFinal.get(self.final.id)
        self.assertEqual(pick_turn(initial), 0)
        lineup = self.posts[initial.lineup_message_id]
        expected = [f"**#{seed}** **[Player {seed}](<https://discordapp.com/users/{seed}>)**"
                    for seed in range(1, 9)]
        self.assertEqual(lineup.payload["embed"].to_dict()["description"].splitlines(), expected)
        old_description = "\n".join(f"**#{seed}** [Player {seed}](<https://discordapp.com/users/{seed}>)"
                                    for seed in range(1, 9))
        lineup.payload["embed"].description = old_description
        await initial.set({"lineup_signature": sha256(old_description.encode()).hexdigest()})
        await sync_registration_posts(self.bot, initial)
        self.assertEqual(lineup.edit.await_args.kwargs["embed"].description.splitlines(), expected)
        self.assertEqual((await EloFinal.get(initial.id)).lineup_message_id, initial.lineup_message_id)
        first = self.posts[self.prompt_message(initial, 0).dm_message_id]
        self.assertEqual([opt.value for opt in first.payload["components"][0].components[0].options],
                         ["5", "6", "7", "8"])
        foreign = self.context(2, manager=False, component=True)
        foreign.custom_id = first.payload["components"][0].components[0].custom_id
        foreign.values = ["5"]
        await self.cog.final_opponent_pick.callback.func(self.cog, foreign)
        self.assertIn("another player", foreign.send.await_args.args[0])
        old_confirm = await self.choose(1, "5")
        current = await EloFinal.get(self.final.id)
        self.assertEqual(pick_turn(current), 1)
        self.assertEqual(current.opponent_picks.turns[0].opponent, "5")
        late = self.context(1, manager=False, component=True)
        late.custom_id = old_confirm
        await self.cog.final_opponent_pick.callback.func(self.cog, late)
        self.assertIn("closed", late.send.await_args.args[0])

        await EloFinal.get_pymongo_collection().update_one({"_id": current.id},
            {"$unset": {"opponent_picks.confirmed": ""}})
        current = await EloFinal.get(self.final.id)
        self.assertEqual(pick_turn(current), 1)
        deadline = current.opponent_picks.turns[1].deadline
        self.assertFalse(await save_pick(current, 1, deadline, "6"))
        with (patch("elo_bot.features.finals.registration_posts.time", MagicMock(time=lambda: deadline)),
              patch("elo_bot.features.finals.opponent_picks.choice", return_value="7") as random_pick):
            await sync_registration_posts(self.bot, current)
        random_pick.assert_called_once_with(["6", "7", "8"])
        current = await EloFinal.get(self.final.id)
        self.assertTrue(current.opponent_picks.turns[1].random)
        self.assertEqual(current.opponent_picks.turns[2].deadline, deadline + FINAL_PICK_WINDOW)
        timed_out = self.posts[self.prompt_message(current, 1).dm_message_id]
        self.assertIn("Assigned", timed_out.edit.await_args.kwargs["embed"].description)
        await self.choose(3, "6")
        completed = await EloFinal.get(self.final.id)
        self.assertIsNone(pick_turn(completed))
        self.assertEqual(automatic_pairs(completed), {"QF1": ["1", "5"], "QF2": ["4", "8"],
                                                      "QF3": ["2", "7"], "QF4": ["3", "6"]})
        await sync_registration_posts(self.bot, completed)
        self.assertEqual(self.channel.send.await_count, 4)

    async def test_manager_can_confirm_early_and_a_new_player_pick_invalidates_an_open_editor(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        await sync_registration_posts(self.bot, self.final)
        opener = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, opener)
        old = self.context(component=True)
        old.custom_id = opener.send.await_args.kwargs["components"][1].components[1].custom_id
        await self.choose(1, "5")
        await self.cog.final_pairing_action.callback.func(self.cog, old)
        self.assertIn("outdated", old.send.await_args.args[0])

        opener = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, opener)
        self.assertIn("close opponent picks", opener.send.await_args.kwargs["embed"].description)
        confirm = self.context(component=True)
        confirm.custom_id = opener.send.await_args.kwargs["components"][1].components[1].custom_id
        fetch_channel = self.bot.fetch_channel.side_effect
        self.bot.fetch_channel.side_effect = lambda channel_id: self.dm if channel_id == self.dm.id else None
        await self.cog.final_pairing_action.callback.func(self.cog, confirm)
        final = await EloFinal.get(self.final.id)
        self.assertEqual(final.state, "BRACKET_READY")
        self.assertEqual(final.nodes["QF1"].contestants, ["1", "5"])
        self.assertTrue(final.publication_pending)
        self.assertIn("Bracket confirmed", confirm.edit_origin.await_args.kwargs["content"])
        self.assertIsNone(pick_turn(final))
        closed = self.posts[self.prompt_message(final, 1).dm_message_id]
        self.assertEqual(closed.edit.await_args.kwargs["components"], [])
        self.assertIn("closed", closed.edit.await_args.kwargs["content"])
        self.bot.fetch_channel.side_effect = fetch_channel
        await reset_bracket(final)
        reset = await EloFinal.get(self.final.id)
        self.assertIsNone(pick_turn(reset))
        self.assertEqual(automatic_pairs(reset)["QF1"], ["1", "5"])
        old_dm = self.context(2, manager=False, component=True)
        old_dm.custom_id = closed.payload["components"][0].components[0].custom_id
        old_dm.values = ["6"]
        await self.cog.final_opponent_pick.callback.func(self.cog, old_dm)
        self.assertIn("closed", old_dm.send.await_args.args[0])

    async def test_failed_lineup_retries_and_closed_dms_leave_the_pick_to_timeout(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        fetch_channel = self.bot.fetch_channel.side_effect
        self.bot.fetch_channel.side_effect = lambda _: None
        self.bot.cache.fetch_dm_channel.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        self.assertEqual(await sync_registration_posts(self.bot, self.final), ["Lineup update pending."])
        saved = await EloFinal.get(self.final.id)
        self.assertEqual(len(saved.entrant_ids), 8)
        self.assertTrue(self.prompt_message(saved, 0).dm_unavailable)
        self.assertIsNone(saved.lineup_message_id)

        self.bot.fetch_channel.side_effect = fetch_channel
        await sync_registration_posts(self.bot, saved)
        saved = await EloFinal.get(self.final.id)
        dashboard = self.context(1, manager=False)
        await self.cog.final_dashboard.callback.func(self.cog, dashboard)
        self.assertEqual(dashboard.send.await_args.kwargs["components"], [])
        dashboard = self.context(1)
        await self.cog.final_dashboard.callback.func(self.cog, dashboard)
        rows = dashboard.send.await_args.kwargs["components"]
        self.assertEqual([[button.label for button in row.components] for row in rows], [["Create Bracket", None]])

        deadline = saved.opponent_picks.turns[0].deadline
        with (patch("elo_bot.features.finals.registration_posts.time", MagicMock(time=lambda: deadline)),
              patch("elo_bot.features.finals.opponent_picks.choice", return_value="8")):
            await sync_registration_posts(self.bot, saved)
        saved = await EloFinal.get(self.final.id)
        self.assertEqual(saved.opponent_picks.turns[0].opponent, "8")
        self.assertTrue(saved.opponent_picks.turns[0].random)

        await remove_entrant(saved, "8")
        await sync_registration_posts(self.bot, saved)
        changed = await EloFinal.get(self.final.id)
        self.assertEqual(changed.lineup_message_id, saved.lineup_message_id)
        self.assertEqual(self.posts[saved.lineup_message_id].edit.await_args.kwargs["embed"].description,
                         "Lineup pending confirmation.")
        self.assertIsNone(pick_turn(changed))

    async def test_refilling_a_seat_preserves_cleanup_for_the_old_dm_and_updates_the_same_lineup(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        await sync_registration_posts(self.bot, self.final)
        saved = await EloFinal.get(self.final.id)
        saved = await save_pairing_draft(saved, "8", automatic_pairs(saved))
        old_dm = self.posts[self.prompt_message(saved, 0).dm_message_id]
        old_lineup_id = saved.lineup_message_id
        old_dm.nonce = None
        self.posts[old_lineup_id].nonce = None
        old_embed = self.posts[old_lineup_id].embeds[0]
        old_embed.title = "**Season 7 Elo Final · Lineup**"
        old_embed.author = None
        different_edition = lineup_embed(saved.model_copy(update={"id": PydanticObjectId()}))
        other_lineup = MagicMock(id=999, nonce=None, embeds=[different_edition], components=[], edit=AsyncMock())
        other_lineup.author.id = self.bot.user.id
        self.channel.fetch_messages.side_effect = lambda **_: [other_lineup, *self.posts.values()]
        key = pick_prompt_key(saved.opponent_picks.generation, 0)
        await saved.set({"lineup_message_id": None, f"opponent_pick_prompts.{key}.dm_message_id": None})
        await remove_entrant(saved, "8")
        changed = await EloFinal.get(saved.id)
        refilled = await add_entrant(changed, "9", "Player [9] | The Ninth")
        self.bot.cache.fetch_dm_channel.reset_mock()
        self.assertEqual(await sync_registration_posts(self.bot, refilled), [])
        self.bot.cache.fetch_dm_channel.assert_not_awaited()
        updated = self.posts[old_lineup_id].edit.await_args.kwargs["embed"]
        self.assertEqual(updated.description, "Lineup pending confirmation.")
        self.assertEqual(updated.title, "**Lineup**")
        self.assertEqual(updated.author.name, "Season 7 Final")
        refilled = await confirm_lineup(await EloFinal.get(saved.id))
        self.bot.cache.fetch_dm_channel.side_effect = ipy.errors.HTTPException(MagicMock(status=503))
        self.assertEqual(await sync_registration_posts(self.bot, refilled), ["Opponent pick message update pending."])
        self.assertEqual(old_dm.edit.await_args.kwargs["components"], [])
        self.bot.cache.fetch_dm_channel.side_effect = None
        await sync_registration_posts(self.bot, refilled)
        current = await EloFinal.get(saved.id)
        self.assertNotEqual(current.opponent_picks.generation, saved.opponent_picks.generation)
        self.assertEqual(current.lineup_message_id, old_lineup_id)
        other_lineup.edit.assert_not_awaited()
        lineup = self.posts[current.lineup_message_id]
        self.assertIn(r"**#8** **[Player \[9\] | The Ninth](<https://discordapp.com/users/9>)**",
                      lineup.edit.await_args.kwargs["embed"].description.splitlines())
        self.assertEqual(old_dm.edit.await_args.kwargs["components"], [])
        self.assertNotEqual(self.prompt_message(current, 0).dm_message_id, str(old_dm.id))
        editor = self.context()
        await self.cog.final_bracket_create.callback.func(self.cog, editor)
        self.assertEqual((await EloFinal.get(current.id)).draft_owner, "9")

    async def test_inactive_final_remains_eligible_for_recovering_a_failed_lineup_edit(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        await sync_registration_posts(self.bot, self.final)
        saved = await EloFinal.get(self.final.id)
        lineup = self.posts[saved.lineup_message_id]
        await saved.set({"active": False, "state": "CANCELLED", "publication_pending": False})
        lineup.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        self.assertEqual(await sync_registration_posts(self.bot, saved), ["Lineup update pending."])
        pending = await EloFinal.get(saved.id)
        self.assertTrue(registration_posts_pending(pending))
        lineup.edit.side_effect = None
        await self.cog.recover_pending_finals.callback.func(self.cog)
        self.assertIn("Final cancelled.", lineup.edit.await_args.kwargs["embed"].description)
        self.assertFalse(registration_posts_pending(await EloFinal.get(saved.id)))

    async def test_forbidden_sdk_channel_fallback_keeps_lineup_and_dm_updates_recoverable(self):
        await self.fill_roster()
        self.final = await confirm_lineup(self.final)
        client = ipy.Client()
        client.http.get_channel = AsyncMock(side_effect=ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden")))
        blocked = await client.fetch_channel(79)
        fetch_channel = self.bot.fetch_channel.side_effect
        self.bot.fetch_channel.side_effect = lambda _: blocked
        self.bot.cache.fetch_dm_channel.return_value = blocked
        self.assertEqual(await sync_registration_posts(self.bot, self.final),
                         ["Lineup update pending.", "Opponent pick message update pending."])
        self.channel.send.assert_not_awaited()

        self.bot.fetch_channel.side_effect = fetch_channel
        self.bot.cache.fetch_dm_channel.return_value = self.dm
        await sync_registration_posts(self.bot, self.final)
        saved = await EloFinal.get(self.final.id)
        self.assertIsNotNone(saved.lineup_message_id)
        first_dm = self.posts[self.prompt_message(saved, 0).dm_message_id]
        self.bot.fetch_channel.side_effect = lambda channel_id: blocked if channel_id == self.dm.id else self.channel
        await self.choose(1, "5")
        picked = await EloFinal.get(self.final.id)
        self.assertEqual(pick_turn(picked), 1)
        self.assertIsNotNone(self.prompt_message(picked, 1).dm_message_id)
        self.assertFalse(self.prompt_message(picked, 0).dm_retired)
        first_dm.edit.assert_not_awaited()

        self.bot.fetch_channel.side_effect = fetch_channel
        first_dm.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        self.assertEqual(await sync_registration_posts(self.bot, picked), ["Opponent pick message update pending."])
        self.assertFalse(self.prompt_message(await EloFinal.get(self.final.id), 0).dm_retired)
        first_dm.edit.side_effect = None
        await sync_registration_posts(self.bot, picked)
        self.assertEqual(first_dm.edit.await_args.kwargs["components"], [])
        self.assertTrue(self.prompt_message(await EloFinal.get(self.final.id), 0).dm_retired)

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elo_bot.config.ids import ELO_BLACKLIST_ROLE, LVL1_ROLES, STAFF_CHANNEL
from elo_bot.db import EloFinal
from elo_bot.db.final_invitation_models import FinalInvitation
from elo_bot.extensions.final import FinalCommands
from elo_bot.extensions.final.roster import roster_modal
from elo_bot.features.finals.dashboard import DashboardState, dashboard_controls
from elo_bot.features.finals.invitations import (
    accept_invitation, add_players, decline_invitation, invite_players, retire_outdated_invitations,
)
from elo_bot.features.finals.lifecycle import add_entrant, confirm_bracket
from tests.integration._final_fixture import FinalScenarioCase


def member(user_id: int, *, blacklisted: bool = False, dms_closed: bool = False) -> MagicMock:
    member = MagicMock(id=user_id, display_name=f"Player {user_id}")
    member.has_role.side_effect = lambda role: blacklisted and role == ELO_BLACKLIST_ROLE
    sent = MagicMock(id=5000 + user_id)
    sent.channel.id = 6000 + user_id
    member.send = AsyncMock(side_effect=ipy.errors.Forbidden(MagicMock(status=403)) if dms_closed else None,
                            return_value=sent)
    return member


class TestFinalInvitations(FinalScenarioCase):
    async def invite(self, final, *users):
        return await invite_players(final, {str(user): member(user) for user in users}, "manager")

    async def test_each_selected_player_is_checked_on_their_own(self):
        final = await self.roster(5)
        members = {"1": member(1), "6": member(6), "7": member(7, blacklisted=True), "8": None,
                   "20": member(20), "9": member(9, dms_closed=True)}
        outcome = await invite_players(final, members, "manager")
        self.assertEqual((len(outcome.done), len(outcome.refused)), (1, 5))
        saved = await FinalInvitation.find_all().to_list()
        self.assertEqual([(row.user_id, row.state, row.dm_message_id) for row in saved], [("6", "pending", "5006")])
        members["6"].send.assert_awaited_once()

        added = await add_players(await EloFinal.get(final.id), {"6": member(6), "1": member(1)})
        self.assertEqual((len(added.done), len(added.refused)), (1, 1))
        self.assertEqual((await FinalInvitation.find_one({"user_id": "6"})).state, "withdrawn")
        self.assertIn("6", (await EloFinal.get(final.id)).entrant_ids)

    async def test_one_pending_invitation_per_player_until_it_closes(self):
        final = await self.roster(5)
        await self.invite(final, 6)
        self.assertEqual(len((await self.invite(final, 6)).refused), 1)
        invitation = await FinalInvitation.find_one({"user_id": "6"})
        await decline_invitation(invitation.id)
        self.assertEqual(len((await self.invite(final, 6)).done), 1)
        self.assertEqual(await FinalInvitation.find({"user_id": "6", "state": "pending"}).count(), 1)

    async def test_first_to_accept_takes_the_last_seat(self):
        final = await self.roster(7)
        await self.invite(final, 8, 9)
        invitations = await FinalInvitation.find_all().sort("user_id").to_list()
        async with asyncio.timeout(10):
            answers = await asyncio.gather(*(accept_invitation(invitation.id, member(int(invitation.user_id)))
                                             for invitation in invitations))
        self.assertEqual(sorted(answer.state for answer in answers), ["accepted", "full"])
        saved = await EloFinal.get(final.id)
        self.assertEqual(len(saved.entrant_ids), 8)
        winner = next(invitation.user_id for invitation, answer in zip(invitations, answers)
                      if answer.state == "accepted")
        self.assertIn(winner, saved.entrant_ids)
        self.assertEqual(saved.seed_elo[winner], 1000 - int(winner))

    async def test_expired_invitation_cannot_be_accepted_and_its_dm_is_retired(self):
        final = await self.roster(5)
        await self.invite(final, 6)
        invitation = await FinalInvitation.find_one({"user_id": "6"})
        await invitation.set({"expires_at": int(time.time()) - 1})
        answer = await accept_invitation(invitation.id, member(6))
        self.assertEqual((answer.state, answer.closed_now), ("expired", True))
        self.assertNotIn("6", (await EloFinal.get(final.id)).entrant_ids)

        await self.invite(final, 7)
        await (await FinalInvitation.find_one({"user_id": "7"})).set({"expires_at": int(time.time()) - 1})
        message = MagicMock(edit=AsyncMock())
        channel = MagicMock(fetch_message=AsyncMock(return_value=message))
        await retire_outdated_invitations(MagicMock(fetch_channel=AsyncMock(return_value=channel)))
        retired = await FinalInvitation.find_one({"user_id": "7"})
        self.assertEqual((retired.state, retired.dm_outdated), ("expired", False))
        self.assertEqual(message.edit.await_args.kwargs["components"], [])

    async def test_creating_the_bracket_withdraws_pending_invitations(self):
        final = await self.roster(5)
        await self.invite(final, 6)
        await confirm_bracket(await EloFinal.get(final.id), "manager")
        invitation = await FinalInvitation.find_one({"user_id": "6"})
        self.assertEqual((invitation.state, invitation.dm_outdated), ("withdrawn", True))
        answer = await accept_invitation(invitation.id, member(6))
        self.assertEqual((answer.state, answer.closed_now), ("withdrawn", False))

    async def test_manual_add_closes_that_players_invitation(self):
        final = await self.roster(5)
        await self.invite(final, 6)
        await add_entrant(await EloFinal.get(final.id), "6", "Player 6")
        self.assertEqual((await FinalInvitation.find_one({"user_id": "6"})).state, "withdrawn")

    async def test_modal_and_controls_follow_open_seats(self):
        final = await self.roster(6)
        modal = roster_modal("invite", final, "9")
        self.assertEqual(modal["data"]["components"][0]["component"]["max_values"], 2)
        await self.invite(final, 7)
        invitations = await FinalInvitation.find_all().to_list()
        rows = dashboard_controls(DashboardState(await EloFinal.get(final.id), invitations=invitations), "9")
        roster = [button for button in rows[0].components if button.custom_id.startswith("finalroster|")]
        self.assertEqual([button.disabled for button in roster], [False, False])
        self.assertEqual(rows[1].components[0].options[0].value, str(invitations[0].id))
        refresh = rows[0].components[-1]
        self.assertEqual((refresh.label, refresh.emoji.name, refresh.custom_id),
                         (None, "🔄", f"finaldash|refresh|{final.id}|9"))
        full = dashboard_controls(DashboardState(await self.roster_full(final)), "9")
        self.assertTrue(all(button.disabled for button in full[0].components
                            if button.custom_id.startswith("finalroster|")))

    async def roster_full(self, final):
        for user in range(7, 9):
            final = await add_entrant(await EloFinal.get(final.id), str(user), f"Player {user}")
        return final

    async def test_dm_answer_retires_buttons_and_notifies_without_a_ping(self):
        final = await self.roster(5)
        await self.invite(final, 6)
        invitation = await FinalInvitation.find_one({"user_id": "6"})
        staff = MagicMock(send=AsyncMock())
        guild = MagicMock(fetch_member=AsyncMock(return_value=member(6)))
        bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=staff), fetch_guild=AsyncMock(return_value=guild))
        cog = FinalCommands(bot)
        for click in range(2):
            ctx = MagicMock(spec=ipy.ComponentContext, custom_id=f"finalinvite|accept|{invitation.id}",
                            send=AsyncMock(), edit_origin=AsyncMock())
            await cog.final_invitation_answer.callback.func(cog, ctx)
            self.assertEqual(ctx.edit_origin.await_args.kwargs["components"], [])
        bot.fetch_channel.assert_awaited_once_with(STAFF_CHANNEL)
        self.assertEqual(staff.send.await_args.kwargs["allowed_mentions"].parse, [])
        self.assertIn("6", (await EloFinal.get(final.id)).entrant_ids)

    async def test_roster_modal_submit_answers_before_working_and_updates_the_dashboard(self):
        final = await self.roster(5)
        guild = MagicMock(fetch_member=AsyncMock(side_effect=lambda user: member(int(user))))
        cog = FinalCommands(MagicMock(ext={}, fetch_channel=AsyncMock()))
        ctx = MagicMock(spec=ipy.ModalContext, custom_id=f"finalrostermodal|add|{final.id}|9",
                        guild_id=final.guild_id, guild=guild, deferred=False, edit=AsyncMock(), send=AsyncMock())
        ctx.author.id = 9
        ctx.author.has_role.side_effect = lambda role: role in LVL1_ROLES
        order = []
        ctx.defer = AsyncMock(side_effect=lambda **_: order.append("defer"))
        ctx.edit.side_effect = lambda *_args, **_kwargs: order.append("edit")
        await cog.final_roster_submit.callback.func(cog, ctx, players=["6", "7"])
        self.assertEqual(order, ["defer", "edit"])
        ctx.defer.assert_awaited_once_with(edit_origin=True)
        self.assertEqual((await EloFinal.get(final.id)).entrant_ids[-2:], ["6", "7"])
        self.assertEqual(ctx.edit.await_args.args, ("@original",))

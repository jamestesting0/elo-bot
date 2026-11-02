import unittest
from asyncio import Event, gather, wait_for
from datetime import datetime, timezone
from typing import Final
from unittest.mock import AsyncMock, MagicMock, patch

from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

from cogs.challenge_commands import ChallengeCommands, available_challenges
from common.constants import ERROR_EMOJI, SUCCESS_EMOJI
from common.matches.challenges import weekly_challenge_balance
from database_models import Match, MatchPlayerStats, OverrideCode, Participant, Player, RecordedMatch, ServerState


NOW: Final = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
WEEK: Final = 7 * 24 * 60 * 60
DISTRICTS: Final = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]


class TestChallengeLimits(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Match, Player):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(name=model.__name__)))
        self.enterContext(patch.object(Player, "user_id", ExpressionField("user_id"), create=True))
        self.enterContext(patch.object(Match, "token", ExpressionField("token"), create=True))
        self.sender = Player.model_construct(
            id="111", user_id="111", tag="#A", elo=1200, highest_elo=1200,
            matches=[self.record(1)], last_challenged=["", ""],
        )
        self.recipient = Player.model_construct(
            id="222", user_id="222", tag="#B", elo=1200, highest_elo=1200,
            matches=[self.record(1)], last_challenged=["", ""],
        )
        self.ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        self.ctx.author = MagicMock(id=111, mention="<@111>", send=AsyncMock())
        self.opponent = MagicMock(id=222, mention="<@222>", send=AsyncMock())
        for member in (self.ctx.author, self.opponent):
            member.has_role.return_value = False
        self.cog = ChallengeCommands(MagicMock())
        self.players = self.enterContext(patch.object(Player, "find_one", new_callable=AsyncMock))
        self.players.side_effect = [self.sender, self.recipient]
        self.enterContext(patch.object(ServerState, "current", new=AsyncMock(
            return_value=MagicMock(queue_disabled=False),
        )))
        self.override = self.enterContext(patch.object(OverrideCode, "is_valid", new=AsyncMock(return_value=False)))
        self.enterContext(patch.object(OverrideCode, "consume", new=AsyncMock(return_value=True)))
        self.enterContext(patch("cogs.challenge_commands.is_player_occupied", new=AsyncMock(return_value=None)))
        self.enterContext(patch("cogs.challenge_commands.datetime")).now.return_value = NOW
        self.enterContext(patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[]))))
        self.matches = self.enterContext(patch.object(Match, "find_one", new=AsyncMock(return_value=None)))
        self.persisted: list[Match] = []

        async def insert(match: Match) -> Match:
            self.persisted.append(match.model_copy(deep=True))
            return match

        self.enterContext(patch.object(Match, "insert", new=insert))
        self.opponent.send.side_effect = lambda **_kwargs: self.assertEqual(len(self.persisted), 1)
        self.reset = self.enterContext(patch.object(type(self.cog.challenge_send.cooldown), "reset", new_callable=AsyncMock))

    @staticmethod
    def record(index: int, age: int = 60, challenge: bool = False) -> RecordedMatch:
        stats = MatchPlayerStats(stars=9, destruction=300, opponent_stars=9, opponent_destruction=300,
                                 opponent="333", elo_change=0, won=False)
        return RecordedMatch.model_construct(id=str(index), time=int(NOW.timestamp()) - age,
                                            is_challenge=challenge, players={"111": stats, "222": stats})

    async def send(self) -> None:
        callback = self.cog.challenge_send.callback
        await getattr(callback, "func", callback)(self.cog, self.ctx, self.opponent, *DISTRICTS)

    def assert_sent(self) -> None:
        self.assertEqual(len(self.persisted), 1)
        self.assertTrue(self.persisted[0].is_challenge)
        self.assertEqual([p.user_id for p in self.persisted[0].participants], ["111", "222"])
        self.opponent.send.assert_awaited_once()
        self.ctx.send.assert_awaited_once_with(f"{SUCCESS_EMOJI} Your challenge is successfully sent!", ephemeral=True)

    async def test_ineligible_player_is_rejected_before_storage(self) -> None:
        for sender in (True, False):
            for normals, challenges in ((0, 0), (1, 1), (1, 2)):
                with self.subTest(sender=sender, normals=normals, challenges=challenges):
                    # Given: one participant has used all of their recent normal matches.
                    history = [self.record(i + 1) for i in range(normals)]
                    history += [self.record(normals + i + 1, challenge=True) for i in range(challenges)]
                    (self.sender if sender else self.recipient).matches = history
                    (self.recipient if sender else self.sender).matches = [self.record(1)]
                    self.players.side_effect = [self.sender, self.recipient]
                    self.ctx.send.reset_mock()
                    self.reset.reset_mock()
                    # When: the actual send callback checks both participants.
                    await self.send()
                    # Then: no challenge or invitation exists, and the exact failed eligibility is visible.
                    subject = "You need" if sender else "<@222> needs"
                    action = "send" if sender else "receive"
                    self.ctx.send.assert_awaited_once_with(
                        f"{ERROR_EMOJI} {subject} an unused normal ranked match from the past "
                        f"7 days to {action} a challenge. Available challenges: `0`.",
                        ephemeral=True,
                    )
                    self.assertEqual(self.persisted, [])
                    self.opponent.send.assert_not_awaited()
                    self.reset.assert_awaited_once_with(self.ctx)

    async def test_consecutive_challenges_are_allowed_with_enough_normal_matches(self) -> None:
        # Given: both players have 3 normal matches and 2 challenges, with a challenge last.
        history = [self.record(i) for i in range(1, 4)] + [self.record(i, challenge=True) for i in (4, 5)]
        self.sender.matches = history
        self.recipient.matches = history
        # When: they send another challenge.
        await self.send()
        # Then: the committed invitation succeeds without consuming either acceptance allowance.
        self.assert_sent()
        self.assertEqual((self.sender.last_challenged, self.recipient.last_challenged), (["", ""], ["", ""]))

    async def test_cutoff_challenges_do_not_create_debt_against_new_normals(self) -> None:
        # Given: mm | cc mm cc m, stored out of order, for both participants.
        history = [self.record(i + 1, age=100 - i, challenge=kind == "c")
                   for i, kind in enumerate("ccmmccm")]
        history += [self.record(20 + i, age=WEEK + 100 - i) for i in range(2)]
        self.sender.matches = list(reversed(history))
        self.recipient.matches = list(reversed(history))
        # When: the latest normal match supplies one available challenge.
        await self.send()
        # Then: the real callback persists and publishes the invitation.
        self.assert_sent()

    def test_weekly_balance_replays_order_and_never_uses_expired_or_future_normals(self) -> None:
        for sequence, expected in (("", 0), ("ccmmcc", 0), ("ccmmccm", 1), ("mmmcc", 1), ("ccm", 1)):
            with self.subTest(sequence=sequence):
                # Given: recent history has equal timestamps, with IDs defining match order.
                history = [self.record(i + 1, challenge=kind == "c") for i, kind in enumerate(sequence)]
                history += [self.record(100, age=WEEK + 1), self.record(101, age=-1)]
                # When: the balance is reconstructed from an unordered history.
                balance = weekly_challenge_balance(list(reversed(history)), NOW.timestamp())
                # Then: only seven-day normal matches can supply unused allowance.
                self.assertEqual(balance, expected)

    async def test_accepted_unrecorded_challenge_consumes_allowance_without_double_counting(self) -> None:
        for recorded in (False, True):
            with self.subTest(recorded=recorded):
                # Given: an accepted challenge is either awaiting results or already in player history.
                active = Match.model_construct(token="ACTIVE", matched_time=NOW.timestamp(), is_challenge=True,
                                               participants=[
                                                   Participant(user_id="111", confirmed=True),
                                                   Participant(user_id="222", confirmed=True),
                                               ], recorded_match_id="3" if recorded else None)
                self.sender.matches = [self.record(1), self.record(2)]
                if recorded:
                    self.sender.matches.append(self.record(3, challenge=True))
                with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[active]))):
                    # When: the same logical challenge is accounted for.
                    balance = await available_challenges(self.sender, NOW.timestamp())
                # Then: it consumes exactly one normal match, not zero or two.
                self.assertEqual(balance, 1)

    async def test_old_or_future_normal_matches_cannot_supply_eligibility(self) -> None:
        for age in (WEEK + 1, -1):
            with self.subTest(age=age):
                # Given: the sender's only normal match is outside the rolling week.
                self.sender.matches = [self.record(1, age=age)]
                self.players.side_effect = [self.sender, self.recipient]
                self.ctx.send.reset_mock()
                # When: the sender requests a challenge.
                await self.send()
                # Then: that record cannot authorize an invitation.
                self.assertEqual(self.persisted, [])
                self.ctx.send.assert_awaited_once()
                self.assertIn("Available challenges: `0`", self.ctx.send.await_args.args[0])

    async def test_week_boundary_and_current_time_normal_matches_are_included(self) -> None:
        for age in (WEEK, 0):
            with self.subTest(age=age):
                # Given: the sender's normal match lies exactly on a window boundary.
                self.sender.matches = [self.record(1, age=age)]
                self.players.side_effect = [self.sender, self.recipient]
                self.persisted.clear()
                self.opponent.send.reset_mock()
                self.ctx.send.reset_mock()
                # When: the sender requests a challenge.
                await self.send()
                # Then: either boundary counts as recent normal activity.
                self.assert_sent()

    async def test_old_challenges_do_not_reduce_recent_eligibility(self) -> None:
        # Given: a recent normal match followed in the list by an expired challenge.
        self.sender.matches = [self.record(1), self.record(2, age=WEEK + 1, challenge=True)]
        # When: the sender requests a challenge.
        await self.send()
        # Then: only the normal match within the rolling week contributes.
        self.assert_sent()

    async def test_sender_weekly_cap_applies_even_with_an_override(self) -> None:
        # Given: the eligible sender has already used both weekly acceptance slots.
        week = f"{NOW.year}.{NOW.strftime('%U')}"
        self.sender.last_challenged = [week, week]
        self.override.return_value = True
        # When: the sender requests another challenge with a valid override.
        await self.send()
        # Then: the sender's independent weekly cap still prevents a persisted invitation.
        self.ctx.send.assert_awaited_once_with(f"{ERROR_EMOJI} You may only challenge twice per week.", ephemeral=True)
        self.assertEqual(self.persisted, [])
        self.opponent.send.assert_not_awaited()

    async def test_recipient_sending_allowance_does_not_limit_received_challenges(self) -> None:
        # Given: the eligible recipient has already sent two accepted challenges this week.
        week = f"{NOW.year}.{NOW.strftime('%U')}"
        self.recipient.last_challenged = [week, week]
        # When: someone else challenges them.
        await self.send()
        # Then: their sending allowance does not block the incoming invitation.
        self.assert_sent()

    async def test_override_preserves_existing_activity_bypass(self) -> None:
        # Given: a valid override but neither participant has recent matches.
        self.sender.matches = []
        self.recipient.matches = []
        self.override.return_value = True
        # When: the sender requests a challenge.
        await self.send()
        # Then: the existing activity override still authorizes the committed invitation.
        self.assert_sent()
        self.assertTrue(self.persisted[0].challenge_activity_override)

    async def test_recent_same_opponent_restriction_remains(self) -> None:
        # Given: enough normal activity but the sender's recent challenge was against this opponent.
        recent_challenge = self.record(2, challenge=True)
        recent_challenge.players["111"].opponent = "222"
        self.sender.matches = [self.record(1), recent_challenge, self.record(3)]
        # When: the sender requests another challenge against that same player.
        await self.send()
        # Then: the separate repeat-opponent rule still rejects the request before storage.
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} You have already challenged <@222> in your recent challenges.", ephemeral=True,
        )
        self.assertEqual(self.persisted, [])
        self.opponent.send.assert_not_awaited()

    async def test_acceptance_consumes_only_sender_allowance(self) -> None:
        # Given: a pending invitation with an eligible recipient accepting it.
        self.ctx.author = self.opponent
        self.ctx.channel = None
        self.ctx.custom_id = "challenge_accept|TOKEN"
        self.ctx.message.edit = AsyncMock()
        match = Match.model_construct(token="TOKEN", matched_time=NOW.timestamp(), participants=[
            Participant(user_id="111", districts=DISTRICTS, confirmed=True),
            Participant(user_id="222", districts=DISTRICTS),
        ])
        self.matches.return_value = match
        self.players.side_effect = None
        self.players.side_effect = [self.sender, self.recipient, self.sender]
        saved: list[tuple[str, list[str]]] = []

        async def save_player(player: Player) -> Player:
            saved.append((player.user_id, list(player.last_challenged)))
            return player

        # When: the actual acceptance callback persists the sender's allowance.
        with (patch.object(Match, "save_changes", new=AsyncMock(return_value=match)),
              patch.object(Player, "save_changes", new=save_player),
              patch("cogs.challenge_commands.confirm_match", new=AsyncMock(return_value="42"))):
            callback = self.cog.challenge_accept_button.callback
            await getattr(callback, "func", callback)(self.cog, self.ctx)
        # Then: only the sender has one consumed slot in storage.
        self.assertEqual(saved, [("111", [f"{NOW.year}.{NOW.strftime('%U')}", ""])])
        self.assertEqual(self.recipient.last_challenged, ["", ""])

    def pending_acceptance(self, override: bool = False) -> Match:
        self.ctx.author = self.opponent
        self.ctx.channel = None
        self.ctx.custom_id = "challenge_accept|TOKEN"
        self.ctx.message.edit = AsyncMock()
        match = Match.model_construct(token="TOKEN", matched_time=NOW.timestamp(), is_challenge=True,
                                      challenge_activity_override=override, participants=[
                                          Participant(user_id="111", districts=DISTRICTS, confirmed=True),
                                          Participant(user_id="222", districts=DISTRICTS),
                                      ])
        self.matches.return_value = match
        self.players.side_effect = lambda query, **_kwargs: (
            self.sender if query == {"user_id": "111"} else self.recipient
        )
        return match

    async def test_acceptance_rechecks_both_players_before_any_write(self) -> None:
        for sender in (True, False):
            with self.subTest(sender=sender):
                # Given: a participant's only normal has expired since the invitation was sent.
                match = self.pending_acceptance()
                self.sender.matches = [self.record(1)]
                self.recipient.matches = [self.record(1)]
                (self.sender if sender else self.recipient).matches = [self.record(1, age=WEEK + 1)]
                self.ctx.send.reset_mock()
                with (patch.object(Match, "save_changes", new_callable=AsyncMock) as save,
                      patch.object(Player, "save_changes", new_callable=AsyncMock) as save_player,
                      patch("cogs.challenge_commands.confirm_match", new_callable=AsyncMock) as confirm):
                    # When: the recipient accepts.
                    await self.cog.challenge_accept_button.callback.func(self.cog, self.ctx)
                # Then: neither storage, confirmation, nor success controls are changed.
                save.assert_not_awaited()
                save_player.assert_not_awaited()
                confirm.assert_not_awaited()
                self.assertFalse(match.participants[1].confirmed)
                self.ctx.message.edit.assert_not_awaited()
                self.ctx.send.assert_awaited_once()

    async def test_acceptance_preserves_consumed_override(self) -> None:
        # Given: the stored invitation used an override, and neither player has recent activity.
        match = self.pending_acceptance(override=True)
        self.sender.matches = []
        self.recipient.matches = []
        with (patch.object(Match, "save_changes", new=AsyncMock(return_value=match)),
              patch.object(Player, "save_changes", new=AsyncMock(return_value=self.sender)),
              patch("cogs.challenge_commands.confirm_match", new=AsyncMock(return_value="42")) as confirm):
            # When: the invitation is accepted without reusing the consumed code.
            await self.cog.challenge_accept_button.callback.func(self.cog, self.ctx)
        # Then: the persisted override authorizes acceptance and consumes the sender's weekly slot.
        self.assertTrue(match.participants[1].confirmed)
        self.assertEqual(self.sender.last_challenged, [f"{NOW.year}.{NOW.strftime('%U')}", ""])
        confirm.assert_awaited_once()

    async def test_acceptance_rechecks_weekly_cap_even_with_override(self) -> None:
        # Given: another invitation consumed the sender's last weekly slot.
        match = self.pending_acceptance(override=True)
        self.sender.last_challenged = [f"{NOW.year}.{NOW.strftime('%U')}"] * 2
        with (patch.object(Match, "save_changes", new_callable=AsyncMock) as save,
              patch("cogs.challenge_commands.confirm_match", new_callable=AsyncMock) as confirm):
            # When: the recipient accepts this older invitation.
            await self.cog.challenge_accept_button.callback.func(self.cog, self.ctx)
        # Then: an override cannot bypass the weekly cap or commit acceptance.
        save.assert_not_awaited()
        confirm.assert_not_awaited()
        self.assertFalse(match.participants[1].confirmed)

    async def test_concurrent_acceptances_cannot_reuse_the_same_allowance(self) -> None:
        # Given: two invitations share participants with one normal match apiece.
        first = self.pending_acceptance()
        second = first.model_copy(deep=True)
        second.token = "SECOND"
        second_ctx = MagicMock(author=self.opponent, channel=None, custom_id="challenge_accept|SECOND",
                               defer=AsyncMock(), send=AsyncMock(), message=MagicMock(edit=AsyncMock()))
        accepted = []
        saved_players = []
        second_read = Event()

        async def find_match(query) -> Match:
            if query == {"token": "SECOND"}:
                second_read.set()
                return second
            return first

        async def save_match(match: Match) -> Match:
            await wait_for(second_read.wait(), timeout=1)
            accepted.append(match.model_copy(deep=True))
            return match

        async def save_player(player: Player) -> Player:
            saved_players.append(player.user_id)
            return player

        with (patch.object(Match, "find_one", new=AsyncMock(side_effect=find_match)),
              patch.object(Match, "save_changes", new=save_match),
              patch.object(Player, "save_changes", new=save_player),
              patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(side_effect=lambda: accepted))),
              patch("cogs.challenge_commands.confirm_match", new=AsyncMock(return_value="42")) as confirm):
            # When: both acceptances overlap before the first challenge has results.
            await wait_for(gather(
                self.cog.challenge_accept_button.callback.func(self.cog, self.ctx),
                self.cog.challenge_accept_button.callback.func(self.cog, second_ctx),
            ), timeout=2)
        # Then: only the first acceptance commits, consumes a weekly slot, and opens a thread.
        self.assertTrue(first.participants[1].confirmed)
        self.assertFalse(second.participants[1].confirmed)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(saved_players, ["111"])
        confirm.assert_awaited_once()

    async def test_acceptance_failure_never_announces_success(self) -> None:
        for publication_failure in (False, True):
            with self.subTest(publication_failure=publication_failure):
                # Given: either committing acceptance or publishing its controls fails.
                match = self.pending_acceptance()
                self.sender.last_challenged = ["", ""]
                self.ctx.send.reset_mock()
                self.ctx.message.edit.reset_mock(side_effect=True)
                if publication_failure:
                    self.ctx.message.edit.side_effect = RuntimeError("delivery failed")
                save = AsyncMock(return_value=match)
                if not publication_failure:
                    save.side_effect = OperationFailure("write failed")
                with (patch.object(Match, "save_changes", new=save),
                      patch.object(Player, "save_changes", new_callable=AsyncMock) as save_player,
                      patch("cogs.challenge_commands.confirm_match", new_callable=AsyncMock) as confirm):
                    # When: the real acceptance callback reaches the failing boundary.
                    with self.assertRaises(RuntimeError if publication_failure else OperationFailure):
                        await self.cog.challenge_accept_button.callback.func(self.cog, self.ctx)
                # Then: no success is sent; committed acceptance survives a publication failure.
                self.ctx.send.assert_not_awaited()
                confirm.assert_not_awaited()
                if publication_failure:
                    self.assertTrue(match.participants[1].confirmed)
                    save_player.assert_awaited_once()
                else:
                    save_player.assert_not_awaited()
                    self.ctx.message.edit.assert_not_awaited()

    async def test_duplicate_acceptance_does_not_consume_another_weekly_slot(self) -> None:
        # Given: the invitation has already been accepted.
        match = self.pending_acceptance()
        match.participants[1].confirmed = True
        with (patch.object(Match, "save_changes", new_callable=AsyncMock) as save,
              patch.object(Player, "save_changes", new_callable=AsyncMock) as save_player,
              patch("cogs.challenge_commands.confirm_match", new_callable=AsyncMock) as confirm):
            # When: a stale acceptance button is clicked.
            await self.cog.challenge_accept_button.callback.func(self.cog, self.ctx)
        # Then: neither acceptance, quota, nor thread creation runs again.
        save.assert_not_awaited()
        save_player.assert_not_awaited()
        confirm.assert_not_awaited()

    async def test_failed_challenge_persistence_does_not_publish_success(self) -> None:
        # Given: both players qualify but inserting the invitation fails.
        with patch.object(Match, "insert", new=AsyncMock(side_effect=OperationFailure("write failed"))):
            # When: the send callback encounters the database failure.
            with self.assertRaises(OperationFailure):
                await self.send()
        # Then: neither an invitation nor a success acknowledgement is published.
        self.assertEqual(self.persisted, [])
        self.opponent.send.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_failed_invitation_delivery_preserves_committed_match_without_success(self) -> None:
        # Given: the invitation commits but delivering it to the recipient fails.
        self.opponent.send.side_effect = RuntimeError("delivery failed")
        # When: the sender requests a challenge.
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            await self.send()
        # Then: the committed match remains and the sender gets no false success acknowledgement.
        self.assertEqual(len(self.persisted), 1)
        self.ctx.send.assert_not_awaited()

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.features.base_store.models import SelfBaseStoreRequest
from elobot.extensions.match import MatchCommands
from elobot.config.emojis import ERROR_EMOJI
from elobot.config.ids import LVL2_ROLES, PLAYER_ROLE
from elobot.db import Match, Participant
from tests._patch_sites import patch_sites


class TestMatchBaseAutostore(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.owner.id = 999
        self.cog = MatchCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author.id = 111
        self.ctx.author.has_role.return_value = False
        self.ctx.channel.id = 123
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock()
        self.match = MagicMock(spec=Match)
        self.match.participants = [Participant(user_id="111"), Participant(user_id="222")]
        self.match.save_changes = AsyncMock()

    async def _invoke(self, player: int) -> AsyncMock:
        with (
            patch_sites(('elobot.extensions.match.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
            patch("elobot.extensions.match.base_store_storage.store_self_bases", new_callable=AsyncMock) as store,
        ):
            match_model.find_one = AsyncMock(return_value=self.match)
            await self.cog.match_base_autostore.callback.func(self.cog, self.ctx, player=player)
        return store

    async def test_level_two_helper_can_autostore_for_either_opponent(self) -> None:
        # Given: either participant has one of the configured level 2 roles.
        for author_index in (0, 1):
            for role in LVL2_ROLES:
                with self.subTest(author_index=author_index, role=role):
                    self.ctx.author.id = int(self.match.participants[author_index].user_id)
                    self.ctx.author.has_role.side_effect = lambda role_id: role_id == role
                    player = 1 - author_index

                    # When: the helper selects their opponent in match base autostore.
                    store = await self._invoke(player)

                    # Then: storage receives the opponent's side and the invoking helper's identity.
                    store.assert_awaited_once_with(
                        self.cog,
                        self.ctx,
                        SelfBaseStoreRequest(match=self.match, player=player, helper_id=str(self.ctx.author.id)),
                    )
                    self.ctx.send.assert_not_awaited()

    async def test_level_two_helper_cannot_autostore_for_themselves(self) -> None:
        # Given: a match participant has a level 2 role.
        self.ctx.author.has_role.side_effect = lambda role_id: role_id == LVL2_ROLES[-1]

        # When: they select themselves in match base autostore.
        store = await self._invoke(0)

        # Then: the command rejects the action before storage or persistence.
        store.assert_not_awaited()
        self.match.save_changes.assert_not_awaited()
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True,
        )

    async def test_regular_participant_cannot_autostore_for_opponent(self) -> None:
        # Given: the participant has the player role without a level 2 role.
        self.ctx.author.has_role.side_effect = lambda role_id: role_id == PLAYER_ROLE

        # When: they select their opponent in match base autostore.
        store = await self._invoke(1)

        # Then: the command rejects the action before storage or persistence.
        store.assert_not_awaited()
        self.match.save_changes.assert_not_awaited()
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True,
        )

    async def test_nonparticipant_can_still_autostore_selected_side(self) -> None:
        # Given: the invoker is outside the match and has no level 2 role.
        self.ctx.author.id = 333

        # When: they select the second participant.
        store = await self._invoke(1)

        # Then: the existing nonparticipant access routes the selected side to storage.
        store.assert_awaited_once_with(
            self.cog, self.ctx, SelfBaseStoreRequest(match=self.match, player=1, helper_id="333"),
        )
        self.ctx.send.assert_not_awaited()

    async def test_owner_participant_retains_autostore_access(self) -> None:
        # Given: the bot owner is a participant with no level 2 role.
        self.bot.owner.id = self.ctx.author.id

        # When: the owner selects their own side.
        store = await self._invoke(0)

        # Then: the existing owner exception continues to route to storage.
        store.assert_awaited_once_with(
            self.cog, self.ctx, SelfBaseStoreRequest(match=self.match, player=0, helper_id="111"),
        )
        self.ctx.send.assert_not_awaited()

    async def test_missing_match_rejects_before_storage(self) -> None:
        # Given: the command is invoked outside a match thread.
        self.match = None

        # When: match base autostore is invoked.
        store = await self._invoke(1)

        # Then: no storage begins and the invoker receives the ephemeral failure.
        store.assert_not_awaited()
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} Unable to fetch a valid match, please make sure to use this command in a 1v1 thread!",
            ephemeral=True,
        )

    async def test_level_two_participant_still_cannot_use_manual_base_store(self) -> None:
        # Given: a match participant has a level 2 role.
        self.ctx.author.has_role.side_effect = lambda role_id: role_id == LVL2_ROLES[-1]

        with (
            patch_sites(('elobot.extensions.match.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
            patch.object(self.cog, "_prepare_base_store", new_callable=AsyncMock) as prepare,
        ):
            match_model.find_one = AsyncMock(return_value=self.match)
            # When: they select their opponent in the manual base-store command.
            await self.cog.match_base_store.callback.func(
                self.cog, self.ctx, player=1, clan="#CLAN", slot="Slot 1",
            )

        # Then: this permission change remains limited to autostore.
        prepare.assert_not_awaited()
        self.match.save_changes.assert_not_awaited()
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} You cannot store bases as one of the players.", ephemeral=True,
        )


if __name__ == "__main__":
    unittest.main()

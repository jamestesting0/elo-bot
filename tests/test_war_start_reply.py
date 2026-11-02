import re
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.results import UpdateResult

from cogs.base_store import storage as base_store_storage
from cogs.war_hosting import war_start
from common.constants import IMPORTANT_EMOJI, SUCCESS_EMOJI


PLAYER_ID = "100"
OPPONENT_ID = "200"


class TestStoredWarStartReply(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_match() -> MagicMock:
        match = MagicMock()
        match.id = PydanticObjectId()
        match.token = "QA_TOKEN"
        match.is_war = True
        match.thread = "12345"
        match.times = [1000, None]
        match.hosts = ["999", None]
        match.host_clans = ["#CLAN", None]
        match.old_msgs = [None, None]
        match.final_districts = ["D1", "D2", "D3"]
        match.participants = [
            MagicMock(user_id=PLAYER_ID, bases=[], base_store=None, opponent_bases_stored=False),
            MagicMock(
                user_id=OPPONENT_ID,
                bases=[
                    MagicMock(link="https://base/1"),
                    MagicMock(link="https://base/2"),
                    MagicMock(link="https://base/3"),
                ],
            ),
        ]
        match.set = AsyncMock()
        match.get_saved_state.return_value = None
        collection = match.get_pymongo_collection.return_value
        collection.update_one = AsyncMock(return_value=UpdateResult({"n": 1}, acknowledged=True))
        collection.find_one = AsyncMock(return_value={"_id": match.id})
        return match

    async def render_response(
            self,
            reply: war_start.StoredWarStartReply,
            clicker_id: str,
    ) -> str:
        match = self.make_match()
        start_message = MagicMock(spec=ipy.Message, id=7001)
        start_message.pin = AsyncMock()
        start_message.edit = AsyncMock()
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author = MagicMock(id=clicker_id)
        ctx.channel = MagicMock()
        ctx.channel.send = AsyncMock(return_value=start_message)
        ctx.send = AsyncMock()

        bot = MagicMock(spec=ipy.Client)
        bot.fetch_user = AsyncMock(return_value=MagicMock(mention=f"<@{PLAYER_ID}>"))
        coc_client = MagicMock()
        coc_client.get_player = AsyncMock(return_value=MagicMock(
            tag="#PLAYER",
            share_link="https://player",
        ))
        match_model = MagicMock()
        match_model.find_one = AsyncMock(return_value=match)
        player_model = MagicMock()
        player_model.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))

        clan = MagicMock(tag="#CLAN", share_link="https://clan")
        clan.name = "QA Clan"
        with (
                patch.object(war_start.match_commands, "Match", new=match_model),
                patch.object(war_start.match_commands, "Player", new=player_model),
                patch.object(war_start.match_commands, "send_bases", new_callable=AsyncMock),
                patch.object(war_start.match_commands, "remove_helper_task", new_callable=AsyncMock),
                patch.object(
                    base_store_storage,
                    "clear_participant_base_store",
                    new_callable=AsyncMock,
                ),
        ):
            await war_start.complete_war_start(
                bot,
                ctx,
                war_start.WarStart(
                    match=match,
                    player=0,
                    host=MagicMock(id=999, mention="<@999>"),
                    clan=clan,
                    coc_client=coc_client,
                    reply=reply,
                ),
            )

        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.kwargs["suppress_embeds"])
        return str(ctx.send.await_args.args[0])

    async def test_stored_reply_routes_mentions_and_links(self) -> None:
        for helper_hosted, clicker, mentions, linked in (
            (True, PLAYER_ID, [], False),
            (True, "300", [f"<@{PLAYER_ID}>"], False),
            (False, "300", [f"<@{PLAYER_ID}>", f"<@{OPPONENT_ID}>"], True),
        ):
            with self.subTest(helper_hosted=helper_hosted, clicker=clicker):
                reply = war_start.StoredWarStartReply(
                    slot="Slot 1", slot_instruction="", helper_hosted=helper_hosted,
                )

                response = await self.render_response(reply, clicker)

                self.assertTrue(response.startswith(str(SUCCESS_EMOJI)))
                self.assertEqual(re.findall(r"<@\d+>", response), mentions)
                self.assertEqual("[#PLAYER](https://player)" in response, linked)
                if helper_hosted and clicker != PLAYER_ID:
                    self.assertIn("#PLAYER", response)
                self.assertNotIn(str(IMPORTANT_EMOJI), response)


if __name__ == "__main__":
    unittest.main()

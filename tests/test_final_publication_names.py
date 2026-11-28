import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import LOSS_EMOJI, WIN_EMOJI
from elo_bot.db import EloFinal, EloFinalMatch
from elo_bot.db.final_models import FinalMatchResult, FinalResultSnapshot
from elo_bot.db.season_models import FinalResultProgress
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.result_publication import publish_result


class TestFinalPublicationNames(unittest.IsolatedAsyncioTestCase):
    async def test_bracket_send_and_refresh_resolve_members_and_missing_players(self):
        for message_id in (None, "99"):
            with self.subTest(message_id=message_id):
                final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, channel_id=7, active=True,
                    bracket_message_id=message_id, entrant_ids=["1", "2"], nodes={
                        "QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], winner="1"),
                    })
                message = MagicMock(id=99, pin=AsyncMock(), edit=AsyncMock())
                channel = MagicMock(send=AsyncMock(return_value=message),
                                    fetch_message=AsyncMock(return_value=message))
                channel.guild.fetch_member = AsyncMock(
                    side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A") if user == "1" else None,
                )
                bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
                collection = MagicMock(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
                with patch("elo_bot.features.finals.publication.reload_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection), \
                        patch("elo_bot.features.finals.publication.render_bracket", return_value=b"bracket-image"), \
                        patch("elo_bot.features.finals.publication.submitted_predictions", new=AsyncMock(return_value=0)):
                    await publish_bracket(bot, final)
                publication = message.edit if message_id else channel.send
                text = publication.await_args.kwargs["embed"].fields[0].value
                self.assertIn("[Player A](<https://discordapp.com/users/1?tab=wishlist>)", text)
                self.assertIn("https://discordapp.com/users/2", text)
                self.assertIn(str(WIN_EMOJI), text)

    async def test_result_send_refresh_and_deleted_thread_use_publication_guild_names(self):
        for message_id, deleted in ((None, False), ("99", False), (None, True)):
            with self.subTest(message_id=message_id, deleted=deleted):
                result = FinalMatchResult.model_construct(id="result1", node_id="QF1", source_thread="8",
                    snapshot=FinalResultSnapshot(contestants=["1", "2"], winner="1",
                        scores={"1": ["3.100"] * 7, "2": ["3.100"] * 7}))
                final = EloFinal.model_construct(id=PydanticObjectId(), season_number=13, channel_id=7,
                    result_progress={result.id: FinalResultProgress(message_id=message_id)},
                    nodes={"QF1": EloFinalMatch(id="QF1", contestants=["1", "2"], result_id=result.id)})
                message = MagicMock(id=99, edit=AsyncMock())
                channel = MagicMock(send=AsyncMock(return_value=message),
                                    fetch_message=AsyncMock(return_value=message))
                channel.guild.fetch_member = AsyncMock(
                    side_effect=lambda user: SimpleNamespace(id=1, display_name="Player A", display_avatar=None) if user == "1" else None,
                )
                bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
                if deleted:
                    bot.fetch_channel.side_effect = [
                        ipy.errors.NotFound(MagicMock(status=404, reason="Not Found")), channel,
                    ]
                collection = MagicMock(update_one=AsyncMock())
                with patch("elo_bot.features.finals.result_publication.reload_final", new=AsyncMock(return_value=final)), \
                        patch.object(EloFinal, "get_pymongo_collection", return_value=collection):
                    await publish_result(bot, final, result)
                publication = message.edit if message_id else channel.send
                embed = publication.await_args.kwargs["embed"]
                self.assertIn("[Player A](<https://discordapp.com/users/1?tab=wishlist>)", embed.description)
                self.assertIn("https://discordapp.com/users/2", embed.description)
                self.assertIn(str(WIN_EMOJI), embed.description)
                self.assertIn(str(LOSS_EMOJI), embed.description)
                self.assertEqual(embed.fields, [])
                self.assertEqual(embed.image.url, "attachment://scoreboard.png")

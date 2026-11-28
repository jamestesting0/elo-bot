from copy import deepcopy

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal, Match
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.features.finals.embeds import final_season_label
from elo_bot.features.finals.posts import PUBLICATION_LOCKS
from elo_bot.features.matches.message_lock import match_message_lock
from elo_bot.features.matches.messages import (
    MatchMessagePublicationError, MatchMessageState, match_title, match_title_parts, publish_match_message,
)
from elo_bot.features.matches.publication_state import restore_match_publication


async def _refresh_post_header(bot: ipy.Client, channel_id: int, message_id: str,
                               legacy_title: str, season: str, title: str) -> None:
    try:
        channel = await bot.fetch_channel(channel_id)
        if channel is None:
            raise FinalError("The Final channel is unavailable.")
        message = await channel.fetch_message(int(message_id))
    except ipy.errors.NotFound:
        return
    if message.author.id != bot.user.id or not message.embeds or message.embeds[0].title != legacy_title:
        return
    embeds = deepcopy(message.embeds)
    embeds[0].set_author(name=season)
    embeds[0].title = title
    await edit_channel_message(message, embeds=embeds)


async def _refresh_match_header(ctx: ipy.InteractionContext, bot: ipy.Client, final: EloFinal,
                                node_id: str, season: str) -> None:
    node = final.nodes[node_id]
    async with match_message_lock(node.thread_id):
        source = await Match.get(node.match_id)
        if (source is None or source.final_superseded or not source.msg or source.thread != node.thread_id
                or source.final_id != str(final.id) or source.final_node_id != node_id):
            return
        channel = await bot.fetch_channel(source.routing_channel_id)
        if channel is None:
            raise FinalError("The Final channel is unavailable.")
        message = await channel.fetch_message(int(source.msg))
        if message.author.id != bot.user.id:
            return
        publication = source.public_message_publication
        if publication and publication.pending:
            if source.final_result_id:
                return
            if not await restore_match_publication(message, source, None):
                raise FinalError("The match announcement changed during recovery.")
            embed = ipy.Embed.from_dict(publication.embeds[0])
        elif len(message.embeds) == 1:
            embed = message.embeds[0]
        else:
            return
        _, status = match_title_parts(embed.title)
        if status not in {"**Match Started**", "**Match Scheduled**", "**Match Rescheduled**",
                          "**Match Ended**", "**Match Aborted**"}:
            return
        if embed.title != match_title(status, node_id) or not embed.author or embed.author.name != season:
            await publish_match_message(ctx, source, MatchMessageState.capture(source, 0), embed=embed,
                                        existing_message=message, public_channel=channel)


async def refresh_final_headers(ctx: ipy.InteractionContext, bot: ipy.Client, final: EloFinal) -> list[str]:
    season = final_season_label(final.season_number)
    pending = []
    for title, message_id, lock, legacy in (
        ("Lineup", final.lineup_message_id, f"registration:{final.id}",
         f"**Season {final.season_number} Elo Final · Lineup**"),
        ("Bracket", final.bracket_message_id, str(final.id), f"**{season} · Bracket**"),
    ):
        if not message_id:
            continue
        try:
            async with PUBLICATION_LOCKS.setdefault(lock, anyio.Lock()):
                await _refresh_post_header(bot, final.channel_id, message_id, legacy, season, f"**{title}**")
        except (ipy.errors.HTTPException, FinalError):
            pending.append(f"{title} header update pending.")
    for node in final.nodes.values():
        if node.result_id:
            progress = final.result_progress[node.result_id.replace(".", "_")]
            replies = {(final.channel_id, progress.public_message_id),
                       (int(progress.channel_id or node.thread_id or final.channel_id), progress.message_id)}
            async with PUBLICATION_LOCKS.setdefault(f"result:{final.id}:{node.id}", anyio.Lock()):
                for channel_id, message_id in replies:
                    if not message_id:
                        continue
                    try:
                        await _refresh_post_header(bot, channel_id, message_id, f"**{season} · {node.id}**",
                                                   season, f"**{node.id} · Result**")
                    except (ipy.errors.HTTPException, FinalError):
                        warning = f"{node.id} result header update pending."
                        if warning not in pending:
                            pending.append(warning)
        if node.match_id and node.thread_id:
            try:
                await _refresh_match_header(ctx, bot, final, node.id, season)
            except ipy.errors.NotFound:
                continue
            except (ipy.errors.HTTPException, FinalError, PyMongoError, MatchMessagePublicationError):
                pending.append(f"{node.id} match announcement header update pending.")
    return pending

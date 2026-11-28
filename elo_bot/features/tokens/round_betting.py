from collections.abc import Sequence
from contextlib import ExitStack, suppress
from hashlib import sha256
import json

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import TOKEN_EMOJI, WIN_EMOJI
from elo_bot.config.ids import FINAL_CHANNEL
from elo_bot.core.finals import FinalError, ROUND_NODES
from elo_bot.db import EloFinal
from elo_bot.db.season_models import FinalBettingPost
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.embeds import embed_color, v2_footer_text
from elo_bot.discord.mentions import fetch_members
from elo_bot.discord.message_edits import MessageEditUnconfirmed, edit_channel_message
from elo_bot.features.finals.bracket import seeded_players
from elo_bot.features.finals.head_to_head import final_matchup_attachment
from elo_bot.features.finals.labels import final_nickname, final_player_link, market_label
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.posts import PUBLICATION_LOCKS, PostReference, locate_post


BETTING_LINKS = {"Quarterfinals": "QF Betting", "Semifinals": "SF Betting", "Final": "Final Betting"}
BETTING_TITLES = {"Quarterfinals": "Quarterfinal Betting", "Semifinals": "Semifinal Betting", "Final": "Final Betting"}


def _fingerprint(value) -> str:
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _current_markets(final: EloFinal, round_name: str,
                     markets: Sequence[FinalBettingMarket]) -> list[FinalBettingMarket]:
    by_node = {market.node_id: market for market in markets
               if (node := final.nodes.get(market.node_id)) is not None and not node.bye
               and node.contestant_revision == market.contestant_revision}
    return [by_node[node] for node in ROUND_NODES[round_name] if node in by_node]


def _artwork_name(final: EloFinal, round_name: str, market: FinalBettingMarket) -> str:
    seeds = seeded_players(final)
    players = [(user, final.player_nicknames.get(user), seeds.index(user) + 1 if user in seeds else None)
               for user in market.contestants]
    return f"{_artwork_prefix(final, round_name)}{market.node_id}-{_fingerprint(players)[:16]}.webp"


def _artwork_prefix(final: EloFinal, round_name: str) -> str:
    return f"final-betting-{final.id}-{ROUND_NODES[round_name][0]}-"


def _allows_bets(final: EloFinal) -> bool:
    return final.active and not final.repair_pending and final.state not in {"CANCELLING", "CANCELLED"}


def _board_header(final: EloFinal, round_name: str) -> str:
    return f"## S{final.season_number} · {BETTING_TITLES[round_name]}"


def _signature(final: EloFinal, round_name: str, markets: Sequence[FinalBettingMarket]) -> str:
    return _fingerprint([3, _board_header(final, round_name), v2_footer_text(), _allows_bets(final), [
        [market.id, market.contestants, market.state, market.pools, market.desired_winner,
         [final_nickname(final, user) for user in market.contestants], _artwork_name(final, round_name, market)]
        for market in markets
    ]])


def _legacy_pending(market: FinalBettingMarket, board_id: str | None) -> bool:
    return bool((market.announcement_id or market.announcement_attempted)
                and market.announcement_signature != f"round:{board_id}")


def round_post_pending(final: EloFinal, round_name: str, markets: Sequence[FinalBettingMarket]) -> bool:
    round_markets = [market for market in markets if market.node_id in ROUND_NODES[round_name]]
    post = final.betting_posts.get(round_name)
    if post is None:
        return final.active and final.state not in {"CANCELLING", "CANCELLED"} and bool(round_markets)
    return (not post.message_id or bool(post.error)
            or post.signature != _signature(final, round_name, _current_markets(final, round_name, round_markets))
            or any(_legacy_pending(market, post.message_id) for market in round_markets))


def round_betting_components(
        final: EloFinal, round_name: str, markets: Sequence[FinalBettingMarket],
        members: dict[str, ipy.Member | None], gallery: list[ipy.MediaGalleryItem],
) -> list[ipy.ContainerComponent | ipy.ActionRow]:
    lines = []
    options = []
    for market in markets:
        players = [final_player_link(final, user, members[user])
                   + (f" {WIN_EMOJI}" if market.state == "PAID" and user == market.desired_winner else "")
                   for user in market.contestants]
        lines.append(f"`{market.node_id}` {' vs. '.join(players)} · {market_label(market)} · "
                     f"{sum(market.pools.values()):,} {TOKEN_EMOJI}")
        if market.state == "OPEN" and _allows_bets(final):
            names = [final.player_nicknames.get(user, user) for user in market.contestants]
            detail = " vs. ".join(" ".join(name.split()) for name in names)
            options.append(ipy.StringSelectOption(label=f"{market.node_id} · {detail}"[:100], value=market.id))
    body = ipy.TextDisplayComponent("\n".join(lines) or "No markets available.")
    card = ipy.ContainerComponent(ipy.TextDisplayComponent(_board_header(final, round_name)), body,
                                  accent_color=embed_color())
    if gallery:
        card.append(ipy.MediaGalleryComponent(gallery))
    card.append(ipy.TextDisplayComponent(v2_footer_text()))
    components: list[ipy.ContainerComponent | ipy.ActionRow] = [card]
    if options:
        components.append(ipy.ActionRow(ipy.StringSelectMenu(*options, placeholder="⚔️ Choose a match",
                                                            custom_id=f"betround|{final.id}|{round_name}")))
    return components


def _legacy_button_ids(message: ipy.Message) -> set[str]:
    return {control.custom_id for row in message.components if isinstance(row, ipy.ActionRow)
            for control in row.components if isinstance(control, ipy.Button)}


async def _redirect_legacy_post(message: ipy.Message, node_id: str, url: str) -> None:
    with suppress(ipy.errors.NotFound):
        await edit_channel_message(message, content=f"`{node_id}` · [Betting board](<{url}>)",
                                   embeds=[], components=[], attachments=[], allowed_mentions=ipy.AllowedMentions.none(),
                                   require_confirmation=True)


async def _redirect_legacy_posts(bot: ipy.Client, final: EloFinal, board: ipy.Message,
                                 markets: Sequence[FinalBettingMarket], board_channel: ipy.GuildText,
                                 *, audit: bool) -> None:
    url = f"https://discord.com/channels/{final.guild_id}/{board_channel.id}/{board.id}"
    redirected = set()
    failures = []
    for market in markets:
        if not _legacy_pending(market, str(board.id)):
            continue
        try:
            channel_id = market.announcement_channel_id or final.channel_id or FINAL_CHANNEL
            channel = board_channel if channel_id == board_channel.id else await bot.fetch_channel(channel_id)
            if channel is None:
                raise FinalError("The old betting channel is unavailable.")
            nonce = sha256(f"final-market:{market.id}".encode()).hexdigest()[:24]
            button_id = f"betopen|{market.id}"
            reference = PostReference(nonce, bot.user.id, market.announcement_id, market.announcement_attempted,
                                      lambda message: button_id in _legacy_button_ids(message))
            legacy = await locate_post(channel, reference)
            if legacy is not None:
                redirected.add(legacy.id)
                await _redirect_legacy_post(legacy, market.node_id, url)
            await market.set({"announcement_signature": f"round:{board.id}", "publication_error": None})
        except (ipy.errors.HTTPException, FinalError, MessageEditUnconfirmed) as error:
            failures.append(error)
    if audit and markets:
        by_button = {f"betopen|{market.id}": market.node_id for market in markets}
        after = ipy.Snowflake.from_datetime(final.id.generation_time)
        channel_ids = {board_channel.id, final.channel_id}
        channel_ids.update(market.announcement_channel_id for market in markets if market.announcement_channel_id)
        for channel_id in channel_ids:
            try:
                channel = board_channel if channel_id == board_channel.id else await bot.fetch_channel(channel_id)
                if channel is None:
                    raise FinalError("The old betting channel is unavailable.")
                async for message in channel.history(limit=0, after=after):
                    if message.author.id != bot.user.id or message.id in redirected:
                        continue
                    matching = _legacy_button_ids(message) & by_button.keys()
                    if len(matching) == 1:
                        try:
                            await _redirect_legacy_post(message, by_button[matching.pop()], url)
                        except (ipy.errors.HTTPException, MessageEditUnconfirmed) as error:
                            failures.append(error)
            except (ipy.errors.HTTPException, FinalError) as error:
                failures.append(error)
    if failures:
        raise failures[0]


async def _load_round_markets(final: EloFinal, round_name: str) -> list[FinalBettingMarket]:
    return await FinalBettingMarket.find({"final_id": str(final.id),
        "node_id": {"$in": list(ROUND_NODES[round_name])}}).to_list()


async def publish_betting_round(bot: ipy.Client, final: EloFinal, round_name: str, *, audit: bool = False) -> None:
    lock = PUBLICATION_LOCKS.setdefault(f"round-betting:{final.id}:{round_name}", anyio.Lock())
    async with lock:
        path = f"betting_posts.{round_name}"
        collection = EloFinal.get_pymongo_collection()
        try:
            final = await reload_final(final)
            all_markets = await _load_round_markets(final, round_name)
            if not audit and not round_post_pending(final, round_name, all_markets):
                return
            markets = _current_markets(final, round_name, all_markets)
            signature = _signature(final, round_name, markets)
            post = final.betting_posts.get(round_name, FinalBettingPost())
            channel = await bot.fetch_channel(post.channel_id or FINAL_CHANNEL)
            if channel is None:
                raise FinalError("The Final channel is unavailable.")
            nonce = sha256(f"final-round-betting:{final.id}:{round_name}".encode()).hexdigest()[:24]
            prefix = _artwork_prefix(final, round_name)
            selector_id = f"betround|{final.id}|{round_name}"

            def matches_board(message: ipy.Message) -> bool:
                return (any(attachment.filename.startswith(prefix) for attachment in message.attachments)
                        or any(getattr(control, "custom_id", None) == selector_id
                               for part in message.components
                               for row in (part.components if isinstance(part, ipy.ContainerComponent) else (part,))
                               if isinstance(row, ipy.ActionRow)
                               for control in row.components))

            board = await locate_post(channel, PostReference(nonce, bot.user.id, post.message_id, post.attempted, matches_board))
            if board is None and (not final.active or final.state in {"CANCELLING", "CANCELLED"}):
                await collection.update_one({"_id": final.id}, {"$unset": {path: ""}})
                return
            if board is None or post.signature != signature:
                if board is None:
                    claimed = await collection.update_one({"_id": final.id}, {"$set": {
                        f"{path}.attempted": True, f"{path}.channel_id": int(channel.id),
                    }})
                    if not claimed.matched_count:
                        raise FinalError("The Final changed before its betting board was published.")
                board, complete = await _write_board(channel, board, final, round_name, markets, nonce)
                saved = await collection.update_one({"_id": final.id}, {"$set": {
                    f"{path}.message_id": str(board.id), f"{path}.signature": signature if complete else None,
                }})
                if not saved.matched_count:
                    raise FinalError("Could not save the betting board link.")
                if not complete:
                    raise FinalError("Matchup artwork is pending.")
                latest = await reload_final(final)
                latest_markets = await _load_round_markets(latest, round_name)
                if signature != _signature(latest, round_name, _current_markets(latest, round_name, latest_markets)):
                    raise FinalError("Betting changed during publication.")
            await _redirect_legacy_posts(bot, final, board, all_markets, channel, audit=audit or bool(post.error))
            await collection.update_one({"_id": final.id}, {"$set": {f"{path}.error": None}})
        except (ipy.errors.HTTPException, FinalError, PyMongoError, MessageEditUnconfirmed) as error:
            with suppress(PyMongoError):
                await collection.update_one({"_id": final.id}, {"$set": {f"{path}.error": str(error)}})
            raise FinalError(f"{BETTING_LINKS[round_name]} update pending. Use `/final recover`.") from error


async def _write_board(channel, board, final, round_name, markets, nonce) -> tuple[ipy.Message, bool]:
    members = await fetch_members(channel.guild, {user for market in markets for user in market.contestants})
    retained = []
    files = []
    gallery = []
    attachments = {attachment.filename: attachment for attachment in board.attachments
                   if not attachment.description} if board is not None else {}
    filenames = [_artwork_name(final, round_name, market) for market in markets]
    with ExitStack() as stack:
        if all(filename in attachments for filename in filenames):
            retained = [attachments[filename] for filename in filenames]
            gallery = [ipy.MediaGalleryItem(attachment.url.split("?", 1)[0])
                       for attachment in retained]
        else:
            for market, filename in zip(markets, filenames, strict=True):
                if artwork := await final_matchup_attachment(final, tuple(market.contestants), members):
                    stack.enter_context(artwork)
                    artwork.file_name = filename
                    files.append(artwork)
                    gallery.append(ipy.MediaGalleryItem(f"attachment://{filename}"))
        components = round_betting_components(final, round_name, markets, members, gallery)
        mentions = ipy.AllowedMentions.none()
        if board is None:
            board = await channel.send(components=components, files=files or None, nonce=nonce, enforce_nonce=True,
                                       allowed_mentions=mentions)
        else:
            await edit_channel_message(board, components=components, attachments=retained, files=files or None,
                                       allowed_mentions=mentions, require_confirmation=True)
    return board, len(gallery) == len(markets)

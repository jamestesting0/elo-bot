from hashlib import sha256
import json

import anyio

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.ids import FINAL_CHANNEL
from elo_bot.config.emojis import TOKEN_EMOJI, CROSS_EMOJI, TICK_EMOJI, WIN_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.features.tokens.bets import projected_return
from elo_bot.features.tokens.wallets import ensure_wallet, wallet_summary
from elo_bot.discord.mentions import mention_user, member_names
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBet, FinalBettingMarket
from elo_bot.features.finals.labels import market_label
from elo_bot.features.finals.posts import PUBLICATION_LOCKS, PostContent, PostReference, publish_post


def market_embed(final: EloFinal, market: FinalBettingMarket, names: dict[str, str] | None = None) -> ipy.Embed:
    names = names or {}
    total = sum(market.pools.values())
    lines = []
    for user in market.contestants:
        pool = market.pools.get(user, 0)
        multiplier = f"×{total / pool:.2f}" if pool else "×—"
        won = f" {WIN_EMOJI}" if market.state == "PAID" and user == market.desired_winner else ""
        lines.append(f"{names.get(user, mention_user(None, user))}{won} — {pool:,} {TOKEN_EMOJI} · {multiplier}")
    # The pool rows name both players and mark the winner, so the status line names nobody.
    status = "Paid" if market.state == "PAID" else market_label(market, names)
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · {market.node_id} Betting**", color=COLOR,
        description=status
                    + (". Winning bets split the whole pool." if market.state == "OPEN" else "")
                    + "\n\n" + "\n".join(lines))
    embed.add_field(name="**Total Pool**", value=f"{total:,} {TOKEN_EMOJI}")
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


def market_controls(market: FinalBettingMarket):
    return [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Place / Edit Bet", emoji=TOKEN_EMOJI,
                       custom_id=f"betopen|{market.id}",
                       disabled=market.state != "OPEN")]


def market_signature(market: FinalBettingMarket) -> str:
    state = {key: getattr(market, key) for key in (
        "state", "version", "pools", "escrow", "settled_result_version", "void_reason", "desired_winner",
    )}
    return sha256(json.dumps({"presentation": 3, **state}, sort_keys=True).encode()).hexdigest()


async def publish_market(bot, final: EloFinal, market: FinalBettingMarket):
    async with PUBLICATION_LOCKS.setdefault(f"market:{market.id}", anyio.Lock()):
        return await _publish_market(bot, final, await FinalBettingMarket.get(market.id))


async def _publish_market(bot, final: EloFinal, market: FinalBettingMarket):
    signature = market_signature(market)
    channel_id = market.announcement_channel_id or (
        final.channel_id if market.announcement_id or market.announcement_attempted else FINAL_CHANNEL)
    collection = FinalBettingMarket.get_pymongo_collection()
    try:
        channel = await bot.fetch_channel(channel_id)
        names = await member_names(channel.guild, market.contestants)
        nonce = sha256(f"final-market:{market.id}".encode()).hexdigest()[:24]
        reference = PostReference(nonce, bot.user.id, market.announcement_id, market.announcement_attempted)
        await collection.update_one({"_id": market.id},
            {"$set": {"announcement_attempted": True, "announcement_channel_id": channel_id}})
        message = await publish_post(channel, reference, PostContent(market_embed(final, market, names),
                                                                    market_controls(market)))
        await collection.update_one({"_id": market.id},
            {"$set": {"announcement_id": str(message.id), "announcement_signature": signature,
                      "publication_error": None}})
        if market_signature(await FinalBettingMarket.get(market.id)) != signature:
            raise FinalError("Betting changed. Its announcement update is pending; use `/final recover`.")
        return message
    except (ipy.errors.HTTPException, FinalError) as error:
        await collection.update_one({"_id": market.id},
            {"$set": {"publication_error": str(error)}})
        raise FinalError("Market saved. Announcement update is pending; use `/final recover`.") from error


async def bet_review(market: FinalBettingMarket, user: str, contestant: str, amount: int, version: int,
                     *, guild: ipy.Guild):
    final = await EloFinal.get(PydanticObjectId(market.final_id))
    wallet = await ensure_wallet(user)
    old = await FinalBet.find_one({"market_id": market.id, "wallet_id": wallet.id})
    if market.state != "OPEN":
        raise FinalError("Betting is closed for this match.")
    if (old.version if old else 0) != version:
        raise FinalError("Your bet changed. Open its current view.")
    _, _, debt = await wallet_summary(wallet)
    previous = old.amount if old and old.active else 0
    _, profit = projected_return(market, old, contestant, amount)
    member = await guild.fetch_member(contestant) if amount else None
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · {market.node_id} Bet Review**", color=COLOR,
        description=f"{mention_user(member, contestant)} · {amount:,} {TOKEN_EMOJI}" if amount else
                    f"Cancel bet · Refund: {previous:,} {TOKEN_EMOJI}")
    if amount:
        embed.add_field(name="**Estimated Profit**", value=f"{profit:,} {TOKEN_EMOJI}")
    if debt:
        embed.add_field(name="**Debt**", value=f"{debt:,} {TOKEN_EMOJI}")
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    controls = [ipy.Button(style=ipy.ButtonStyle.SUCCESS if amount else ipy.ButtonStyle.DANGER,
                          label="Confirm Bet" if amount else "Cancel Bet",
                          emoji=TICK_EMOJI if amount else CROSS_EMOJI,
                          custom_id=f"betconfirm|{market.id}|{user}|{contestant}|{version}|{amount}"),
                ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Back", custom_id=f"betback|{market.id}|{user}")]
    return embed, controls

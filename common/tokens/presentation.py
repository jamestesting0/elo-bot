import asyncio
from hashlib import sha256
import json

import anyio

import interactions as ipy
from beanie import PydanticObjectId

from common.constants import COLOR, FINAL_CHANNEL, FOOTER_IMAGE, FOOTER_TEXT, TOKEN_EMOJI
from common.discord.message_edits import edit_channel_message
from common.finals.types import FinalError
from common.finals.publication import PUBLICATION_LOCKS
from common.tokens.bets import projected_return
from common.tokens.wallets import ensure_wallet, wallet_summary
from common.utils import mention_user
from database_models import EloFinal
from database_models.token_models import FinalBet, FinalBettingMarket


def market_embed(final: EloFinal, market: FinalBettingMarket, names: dict[str, str] | None = None) -> ipy.Embed:
    names = names or {}
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · {market.node_id} Betting**", color=COLOR,
        description=f"{market.state}\n" + "\n".join(
            f"{names.get(user, mention_user(None, user))}: {market.pools.get(user, 0):,} {TOKEN_EMOJI}"
            for user in market.contestants))
    embed.add_field(name="**Total Pool**", value=f"{sum(market.pools.values()):,} {TOKEN_EMOJI}")
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


def market_controls(market: FinalBettingMarket):
    return [ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Place / Edit Bet", emoji=TOKEN_EMOJI,
                       custom_id=f"betopen|{market.id}",
                       disabled=market.state != "OPEN")]


def market_signature(market: FinalBettingMarket) -> str:
    state = {key: getattr(market, key) for key in (
        "state", "version", "pools", "escrow", "settled_result_version", "void_reason",
    )}
    return sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


async def publish_market(bot, final: EloFinal, market: FinalBettingMarket):
    async with PUBLICATION_LOCKS.setdefault(f"market:{market.id}", anyio.Lock()):
        return await _publish_market(bot, final, await FinalBettingMarket.get(market.id))


async def _publish_market(bot, final: EloFinal, market: FinalBettingMarket):
    signature = market_signature(market)
    channel_id = market.announcement_channel_id or (
        final.channel_id if market.announcement_id or market.announcement_attempted else FINAL_CHANNEL)
    channel = await bot.fetch_channel(channel_id)
    members = await asyncio.gather(*(channel.guild.fetch_member(user) for user in market.contestants))
    names = {user: mention_user(member, user) for user, member in zip(market.contestants, members)}
    nonce = sha256(f"final-market:{market.id}".encode()).hexdigest()[:24]
    message = await channel.fetch_message(int(market.announcement_id)) if market.announcement_id else None
    if message is None and market.announcement_attempted:
        messages = await channel.fetch_messages(limit=100)
        message = next((item for item in messages if str(item.nonce) == nonce and item.author.id == bot.user.id), None)
        if message is None:
            raise FinalError("Betting announcement is uncertain. Locate its earlier message before retrying.")
    await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market.id},
        {"$set": {"announcement_attempted": True, "announcement_channel_id": channel_id}})
    try:
        if message is None:
            message = await channel.send(embed=market_embed(final, market, names), components=market_controls(market),
                                         nonce=nonce, enforce_nonce=True)
        else:
            await edit_channel_message(message, embed=market_embed(final, market, names),
                                       components=market_controls(market))
        await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market.id},
            {"$set": {"announcement_id": str(message.id), "announcement_signature": signature,
                      "publication_error": None}})
        if market_signature(await FinalBettingMarket.get(market.id)) != signature:
            raise FinalError("Betting changed. Its announcement update is pending; use `/final recover`.")
        return message
    except ipy.errors.HTTPException as error:
        await FinalBettingMarket.get_pymongo_collection().update_one({"_id": market.id},
            {"$set": {"publication_error": str(error)}})
        raise FinalError("Market saved. Announcement update is pending; use `/final recover`.") from error


async def bet_review(market: FinalBettingMarket, user: str, contestant: str, amount: int, version: int,
                     *, guild: ipy.Guild):
    final = await EloFinal.get(PydanticObjectId(market.final_id))
    wallet = await ensure_wallet(user)
    old = await FinalBet.find_one({"market_id": market.id, "wallet_id": wallet.id})
    if market.state != "OPEN" or (old.version if old else 0) != version:
        raise FinalError("Betting closed or your bet changed. Open its current view.")
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
                          custom_id=f"betconfirm|{market.id}|{user}|{contestant}|{version}|{amount}"),
                ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Back", custom_id=f"betopen|{market.id}")]
    return embed, controls

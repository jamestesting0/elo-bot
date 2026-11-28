import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.config.emojis import TOKEN_EMOJI, CROSS_EMOJI, TICK_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.features.tokens.bets import projected_return
from elo_bot.features.tokens.wallets import ensure_wallet, wallet_summary
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBet, FinalBettingMarket
from elo_bot.features.finals.embeds import final_embed
from elo_bot.features.finals.labels import final_player_link


def market_pool_lines(market: FinalBettingMarket, names: dict[str, str]) -> str:
    total = sum(market.pools.values())
    lines = []
    for user in market.contestants:
        pool = market.pools.get(user, 0)
        multiplier = f"×{total / pool:.2f}" if pool else "×—"
        lines.append(f"{names[user]} · {pool:,} {TOKEN_EMOJI} · {multiplier}")
    return "\n".join(lines)


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
    embed = final_embed(final.season_number, f"{market.node_id} Bet Review",
        description=f"{final_player_link(final, contestant, member)} · {amount:,} {TOKEN_EMOJI}" if amount else
                    f"Cancel bet · Refund: {previous:,} {TOKEN_EMOJI}")
    if amount:
        embed.add_field(name="**Estimated Profit**", value=f"{profit:,} {TOKEN_EMOJI}")
    if debt:
        embed.add_field(name="**Debt**", value=f"{debt:,} {TOKEN_EMOJI}")
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    controls = [ipy.Button(style=ipy.ButtonStyle.SUCCESS if amount else ipy.ButtonStyle.DANGER,
                          label="Confirm Bet" if amount else "Cancel Bet",
                          emoji=TICK_EMOJI if amount else CROSS_EMOJI,
                          custom_id=f"betconfirm|{market.id}|{user}|{contestant}|{version}|{amount}"),
                ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Back", custom_id=f"betback|{market.id}|{user}")]
    return embed, controls

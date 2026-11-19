from collections import defaultdict
from dataclasses import dataclass

from elo_bot.config.emojis import TOKEN_EMOJI
from elo_bot.db.token_models import FinalBettingMarket, TokenLedgerEntry


@dataclass(slots=True)
class BettingStats:
    won: int = 0
    settled: int = 0
    staked: int = 0
    profit: int = 0


async def betting_stats(final_id: str | None = None) -> dict[str, BettingStats]:
    """Settled bets per wallet. Refunded or undecided markets neither win nor lose, so they are left out."""
    query: dict = {"state": "PAID", "desired_refund": False, "desired_winner": {"$ne": None}}
    if final_id is not None:
        query["final_id"] = final_id
    markets = await FinalBettingMarket.find(query).to_list()
    net: dict[tuple[str, str], int] = defaultdict(int)
    for entry in await TokenLedgerEntry.find({"market_id": {"$in": [market.id for market in markets]}}).to_list():
        net[entry.wallet_id, entry.market_id] += entry.amount
    stats: dict[str, BettingStats] = defaultdict(BettingStats)
    for market in markets:
        for wallet_id, stake in market.stakes.items():
            if not stake.amount:
                continue
            wallet = stats[wallet_id]
            wallet.settled += 1
            wallet.won += stake.contestant == market.desired_winner
            wallet.staked += stake.amount
            wallet.profit += net[wallet_id, market.id]
    return dict(stats)


def betting_lines(stats: BettingStats | None) -> str:
    if stats is None or not stats.settled:
        return "No bets."
    return (f"**Won:** `{stats.won}/{stats.settled}` → `{stats.won / stats.settled * 100:.2f}%`\n"
            f"**Profit:** {stats.profit:+,} {TOKEN_EMOJI} · `{stats.profit / stats.staked * 100:+.2f}%`")

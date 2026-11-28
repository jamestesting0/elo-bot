from typing import assert_never

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import TOKEN_EMOJI
from elo_bot.core.token_items import ITEMS
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet
from elo_bot.discord.mentions import fetch_members
from elo_bot.features.finals.labels import final_player_link


def ledger_label(entry: TokenLedgerEntry) -> str:
    match entry.kind:
        case "grant":
            return "Starting tokens"
        case "stake":
            return "Bet"
        case "edit":
            return "Bet changed"
        case "switch":
            return "Bet switched"
        case "cancel":
            return "Bet cancelled"
        case "payout":
            return "Payout"
        case "refund":
            return "Refund"
        case "reversal":
            return "Payout reversed"
        case "identity":
            return ""
        case "adjust":
            return "Manager adjustment"
        case "item_adjust":
            return f"{ITEMS[entry.item].name} {entry.item_quantity:+,}" if entry.item else "Item adjustment"
        case "purchase":
            label = ITEMS[entry.item].name if entry.item is not None else "Item purchase"
            return f"{label} × {entry.item_quantity:,}" if entry.item_quantity > 1 else label
        case "match_hold":
            return "Match stake"
        case "match_payout":
            return "Match stake payout"
        case "match_refund":
            return "Match stake refund"
        case "match_correction":
            return "Match stake correction"
        case "activity_reward":
            return "Match reward"
        case "prediction_reward":
            return "Prediction reward"
        case unreachable:
            assert_never(unreachable)


async def _market_editions(markets: list[FinalBettingMarket]) -> dict[str, EloFinal]:
    if not markets:
        return {}
    finals = await EloFinal.find({"_id": {"$in": list({PydanticObjectId(market.final_id)
                                                     for market in markets})}}).to_list()
    return {str(final.id): final for final in finals}


async def market_scopes(market_ids: set[str]) -> dict[str, str]:
    if not market_ids:
        return {}
    markets = await FinalBettingMarket.find({"_id": {"$in": list(market_ids)}}).to_list()
    finals = await _market_editions(markets)
    return {market.id: f"S{finals[market.final_id].season_number} {market.node_id}"
            for market in markets if market.final_id in finals}


async def open_bet_lines(wallet: TokenWallet, guild: ipy.Guild) -> list[str]:
    bets = await FinalBet.find({"wallet_id": wallet.id, "active": True}).to_list()
    markets = await FinalBettingMarket.find({"_id": {"$in": [bet.market_id for bet in bets]},
                                            "state": {"$in": ["OPEN", "CLOSED", "SETTLING", "CORRECTING"]}}).to_list()
    finals = await _market_editions(markets)
    by_id = {market.id: market for market in markets if market.final_id in finals}
    bets = [bet for bet in bets if bet.market_id in by_id]
    members = await fetch_members(guild, {bet.contestant for bet in bets})
    lines = []
    for bet in bets:
        market = by_id[bet.market_id]
        final = finals[market.final_id]
        name = final_player_link(final, bet.contestant, members[bet.contestant])
        lines.append(f"S{final.season_number} {market.node_id} · {bet.amount:,} {TOKEN_EMOJI} on {name}")
    return lines

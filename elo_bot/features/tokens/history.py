from typing import assert_never

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import TOKEN_EMOJI
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBet, FinalBettingMarket, TokenLedgerEntry, TokenWallet
from elo_bot.discord.mentions import member_names


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
        case unreachable:
            assert_never(unreachable)


async def market_scopes(market_ids: set[str]) -> dict[str, str]:
    if not market_ids:
        return {}
    markets = await FinalBettingMarket.find({"_id": {"$in": list(market_ids)}}).to_list()
    finals = await EloFinal.find({"_id": {"$in": list({PydanticObjectId(market.final_id)
                                                     for market in markets})}}).to_list()
    seasons = {str(final.id): final.season_number for final in finals}
    return {market.id: f"S{seasons[market.final_id]} {market.node_id}"
            for market in markets if market.final_id in seasons}


async def open_bet_lines(wallet: TokenWallet, guild: ipy.Guild) -> list[str]:
    bets = await FinalBet.find({"wallet_id": wallet.id, "active": True}).to_list()
    markets = await FinalBettingMarket.find({"_id": {"$in": [bet.market_id for bet in bets]},
                                            "state": {"$in": ["OPEN", "CLOSED", "SETTLING", "CORRECTING"]}}).to_list()
    active = {market.id for market in markets}
    bets = [bet for bet in bets if bet.market_id in active]
    names = await member_names(guild, {bet.contestant for bet in bets})
    scopes = await market_scopes(active)
    return [f"{scopes[bet.market_id]} · {bet.amount:,} {TOKEN_EMOJI} on {names[bet.contestant]}"
            for bet in bets if bet.market_id in scopes]

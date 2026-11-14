from dataclasses import dataclass

from elobot.features.finals.lifecycle import resolve_final
from elobot.core.finals import FinalError
from elobot.features.tokens.wallets import ensure_wallet
from elobot.discord.mentions import mention_user
from elobot.db import EloFinal, Player
from elobot.db.final_models import FinalPrediction
from elobot.db.token_models import TokenLedgerEntry, TokenWallet
from elobot.config.emojis import RANK_EMOJIS, TOKEN_EMOJI


@dataclass(frozen=True, slots=True)
class FinalBoardRow:
    user_id: str
    value: int
    rank: int
    scope: str
    eligible: int = 0
    debt: int = 0


async def final_board_rows(lb_type: str, season: str) -> list[FinalBoardRow]:
    if lb_type == "Tokens":
        players = await Player.find_all().to_list()
        wallets = await TokenWallet.find({"user_id": {"$in": [player.user_id for player in players]}}).to_list()
        by_user = {wallet.user_id: wallet for wallet in wallets}
        receipts = await TokenLedgerEntry.find({"operation_key": {
            "$in": [f"grant:{wallet.id}" for wallet in wallets]}}).to_list()
        granted = {entry.operation_key for entry in receipts}
        for player in players:
            wallet = by_user.get(player.user_id)
            if wallet is None or not wallet.initialized or f"grant:{wallet.id}" not in granted:
                by_user[player.user_id] = await ensure_wallet(player.user_id)
        values = [(wallet.user_id, max(wallet.balance, 0), 0, max(-wallet.balance, 0))
                  for wallet in by_user.values()]
        scope = "Current Season"
    else:
        if season == "All Seasons":
            finals = await EloFinal.find_all().to_list()
            final_ids = {"$in": [str(final.id) for final in finals]}
            scope = "All Seasons"
        else:
            try:
                final = await resolve_final(int(season) if season.isdigit() else None)
            except FinalError:
                return []
            final_ids = str(final.id)
            scope = f"Season {final.season_number} Final"
        predictions = await FinalPrediction.find({"final_id": final_ids, "submitted_at": {"$ne": None}}).to_list()
        totals: dict[str, tuple[int, int]] = {}
        for prediction in predictions:
            points, eligible = totals.get(prediction.user_id, (0, 0))
            totals[prediction.user_id] = points + prediction.points, eligible + prediction.eligible
        values = [(user_id, points, eligible, 0) for user_id, (points, eligible) in totals.items()]
    values.sort(key=lambda item: (-item[1], item[0]))
    rows = []
    for index, (user, value, eligible, debt) in enumerate(values):
        rows.append(FinalBoardRow(user, value, index + 1, scope, eligible, debt))
    return rows


def final_board_text(rows: list[FinalBoardRow], lb_type: str, names: dict[str, str] | None = None) -> str:
    names = names or {}
    return "\n".join((RANK_EMOJIS[row.rank - 1] if row.rank <= 3 else f"**#{row.rank}**")
        + f" {names.get(row.user_id, mention_user(None, row.user_id))} " + (
        f"`{row.value:,}` {TOKEN_EMOJI}" + (f" · Debt: {row.debt:,} {TOKEN_EMOJI}" if row.debt else "") if lb_type == "Tokens" else
        f"`{row.value}/{row.eligible}` → `{row.value / row.eligible * 100 if row.eligible else 0:.2f}%`")
        for row in rows)

from elo_bot.config.emojis import LOSS_EMOJI, STAR_EMOJI, WIN_EMOJI
from elo_bot.core.finals import NODE_IDS
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.discord.mentions import mention_user
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult, FinalPrediction


def prediction_statuses(final: EloFinal, prediction: FinalPrediction) -> dict[str, str]:
    statuses = {}
    for node_id, pick in prediction.submitted.items():
        node = final.nodes[node_id]
        if node.bye or node.outcome == "Forfeit":
            statuses[node_id] = "excluded"
        elif node.result_id and node.outcome == "Played" and node.winner is not None:
            statuses[node_id] = "correct" if pick == node.winner else "incorrect"
    return statuses


def finished_results(final: EloFinal, results: list[FinalMatchResult],
                     names: dict[str, str], starts: dict[str, int]) -> str:
    by_id = {result.id: result for result in results}
    lines = []
    for node_id in NODE_IDS:
        node = final.nodes.get(node_id)
        result = by_id.get(node.result_id) if node and node.result_id else None
        if result is None or node.bye:
            continue
        snapshot = result.snapshot
        left, right = snapshot.contestants
        def participant(user):
            name = names.get(user, mention_user(None, user))
            return f"{name} {WIN_EMOJI if snapshot.winner == user else LOSS_EMOJI}"
        heading = f"{participant(left)} vs. {participant(right)}"
        if snapshot.outcome == "Forfeit":
            lines.append(f"{heading} (Forfeit)")
            continue
        if result.id in starts:
            heading += f" <t:{starts[result.id]}:R>"
        totals = [f"{sum(get_stars(score) for score in snapshot.scores[user])}{STAR_EMOJI} "
                  f"{sum(get_destruction(score) for score in snapshot.scores[user])}%"
                  for user in (left, right)]
        lines.append(f"{heading}\n{' - '.join(totals)}")
    return "\n\n".join(lines)

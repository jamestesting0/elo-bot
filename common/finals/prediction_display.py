from common.constants import LOSS_EMOJI, STAR_EMOJI, WIN_EMOJI
from common.finals.types import NODE_IDS
from common.utils import get_destruction, get_stars, mention_user
from database_models import EloFinal
from database_models.final_models import FinalMatchResult, FinalPrediction


def prediction_statuses(final: EloFinal, prediction: FinalPrediction) -> dict[str, str]:
    statuses = {}
    for node_id, pick in prediction.submitted.items():
        node = final.nodes[node_id]
        if node.bye or node.outcome == "Forfeit":
            statuses[node_id] = "excluded"
        elif node.result_id and node.outcome == "Played" and node.winner is not None:
            statuses[node_id] = "correct" if pick == node.winner else "incorrect"
    return statuses


def prediction_alt_text(final: EloFinal, prediction: FinalPrediction, statuses: dict[str, str]) -> str:
    lines = ["Submitted prediction bracket:"]
    for node_id in NODE_IDS:
        user = prediction.submitted.get(node_id)
        if user is None:
            continue
        name = " ".join(final.player_nicknames.get(user, user).split())
        # Limit each name to 110 UTF-16 units so all seven picks fit Discord's 1024-unit limit.
        units = 0
        length = 0
        for char in name:
            units += 2 if ord(char) > 0xFFFF else 1
            if units > 109:
                break
            length += 1
        if length < len(name):
            name = name[:length] + "…"
        status = statuses.get(node_id, "pending").capitalize()
        lines.append(f"{node_id}: {name} - {status}")
    return "\n".join(lines)


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
            if snapshot.winner is None:
                return name
            return f"{name} {WIN_EMOJI if snapshot.winner == user else LOSS_EMOJI}"
        heading = f"{participant(left)} vs. {participant(right)}"
        if snapshot.outcome == "Forfeit":
            lines.append(f"{heading} (Forfeit)")
            continue
        if snapshot.winner is None:
            heading += " (Tie)"
        if result.id in starts:
            heading += f" <t:{starts[result.id]}:R>"
        totals = [f"{sum(get_stars(score) for score in snapshot.scores[user])}{STAR_EMOJI} "
                  f"{sum(get_destruction(score) for score in snapshot.scores[user])}%"
                  for user in (left, right)]
        lines.append(f"{heading}\n{' - '.join(totals)}")
    return "\n\n".join(lines)

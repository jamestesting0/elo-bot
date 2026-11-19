from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import ROUND_NODES
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.mentions import mention_user


def result_receipt(final: EloFinal, result: FinalMatchResult, names: dict[str, str], *,
                   pending: bool = False, round_completed: bool = False) -> str:
    winner = result.snapshot.winner
    if pending:
        return f"{SUCCESS_EMOJI} Result saved. {IMPORTANT_EMOJI} Bracket and payout updates are pending and retry automatically."
    forfeit = result.snapshot.outcome == "Forfeit"
    content = f"{SUCCESS_EMOJI} {'Forfeit' if forfeit else 'Result'} saved. "
    name = names.get(winner, mention_user(None, winner))
    target = next((node.id for node in final.nodes.values() if result.node_id in node.feeders), None)
    content += f"{name} advances to {target}." if target else f"Champion: {name}."
    if forfeit:
        content += f" {result.node_id} bets are refunded."
    else:
        rounds = list(ROUND_NODES.items())
        index = next(index for index, (_, ids) in enumerate(rounds) if result.node_id in ids)
        if index < 2 and round_completed:
            content += f" The {rounds[index + 1][0]}{' is' if index == 1 else ' are'} ready to start."
    return content

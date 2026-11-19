from collections.abc import Sequence
from dataclasses import dataclass

import interactions as ipy

from elo_bot.config.emojis import IMPORTANT_EMOJI, TOKEN_EMOJI
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.core.finals import NODE_IDS, ROUND_NODES
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.mentions import mention_user
from elo_bot.features.finals.bracket import seeded_players
from elo_bot.features.finals.labels import market_label, match_line, phase_label, prediction_label
from elo_bot.features.tokens.presentation import market_signature


@dataclass(frozen=True, slots=True)
class DashboardState:
    final: EloFinal
    markets: Sequence[FinalBettingMarket] = ()
    submitted: int = 0


def next_round(state: DashboardState) -> str | None:
    """Select the earliest playable round with unfinished thread or betting work."""
    final = state.final
    if not final.active or final.repair_pending or final.state in {"CANCELLING", "CANCELLED"}:
        return None
    previous = ()
    for name, ids in ROUND_NODES.items():
        nodes = [final.nodes[key] for key in ids if key in final.nodes]
        if not nodes or all(node.winner for node in nodes):
            previous = ids
            continue
        if previous and any(not final.nodes[key].winner for key in previous):
            return None
        for node in nodes:
            if node.bye or node.winner or node.result_id or not all(node.contestants):
                continue
            if not node.thread_id:
                return name
            market = next((market for market in state.markets if market.node_id == node.id
                           and market.contestant_revision == node.contestant_revision), None)
            if market is None:
                if node.first_actual_started_at is None:
                    return name
            elif (not market.announcement_id or market.publication_error
                  or market.announcement_signature != market_signature(market)):
                return name
        return None
    return None


def attention(state: DashboardState) -> list[str]:
    final = state.final
    items = []
    for node in final.nodes.values():
        if node.state == "TIED":
            items.append(f"{IMPORTANT_EMOJI} {node.id} is tied. Choose the winner with "
                         f"`/final match result match:{node.id}`.")
        if node.state == "NEEDS_MANAGER":
            items.append(f"{IMPORTANT_EMOJI} {node.id}: Pairing changed after play started. "
                         "Run `/final round start` to replay it or enter a result.")
        if node.provisioning_error:
            items.append(f"{IMPORTANT_EMOJI} {node.id}: {node.provisioning_error}")
    if recovery_pending(state):
        items.append(f"{IMPORTANT_EMOJI} Bracket and payout updates are pending.")
    return items


def recovery_pending(state: DashboardState) -> bool:
    final = state.final
    return bool(final.repair_pending or final.publication_pending or final.publication_error
                or any(progress.error for progress in final.result_progress.values())
                or any(market.publication_error or market.state in {"SETTLING", "CORRECTING"}
                       for market in state.markets))


def available_actions(state: DashboardState) -> list[tuple[str, str]]:
    final = state.final
    if not final.active or final.state in {"CANCELLING", "CANCELLED"}:
        return [("recover", "Retry Now")] if final.active and recovery_pending(state) else []
    actions = []
    if recovery_pending(state):
        actions.append(("recover", "Retry Now"))
    if final.state == "REGISTRATION":
        if 5 <= len(final.entrant_ids) <= 8:
            actions.append(("bracket", "Create Bracket"))
        return actions
    if final.prediction_state == "OPEN":
        actions.append(("close", "Close Predictions"))
    elif (not final.predictions_ever_opened and not final.first_actual_started_at
          and not any(node.result_id for node in final.nodes.values() if not node.bye)):
        actions.append(("open", "Open Predictions"))
    round_name = next_round(state)
    if round_name:
        actions.append(("round", f"Start {round_name}"))
    if final.champion and not recovery_pending(state) and all(
            market.state in {"PAID", "REFUNDED"} and not market.escrow for market in state.markets):
        actions.append(("end", "End Final"))
    return actions


def dashboard_controls(state: DashboardState, owner: str) -> list[ipy.ActionRow]:
    buttons = [ipy.Button(label=label, style=ipy.ButtonStyle.PRIMARY if index == 0 else ipy.ButtonStyle.SECONDARY,
                          custom_id=f"finaldash|{action}|{state.final.id}|{owner}")
               for index, (action, label) in enumerate(available_actions(state))]
    return [ipy.ActionRow(*buttons)] if buttons else []


def dashboard_embed(state: DashboardState, names: dict[str, str], *, manager: bool) -> ipy.Embed:
    final = state.final
    phase = phase_label(final)
    description = phase
    if not final.nodes:
        description += f" · {len(final.entrant_ids)} of 8 entrants"
    elif phase in ROUND_NODES:
        playable = [final.nodes[key] for key in ROUND_NODES[phase] if not final.nodes[key].bye]
        description += f" · {sum(bool(node.winner) for node in playable)} of {len(playable)} decided"
    embed = ipy.Embed(title=f"**Season {final.season_number} Final**", description=description, color=COLOR)
    fields = []
    if final.nodes:
        lines = [match_line(final.nodes[key], names)
                 + (f" · <#{final.nodes[key].thread_id}>" if manager and final.nodes[key].thread_id else "")
                 for key in NODE_IDS if key in final.nodes]
        fields.append(("Matches", lines))
    elif manager:
        lines = []
        for seed, user in enumerate(seeded_players(final), 1):
            name = names.get(user, mention_user(None, user))
            elo = final.seed_elo.get(user)
            lines.append(f"{seed}. {name} · {elo} Elo" if elo is not None else f"{seed}. {name} · Elo unavailable")
        fields.append(("Entrants", lines))
    fields.append(("Predictions", [f"{prediction_label(final.prediction_state)} · {state.submitted:,} submitted"]))
    if state.markets:
        fields.append(("Betting", [f"{market.node_id} · {market_label(market, names)} · "
                                   f"{sum(market.pools.values()):,} {TOKEN_EMOJI}" for market in state.markets]))
    if manager:
        fields.append(("Needs attention", attention(state)))
        actions = available_actions(state)
        ties = [node.id for node in final.nodes.values() if node.state == "TIED"]
        if ties:
            rounds = list(ROUND_NODES)
            tied_round = next(name for name, ids in ROUND_NODES.items() if ties[0] in ids)
            index = rounds.index(tied_round)
            step = (f"Resolve {', '.join(ties)}, then start the {rounds[index + 1]}." if index < 2
                    else "Resolve F1 to decide the champion.")
            fields.append(("Next step", [step]))
        elif actions:
            fields.append(("Next step", [actions[0][1] + "."]))
        elif final.state == "REGISTRATION":
            fields.append(("Next step", ["Add at least 5 entrants with `/final add`."]))
    for title, lines in fields:
        while lines:
            value = lines.pop(0)
            while lines and len(value) + len(lines[0]) + 1 <= 1024:
                value += "\n" + lines.pop(0)
            embed.add_field(name=f"**{title}**", value=value, inline=False)
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed

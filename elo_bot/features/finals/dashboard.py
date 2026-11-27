from collections.abc import Sequence
from dataclasses import dataclass

import interactions as ipy

from elo_bot.config.emojis import IMPORTANT_EMOJI, REFRESH_EMOJI, TOKEN_EMOJI
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.core.finals import FINAL_PICK_COUNT, FINAL_SEATS, NODE_IDS, ROUND_NODES
from elo_bot.db import EloFinal
from elo_bot.db.final_invitation_models import FinalInvitation
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.mentions import mention_user
from elo_bot.features.finals.bracket import lineup_confirmed, seeded_players
from elo_bot.features.finals.opponent_picks import pick_prompt_key, pick_turn
from elo_bot.features.finals.replacement import replaceable_entrants
from elo_bot.features.finals.labels import market_label, match_line, phase_label, prediction_label
from elo_bot.features.tokens.presentation import market_signature


# Shown above the current dashboard when a control was drawn for an earlier state of the Final.
FINAL_CHANGED = f"{IMPORTANT_EMOJI} The Final changed since this view opened."


@dataclass(frozen=True, slots=True)
class DashboardState:
    final: EloFinal
    markets: Sequence[FinalBettingMarket] = ()
    submitted: int = 0
    invitations: Sequence[FinalInvitation] = ()


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
                  or market.announcement_signature != market_signature(market, final)):
                return name
        return None
    return None


def attention(state: DashboardState) -> list[str]:
    final = state.final
    items = []
    if (final.active and final.state not in {"CANCELLING", "CANCELLED"}
            and lineup_confirmed(final) and not final.lineup_message_id):
        items.append(f"{IMPORTANT_EMOJI} Lineup publication pending.")
    for node in final.nodes.values():
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
        if 5 <= len(final.entrant_ids) <= FINAL_SEATS:
            if final.opponent_picks is not None and not lineup_confirmed(final):
                actions.append(("lineup", "Confirm Lineup"))
            else:
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


def registering(final: EloFinal) -> bool:
    return final.active and final.state == "REGISTRATION"


def dashboard_controls(state: DashboardState, owner: str) -> list[ipy.ActionRow]:
    final = state.final
    buttons = []
    for action, label in available_actions(state):
        custom_id = f"finaldash|{action}|{final.id}|{owner}"
        if action == "lineup":
            custom_id += f"|{final.setup_revision}"
        buttons.append(ipy.Button(label=label, style=ipy.ButtonStyle.SECONDARY, custom_id=custom_id))
    if registering(final) and len(final.entrant_ids) < FINAL_SEATS:
        buttons += [ipy.Button(label=label, style=ipy.ButtonStyle.SECONDARY,
                               custom_id=f"finalroster|{action}|{final.id}|{owner}")
                    for action, label in (("add", "Add"), ("invite", "Invite"))]
    elif replaceable_entrants(final):
        buttons.append(ipy.Button(label="Replace Player", style=ipy.ButtonStyle.SECONDARY,
                                  custom_id=f"finalroster|replace|{final.id}|{owner}"))
    if buttons:
        buttons[0].style = ipy.ButtonStyle.PRIMARY
    if final.active:
        buttons.append(ipy.Button(emoji=REFRESH_EMOJI, style=ipy.ButtonStyle.SECONDARY,
                                  custom_id=f"finaldash|refresh|{final.id}|{owner}"))
    rows = [ipy.ActionRow(*buttons[index:index + 5]) for index in range(0, len(buttons), 5)]
    if registering(final) and state.invitations:
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(
            *[ipy.StringSelectOption(label=invitation.display_name[:100], value=str(invitation.id))
              for invitation in state.invitations[:25]],
            custom_id=f"finalinvwithdraw|{final.id}|{owner}", placeholder="🗑️ Withdraw an invitation")))
    return rows


def dashboard_embed(state: DashboardState, names: dict[str, str], *, manager: bool) -> ipy.Embed:
    final = state.final
    phase = phase_label(final)
    description = phase
    if not final.nodes:
        description += f" · {len(final.entrant_ids)} of 8 entrants"
    elif phase in ROUND_NODES:
        playable = [final.nodes[key] for key in ROUND_NODES[phase] if not final.nodes[key].bye]
        description += f" · {sum(bool(node.winner) for node in playable)} of {len(playable)} decided"
    embed = ipy.Embed(title=f"**Season {final.season_number} Final**", description=description, color=embed_color())
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
    if manager and registering(final) and state.invitations:
        fields.append(("Pending Invitations", [f"{names.get(invitation.user_id, mention_user(None, invitation.user_id))}"
                                               f" · expires <t:{invitation.expires_at}:R>"
                                               for invitation in state.invitations]))
    if manager and final.guild_id and final.channel_id:
        channel_url = f"https://discord.com/channels/{final.guild_id}/{final.channel_id}"
        fields.append(("Announcements", [f"[{label}]({channel_url}/{message_id})"
            for label, message_id in (("Lineup", final.lineup_message_id), ("Bracket", final.bracket_message_id))
            if message_id]))
    if (manager and registering(final) and (picks := final.opponent_picks)
            and picks.turns and lineup_confirmed(final)):
        count = sum(turn.opponent is not None for turn in picks.turns)
        progress = f"{count} of {FINAL_PICK_COUNT} complete"
        if (index := pick_turn(final)) is not None:
            turn = picks.turns[index]
            progress += f" · #{index + 1} · <t:{turn.deadline}:R>"
            prompt = final.opponent_pick_prompts.get(pick_prompt_key(picks.generation, index))
            if prompt and prompt.dm_unavailable:
                progress += " · DM unavailable"
        elif picks.closed and count < FINAL_PICK_COUNT:
            progress = f"Closed · {progress}"
        fields.append(("Opponent Picks", [progress]))
    fields.append(("Predictions", [f"{prediction_label(final.prediction_state)} · {state.submitted:,} submitted"]))
    if state.markets:
        fields.append(("Betting", [f"{market.node_id} · {market_label(market)} · "
                                   f"{sum(market.pools.values()):,} {TOKEN_EMOJI}" for market in state.markets]))
    if manager:
        fields.append(("Needs attention", attention(state)))
        actions = available_actions(state)
        if registering(final) and len(final.entrant_ids) < 5:
            missing = 5 - len(final.entrant_ids)
            fields.append(("Next step", [f"Add or invite at least {missing} more player{'s' if missing > 1 else ''}."]))
        elif registering(final) and len(final.entrant_ids) < FINAL_SEATS and (final.opponent_picks is None
                                                                              or lineup_confirmed(final)):
            fields.append(("Next step", ["Create Bracket, or add or invite more players."]))
        elif actions:
            fields.append(("Next step", [actions[0][1] + "."]))
    for title, lines in fields:
        while lines:
            value = lines.pop(0)
            while lines and len(value) + len(lines[0]) + 1 <= 1024:
                value += "\n" + lines.pop(0)
            embed.add_field(name=f"**{title}**", value=value, inline=False)
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    return embed

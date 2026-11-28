from collections.abc import Iterable, Sequence
from typing import assert_never

import interactions as ipy
from beanie import PydanticObjectId

from elo_bot.config.emojis import LOSS_EMOJI, WIN_EMOJI
from elo_bot.core.finals import ROUND_NODES, PredictionState, is_final_match
from elo_bot.db import EloFinal, EloFinalMatch, Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.mentions import fetch_members, mention_user


def final_nickname(final: EloFinal, user_id: str) -> str | None:
    return (final.player_nicknames.get(user_id) or None) if user_id in final.nickname_overrides else None


def final_player_link(final: EloFinal, user_id: str, member: ipy.User | ipy.Member | None = None) -> str:
    return mention_user(member, user_id, display_name=final_nickname(final, user_id))


async def final_member_names(final: EloFinal, guild: ipy.Guild, user_ids: Iterable[str]) -> dict[str, str]:
    members = await fetch_members(guild, user_ids)
    return {user: final_player_link(final, user, member) for user, member in members.items()}


async def match_player_links(match: Match, members: Sequence[ipy.User | ipy.Member | None]) -> list[str]:
    final = await EloFinal.get(PydanticObjectId(match.final_id)) if is_final_match(match) and match.final_id else None
    return [final_player_link(final, player.user_id, member) if final else mention_user(member, player.user_id)
            for player, member in zip(match.participants, members, strict=True)]


def phase_label(final: EloFinal) -> str:
    match final.state:
        case "REGISTRATION":
            return "Setup"
        case "BRACKET_READY" | "RUNNING":
            if final.champion:
                return "Champion decided"
            if (not any(node.thread_id or node.provisioning or node.result_id for node in final.nodes.values())
                    and final.first_actual_started_at is None):
                return "Bracket ready"
            return next((round_name for round_name, ids in ROUND_NODES.items()
                         if any(key in final.nodes and not final.nodes[key].bye
                                and not final.nodes[key].winner for key in ids)), "Final")
        case "COMPLETED":
            return "Ended"
        case "CANCELLING":
            return "Cancelling"
        case "CANCELLED":
            return "Cancelled"
        case unreachable:
            assert_never(unreachable)


def prediction_label(state: PredictionState) -> str:
    match state:
        case "NEVER_OPENED":
            return "Not opened"
        case "OPEN":
            return "Open"
        case "CLOSED":
            return "Closed"
        case unreachable:
            assert_never(unreachable)


def match_status(node: EloFinalMatch) -> str:
    match node.state:
        case "WAITING" | "DECIDED" | "BYE":
            return ""
        case "READY":
            times = [time for time in node.scheduled_times if time is not None]
            if times:
                return " / ".join(ipy.Timestamp.fromtimestamp(time).format(ipy.TimestampStyles.LongDateTime)
                                  for time in dict.fromkeys(times))
            return "Not scheduled" if node.thread_id else "Not started"
        case "PLAYING":
            return "Live"
        case "NEEDS_MANAGER":
            return "Awaiting a manager decision"
        case unreachable:
            assert_never(unreachable)


def pairing_label(final: EloFinal, node: EloFinalMatch) -> str:
    return f"{node.id} · " + " vs. ".join(
        final.player_nicknames.get(user, user) if user else "TBD" for user in node.contestants)


def thread_label(final: EloFinal, node_id: str, players: Sequence[ipy.User]) -> str:
    names = " vs. ".join(final_nickname(final, str(player.id)) or str(player) for player in players)
    return f"{node_id} · {names}".replace("@", "")[:98]


def matchup(contestants: Sequence[str | None], winner: str | None,
            names: dict[str, str]) -> str:
    players = []
    for user in contestants:
        name = names.get(user, mention_user(None, user)) if user else "TBD"
        if winner is not None and user is not None:
            name += f" {WIN_EMOJI if user == winner else LOSS_EMOJI}"
        players.append(name)
    return " vs. ".join(players)


def match_line(node: EloFinalMatch, names: dict[str, str]) -> str:
    if node.bye:
        user = next(user for user in node.contestants if user is not None)
        return f"`{node.id}` {names.get(user, mention_user(None, user))} · bye"
    players = "TBD" if not any(node.contestants) else matchup(node.contestants, node.winner, names)
    status = match_status(node)
    if node.outcome == "Forfeit":
        status = "Forfeit"
    return f"`{node.id}` {players}" + (f" · {status}" if status else "")


def market_label(market: FinalBettingMarket) -> str:
    match market.state:
        case "OPEN":
            return "Open"
        case "CLOSED":
            return "Closed"
        case "SETTLING" | "CORRECTING":
            return "Paying out"
        case "PAID":
            return "Paid"
        case "REFUNDED":
            return "Refunded"
        case unreachable:
            assert_never(unreachable)

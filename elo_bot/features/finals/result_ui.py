import interactions as ipy
from typing import Final, assert_never

from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elo_bot.config.emojis import CROSS_EMOJI, STAR_EMOJI, TICK_EMOJI, WIN_EMOJI
from elo_bot.config.ids import ELO_BLACKLIST_ROLE, LVL1_ROLES, LVL2_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db import Match
from elo_bot.core.finals import FINAL_DISTRICTS, Outcome
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.discord.mentions import mention_user
from elo_bot.db import EloFinal, EloFinalMatch
from elo_bot.db.final_draft_models import FinalResultDraft
from elo_bot.db.final_models import FinalMatchResult, FinalResultSnapshot
from elo_bot.features.finals.labels import matchup
from elo_bot.features.finals.matches import require_ongoing_match

SCORE_PLACEHOLDER: Final = "BC, BL, BW, DC, GQ, SP, GM · one per line, e.g. 2.83"


def require_result_role(ctx: ipy.InteractionContext, needs_manager: bool) -> None:
    roles = LVL1_ROLES if needs_manager else LVL2_ROLES
    if any(ctx.author.has_role(role) for role in roles):
        return
    if needs_manager:
        raise FinalError("This match already has a result. A manager can correct it with `/match resubmit`.")
    raise ipy.errors.CommandCheckFailure(ctx.command, ipy.has_any_role(*roles), ctx)


def result_form(final: EloFinal, node: EloFinalMatch, owner: str, previous: FinalMatchResult | None = None,
                *, winner: str | None = None, outcome: Outcome = "Played", reason: str | None = None,
                stored: dict[str, list[str]] | None = None) -> ipy.Modal:
    custom_id = f"finalres|{final.id}|{node.id}|{owner}|{node.result_version}|" \
                f"{node.contestant_revision}|{node.attempt}|{winner or '-'}|{int(outcome == 'Forfeit')}"
    fields = []
    for side, user in enumerate(node.contestants):
        if outcome == "Forfeit":
            value = "\n".join(["0.00"] * 7)
        elif previous:
            value = "\n".join(previous.snapshot.scores[user])
        elif stored and user in stored:
            value = "\n".join(stored[user])
        else:
            value = None
        fields.append(ipy.ParagraphText(label=f"{final.player_nicknames[user][:35]}'s scores",
                                       custom_id=f"player{side + 1}", value=value, max_length=100,
                                       placeholder=SCORE_PLACEHOLDER))
    if previous or winner is not None or outcome == "Forfeit":
        fields.append(ipy.ShortText(label="Reason", custom_id="reason", value=reason, max_length=300, required=True))
    return ipy.Modal(*fields, title=f"Season {final.season_number} Final · {node.id}", custom_id=custom_id)


def preview_controls(final: EloFinal, draft: FinalResultDraft) -> list[ipy.ActionRow]:
    buttons = ipy.ActionRow(
        ipy.Button(label="Cancel", emoji=CROSS_EMOJI, style=ipy.ButtonStyle.DANGER, custom_id=f"finalcancel|{draft.id}"),
        ipy.Button(label="Confirm", emoji=TICK_EMOJI, style=ipy.ButtonStyle.SUCCESS, custom_id=f"finalconfirm|{draft.id}"),
    )
    if draft.snapshot.outcome == "Forfeit":
        return [buttons]
    options = [ipy.StringSelectOption(label=f"{final.player_nicknames[user]} · {district}"[:100], value=f"{side}|{index}",
                                      description=f"{get_stars(score)}★ {get_destruction(score)}%")
               for side, user in enumerate(draft.snapshot.contestants)
               for index, (district, score) in enumerate(zip(FINAL_DISTRICTS, draft.snapshot.scores[user]))]
    return [ipy.ActionRow(ipy.StringSelectMenu(*options, placeholder="Edit a score", custom_id=f"finaledit|{draft.id}")),
            buttons]


def result_embed(final: EloFinal, result: FinalMatchResult, names: dict[str, str] | None = None,
                 *, scoreboard: bool = False) -> ipy.Embed:
    return score_embed(final, result.node_id, result.snapshot, names, scoreboard=scoreboard)


def score_embed(final: EloFinal, node_id: str, snapshot: FinalResultSnapshot,
                names: dict[str, str] | None = None, *, scoreboard: bool = False) -> ipy.Embed:
    """With an attached scoreboard image the embed omits the district and total scores it already shows."""
    names = names or {}
    left, right = snapshot.contestants
    lines = []
    for index, district in enumerate(FINAL_DISTRICTS):
        scores = [snapshot.scores[user][index] for user in (left, right)]
        lines.append(f"**{district}** · {get_stars(scores[0])}{STAR_EMOJI} {get_destruction(scores[0])}% — "
                     f"{get_stars(scores[1])}{STAR_EMOJI} {get_destruction(scores[1])}%")
    totals = [f"{sum(get_stars(score) for score in snapshot.scores[user])}{STAR_EMOJI} "
              f"{sum(get_destruction(score) for score in snapshot.scores[user])}%" for user in (left, right)]
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · {node_id}**", color=COLOR,
                      description=matchup(snapshot.contestants, snapshot.winner, names))
    match snapshot.outcome:
        case "Forfeit":
            embed.description = " vs. ".join(names.get(user, mention_user(None, user))
                + (f" {WIN_EMOJI}" if user == snapshot.winner else "") for user in snapshot.contestants) + " · Forfeit"
        case "Played":
            if not scoreboard:
                embed.description += "\n\n" + "\n".join(lines)
                embed.add_field(name="**Total**", value=" — ".join(totals))
        case unreachable:
            assert_never(unreachable)
    if final.repair_pending:
        embed.add_field(name="**Recovery**", value="Bracket and payout updates are pending.")
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


async def require_source_result_role(ctx: ipy.InteractionContext, source: Match) -> EloFinal:
    final = await require_ongoing_match(source)
    if ctx.guild_id != final.guild_id:
        raise FinalError("Use this command in the Final's server.")
    if ctx.author.has_role(ELO_BLACKLIST_ROLE):
        raise FinalError("You are banned from using Elo commands. Please contact the staff team if you have doubts.")
    node = final.nodes[source.final_node_id]
    require_result_role(ctx, node.result_id is not None)
    return final


async def show_source_result_form(ctx, source: Match) -> None:
    final = await require_source_result_role(ctx, source)
    node = final.nodes[source.final_node_id]
    previous = await FinalMatchResult.get(node.result_id) if node.result_id else None
    stored = {participant.user_id: list(participant.result) for participant in source.participants
              if all(participant.result)}
    await ctx.send_modal(result_form(final, node, str(ctx.author.id), previous, stored=stored))

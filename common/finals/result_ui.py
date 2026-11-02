import interactions as ipy

from common.constants import COLOR, FOOTER_IMAGE, FOOTER_TEXT, STAR_EMOJI, ELO_BLACKLIST_ROLE, LVL1_ROLES, LVL2_ROLES
from common.finals.types import FinalError
from beanie import PydanticObjectId
from database_models import Match
from common.finals.types import FINAL_DISTRICTS, Outcome
from common.utils import get_destruction, get_stars, mention_user
from database_models import EloFinal, EloFinalMatch
from database_models.final_models import FinalMatchResult, FinalResultSnapshot


def result_form(final: EloFinal, node: EloFinalMatch, owner: str, previous: FinalMatchResult | None = None,
                *, winner: str | None = None, outcome: Outcome = "Played", reason: str | None = None) -> ipy.Modal:
    custom_id = f"finalres|{final.season_number}|{node.id}|{owner}|{node.result_version}|" \
                f"{node.contestant_revision}|{node.attempt}|{winner or '-'}|{int(outcome == 'Forfeit')}"
    fields = []
    for side, user in enumerate(node.contestants):
        value = "\n".join(["0.00"] * 7) if outcome == "Forfeit" else (
            "\n".join(previous.snapshot.scores[user]) if previous else None
        )
        fields.append(ipy.ParagraphText(label=f"{final.player_nicknames[user][:35]}'s result",
                                       custom_id=f"player{side + 1}", value=value, max_length=100,
                                       placeholder="7 lines in district order, e.g. 2.83"))
    if previous or winner is not None or outcome == "Forfeit":
        fields.append(ipy.ShortText(label="Reason", custom_id="reason", value=reason, max_length=300, required=True))
    return ipy.Modal(*fields, title=f"Season {final.season_number} Final · {node.id}", custom_id=custom_id)


def result_embed(final: EloFinal, result: FinalMatchResult, names: dict[str, str] | None = None) -> ipy.Embed:
    return score_embed(final, result.node_id, result.snapshot, names)


def score_embed(final: EloFinal, node_id: str, snapshot: FinalResultSnapshot,
                names: dict[str, str] | None = None) -> ipy.Embed:
    names = names or {}
    left, right = snapshot.contestants
    lines = []
    for index, district in enumerate(FINAL_DISTRICTS):
        scores = [snapshot.scores[user][index] for user in (left, right)]
        lines.append(f"**{district}**\n{get_stars(scores[0])}{STAR_EMOJI} {get_destruction(scores[0])}% — "
                     f"{get_stars(scores[1])}{STAR_EMOJI} {get_destruction(scores[1])}%")
    totals = [f"{sum(get_stars(score) for score in snapshot.scores[user])}{STAR_EMOJI} "
              f"{sum(get_destruction(score) for score in snapshot.scores[user])}%" for user in (left, right)]
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · {node_id}**", color=COLOR,
                      description=f"{names.get(left, mention_user(None, left))} vs. "
                                  f"{names.get(right, mention_user(None, right))}\n\n" + "\n\n".join(lines))
    embed.add_field(name="**Total**", value=" — ".join(totals))
    if snapshot.outcome == "Forfeit":
        embed.add_field(name="**Forfeit**",
                        value=f"{names.get(snapshot.winner, mention_user(None, snapshot.winner))} wins")
    elif snapshot.winner is None:
        embed.add_field(name="**Tie**", value="A manager must choose the winner.")
    elif totals[0] == totals[1]:
        embed.add_field(name="**Winner**", value=names.get(snapshot.winner, mention_user(None, snapshot.winner)))
    if final.repair_pending:
        embed.add_field(name="**Recovery**", value="Bracket and payout updates are pending.")
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    return embed


async def show_source_result_form(ctx, source: Match, *, correction: bool = False) -> None:
    final = await EloFinal.get(PydanticObjectId(source.final_id))
    if final is None or ctx.guild_id != final.guild_id or ctx.author.has_role(ELO_BLACKLIST_ROLE):
        raise FinalError("Use the current Final's server and result controls.")
    roles = LVL1_ROLES if correction else LVL2_ROLES
    if not any(ctx.author.has_role(role) for role in roles):
        raise FinalError("A manager must correct results; staff may submit an initial result.")
    node = final.nodes[source.final_node_id]
    if source.final_superseded or node.match_id != source.id or node.contestant_revision != source.final_contestant_revision:
        raise FinalError("This physical match was superseded. Use `/final match result` for the current pairing.")
    previous = await FinalMatchResult.get(node.result_id) if node.result_id else None
    await ctx.send_modal(result_form(final, node, str(ctx.author.id), previous))

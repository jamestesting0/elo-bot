import interactions as ipy

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.core.finals import ROUND_NODES
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.mentions import mention_user
from elo_bot.features.finals.bracket import seeded_players
from elo_bot.features.finals.embeds import final_embed
from elo_bot.features.finals.labels import match_line, pairing_label, prediction_label


def bracket_embed(final: EloFinal, names: dict[str, str] | None = None, submitted: int = 0) -> ipy.Embed:
    names = names or {}
    embed = final_embed(final.season_number, "Bracket" if final.nodes else "Entrants")
    if not final.nodes:
        embed.description = (f"{len(final.entrant_ids)} of 8 entrants\nThe bracket hasn't been created yet.\n\n"
                             + "\n".join(f"{index}. {names.get(user, mention_user(None, user))}"
                                         for index, user in enumerate(seeded_players(final), 1)))
    else:
        if final.state in {"CANCELLING", "CANCELLED"}:
            embed.description = "**Cancelled.**"
        elif final.champion:
            embed.description = f"**Champion:** {names.get(final.champion, mention_user(None, final.champion))}"
        for round_name, ids in ROUND_NODES.items():
            lines = [match_line(final.nodes[key], names) for key in ids if key in final.nodes]
            while lines:
                value = lines.pop(0)
                while lines and len(value) + len(lines[0]) + 1 <= 1024:
                    value += "\n" + lines.pop(0)
                embed.add_field(name=f"**{round_name}**", value=value, inline=False)
        if final.predictions_ever_opened:
            embed.add_field(name="**Predictions**",
                            value=f"{prediction_label(final.prediction_state)} · {submitted:,} submitted", inline=False)
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    return embed


async def bracket_controls(final: EloFinal) -> list[ipy.ActionRow]:
    controls = []
    if final.active and final.prediction_state == "OPEN":
        controls.append(ipy.ActionRow(ipy.Button(label="Predict", emoji="🔮", style=ipy.ButtonStyle.PRIMARY,
                                                 custom_id=f"finalpredict|{final.id}")))
    nodes = [node for node in final.nodes.values()
             if not node.bye and node.result_id and node.winner]
    if not nodes:
        return controls
    results = await FinalMatchResult.find({"_id": {"$in": [node.result_id for node in nodes]}}).to_list()
    by_id = {result.id: result for result in results}
    options = []
    for node in nodes:
        result = by_id.get(node.result_id)
        if result is None:
            continue
        winner = final.player_nicknames.get(node.winner, node.winner)
        summary = f"{winner} won"
        if result.snapshot.outcome == "Forfeit":
            summary += " · Forfeit"
        else:
            totals = [f"{sum(get_stars(score) for score in result.snapshot.scores[user])}⭐ "
                      f"{sum(get_destruction(score) for score in result.snapshot.scores[user])}%"
                      for user in result.snapshot.contestants]
            summary += " · " + " — ".join(totals)
        options.append(ipy.StringSelectOption(label=pairing_label(final, node)[:100],
                                             value=node.id, description=summary[:100]))
    if options:
        controls.append(ipy.ActionRow(ipy.StringSelectMenu(*options, custom_id=f"finalresult|{final.id}",
                                                          placeholder="🔍 View a match result")))
    return controls

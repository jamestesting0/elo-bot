import interactions as ipy

from elo_bot.config.presentation import COLOR, FOOTER_TEXT
from elo_bot.features.finals.predictions import pick_options
from elo_bot.core.finals import NODE_IDS
from elo_bot.discord.mentions import mention_user
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalPrediction


def prediction_view(final: EloFinal, prediction: FinalPrediction, names: dict[str, str] | None = None):
    player_names = names or {}
    stage = prediction.stage
    prefix = f"finalpred|{final.season_number}|{prediction.user_id}|{prediction.session_revision}|"
    names = ("Quarterfinals", "Semifinals", "Champion", "Review")
    content = [ipy.TextDisplayComponent(content=f"## Season {final.season_number} Final · Prediction\n"
                                               f"{names[stage]} · {stage + 1}/4 · Draft")]
    nodes = (NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:], NODE_IDS)[stage]
    for node_id in nodes:
        node = final.nodes[node_id]
        if stage == 3 or node.bye:
            picked = prediction.draft.get(node_id)
            content.append(ipy.TextDisplayComponent(content=f"{node_id}: " + (
                player_names.get(picked, mention_user(None, picked)) if picked else "TBD")
                                                    + (" · BYE" if node.bye else "")))
            continue
        choices = pick_options(final, prediction.draft, node_id)
        options = [ipy.StringSelectOption(label=final.player_nicknames[user][:100], value=user,
                                         default=prediction.draft.get(node_id) == user) for user in choices]
        if options:
            content.append(ipy.ActionRow(ipy.StringSelectMenu(*options, placeholder=f"{node_id} winner",
                                                              custom_id=prefix + f"pick|{node_id}")))
    content.append(ipy.ActionRow(
        ipy.Button(label="Back", style=ipy.ButtonStyle.SECONDARY, disabled=stage == 0, custom_id=prefix + "back|-"),
        ipy.Button(label="Next", style=ipy.ButtonStyle.PRIMARY,
                   disabled=stage == 3 or any(node not in prediction.draft for node in nodes),
                   custom_id=prefix + "next|-"),
        ipy.Button(label="Submit", style=ipy.ButtonStyle.SUCCESS, disabled=stage != 3 or final.prediction_state != "OPEN",
                   custom_id=prefix + "submit|-"),
    ))
    content.append(ipy.TextDisplayComponent(content=f"-# {FOOTER_TEXT}"))
    return [ipy.ContainerComponent(*content, accent_color=COLOR)]

import interactions as ipy

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.features.finals.embeds import final_embed
from elo_bot.features.finals.predictions import pick_options
from elo_bot.core.finals import NODE_IDS
from elo_bot.discord.mentions import mention_user
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalPrediction
from elo_bot.config.emojis import SUCCESS_EMOJI


def prediction_view(final: EloFinal, prediction: FinalPrediction, names: dict[str, str] | None = None,
                    *, submitted: bool = False, outcome: str = "",
                    image_available: bool = True) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    player_names = names or {}
    stage = prediction.stage
    prefix = f"finalpred|{final.id}|{prediction.user_id}|{prediction.session_revision}|"
    stages = ("Quarterfinals", "Semifinals", "Champion", "Review")
    if submitted:
        description = f"{SUCCESS_EMOJI} Prediction submitted. You can change it until predictions close."
    else:
        description = f"{stages[stage]} · {stage + 1}/4"
        if prediction.submitted_at is not None:
            description += " · Editing your submitted picks"
    if outcome:
        description += f"\n{outcome}"
    embed = final_embed(final.season_number, "Prediction")
    rows = []
    nodes = (NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:], NODE_IDS)[stage]
    if stage == 3 or submitted:
        if image_available:
            embed.set_image(url="attachment://prediction.png")
        picks = prediction.submitted if submitted else prediction.draft
        description += "\n\n" + "\n".join(
            f"{'🏆' if key == 'F1' else f'`{key}`'} {player_names.get(picks[key], mention_user(None, picks[key]))}"
            for key in NODE_IDS if key in picks and not final.nodes[key].bye)
    else:
        lines = []
        for node_id in nodes:
            node = final.nodes[node_id]
            choices = pick_options(final, prediction.draft, node_id)
            pairing = " vs. ".join(player_names.get(user, mention_user(None, user)) for user in choices) or "TBD"
            lines.append(f"**{node_id}** · {pairing}" + (" · bye" if node.bye else ""))
            if node.bye:
                continue
            options = [ipy.StringSelectOption(label=final.player_nicknames[user][:100], value=user,
                description=f"{final.seed_elo[user]} Elo" if final.seed_elo.get(user) is not None else None,
                default=prediction.draft.get(node_id) == user) for user in choices]
            if options:
                rows.append(ipy.ActionRow(ipy.StringSelectMenu(*options, placeholder=f"🏆 Choose the {node_id} winner",
                                                               custom_id=prefix + f"pick|{node_id}")))
        description += "\n\n" + "\n".join(lines)
    embed.description = description
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    if submitted:
        rows.append(ipy.ActionRow(ipy.Button(label="Edit Picks", style=ipy.ButtonStyle.SECONDARY,
                                             custom_id=prefix + "edit|-")))
    else:
        buttons = [ipy.Button(label="Back", style=ipy.ButtonStyle.SECONDARY, disabled=stage == 0,
                              custom_id=prefix + "back|-")]
        if stage != 3:
            buttons.append(ipy.Button(label="Next", style=ipy.ButtonStyle.PRIMARY,
                disabled=any(node not in prediction.draft for node in nodes), custom_id=prefix + "next|-"))
        else:
            buttons.append(ipy.Button(label="Submit", style=ipy.ButtonStyle.SUCCESS,
                disabled=final.prediction_state != "OPEN", custom_id=prefix + "submit|-"))
        rows.append(ipy.ActionRow(*buttons))
    return embed, rows

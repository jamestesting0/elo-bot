from dataclasses import dataclass

import anyio
import interactions as ipy

from elo_bot.config.presentation import COLOR, FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult, FinalPrediction
from elo_bot.discord.mentions import fetch_members, member_names, mention_user
from elo_bot.features.finals.labels import prediction_label
from elo_bot.features.finals.prediction_display import (
    finished_results,
    prediction_alt_text,
    prediction_statuses,
)
from elo_bot.features.finals.rendering import render_bracket

PAGE_SIZE = 25


@dataclass(frozen=True, slots=True)
class PredictionCard:
    embed: ipy.Embed
    image: bytes | None
    alt_text: str


def predictions_visible(final: EloFinal, manager: bool) -> bool:
    """Everyone sees every prediction once they close; managers see them while they are open too."""
    return manager or predictions_public(final)


def predictions_public(final: EloFinal) -> bool:
    return final.prediction_state == "CLOSED" or not final.active


def prediction_score(final: EloFinal, prediction: FinalPrediction) -> tuple[int, int]:
    if prediction.scoring_revision == final.result_revision:
        return prediction.points, prediction.eligible
    statuses = prediction_statuses(final, prediction)
    eligible = sum(not node.bye and node.outcome == "Played" and node.winner is not None
                   and node.result_id is not None for node in final.nodes.values())
    return sum(status == "correct" for status in statuses.values()), eligible


async def submitted_prediction(final: EloFinal, user_id: str) -> FinalPrediction | None:
    return await FinalPrediction.find_one({"final_id": str(final.id), "user_id": user_id,
                                           "submitted_at": {"$ne": None}})


async def prediction_board(final: EloFinal, guild: ipy.Guild, owner: str,
                           page: int) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    predictions = await FinalPrediction.find({"final_id": str(final.id), "submitted_at": {"$ne": None}}).to_list()
    scores = {prediction.user_id: prediction_score(final, prediction) for prediction in predictions}
    predictions.sort(key=lambda prediction: (-scores[prediction.user_id][0], prediction.submitted_at))
    pages = max(1, -(-len(predictions) // PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    shown = predictions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    members = await fetch_members(guild, [prediction.user_id for prediction in shown])
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Predictions**", color=COLOR,
                      description=f"{prediction_label(final.prediction_state)} · {len(predictions):,} submitted")
    lines, options = [], []
    for rank, prediction in enumerate(shown, page * PAGE_SIZE + 1):
        user = prediction.user_id
        points, eligible = scores[user]
        champion = prediction.submitted.get("F1")
        pick = final.player_nicknames.get(champion, champion) if champion else None
        line = f"{rank}. {mention_user(members[user], user)}"
        if pick:
            line += f" · 🏆 {pick}"
        if eligible:
            line += f" · `{points}/{eligible}`"
        lines.append(line)
        label = members[user].display_name if members[user] else final.player_nicknames.get(user, user)
        options.append(ipy.StringSelectOption(label=label[:100], value=user,
                                              description=f"🏆 {pick}"[:100] if pick else None))
    if lines:
        embed.description += "\n\n" + "\n".join(lines)
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    rows = []
    if options:
        rows.append(ipy.ActionRow(ipy.StringSelectMenu(*options, custom_id=f"finalpredpick|{final.season_number}",
                                                       placeholder="🔍 View a player's picks")))
    if pages > 1:
        rows.append(ipy.ActionRow(
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="◀️️", disabled=page == 0,
                       custom_id=f"finalpredpage|{final.season_number}|{owner}|{page - 1}"),
            ipy.Button(style=ipy.ButtonStyle.SECONDARY, label=f"{page + 1}/{pages}", disabled=True,
                       custom_id=f"finalpredpage|{final.season_number}|{owner}|{page}|label"),
            ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="▶️", disabled=page == pages - 1,
                       custom_id=f"finalpredpage|{final.season_number}|{owner}|{page + 1}"),
        ))
    return embed, rows


async def prediction_card(final: EloFinal, prediction: FinalPrediction, guild: ipy.Guild,
                          *, show_owner: bool) -> PredictionCard:
    statuses = prediction_statuses(final, prediction)
    points, eligible = prediction_score(final, prediction)
    result_ids = [node.result_id for node in final.nodes.values() if node.result_id]
    results = await FinalMatchResult.find({"_id": {"$in": result_ids}}).to_list() if result_ids else []
    starts = {result.id: started for result in results
              if result.snapshot.outcome == "Played"
              and (started := getattr(result.snapshot, "actual_started_at", None)) is not None}
    user_ids = {user for result in results for user in result.snapshot.contestants}
    if show_owner:
        user_ids.add(prediction.user_id)
    names = await member_names(guild, user_ids)
    result_text = finished_results(final, results, names, starts)
    parts = [names[prediction.user_id]] if show_owner else []
    if result_text:
        parts.append(f"**Results**\n{result_text}")
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Prediction**", color=COLOR,
                      description="\n\n".join(parts) or None)
    if eligible:
        embed.add_field(name="**Predictions**", value=f"`{points}/{eligible}`")
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    try:
        image = await anyio.to_thread.run_sync(render_bracket, final, prediction.submitted, statuses)
    except FinalError as error:
        embed.add_field(name="**Image**", value=str(error))
        embed.add_field(name="**Picks**", value="\n".join(
            f"{node}: {final.player_nicknames.get(user, user)} · {statuses.get(node, 'pending')}"
            for node, user in prediction.submitted.items()))
        return PredictionCard(embed, None, "")
    embed.set_image(url="attachment://prediction.png")
    return PredictionCard(embed, image, prediction_alt_text(final, prediction, statuses))




import interactions as ipy

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.config.emojis import STAR_EMOJI, WIN_EMOJI, LOSS_EMOJI
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.features.tokens.stats import betting_lines, betting_stats
from elo_bot.features.tokens.wallets import ensure_wallet
from elo_bot.db import EloFinal, Player
from elo_bot.db.final_models import FinalEditionStats, FinalMatchResult, FinalPlayerProfile, FinalPrediction
from elo_bot.db.token_models import TokenWallet
from elo_bot.discord.mentions import member_names


async def profile_view(final: EloFinal, user, owner: str, *, guild: ipy.Guild):
    user_id = str(user.id)
    player = await Player.find_one({"user_id": user_id})
    wallet = await ensure_wallet(user_id) if player else await TokenWallet.find_one({"user_id": user_id})
    profile = await FinalPlayerProfile.find_one({"user_id": user_id})
    if profile is None and player:
        await FinalPlayerProfile.get_pymongo_collection().update_one({"user_id": user_id}, {"$setOnInsert": {
            "_id": user_id, "user_id": user_id, "player_id": player.id, "editions": {}}}, upsert=True)
        profile = await FinalPlayerProfile.find_one({"user_id": user_id})
    stats = profile.editions.get(str(final.id), FinalEditionStats(season_number=final.season_number)) if profile else \
            FinalEditionStats(season_number=final.season_number)
    claims = wallet.identity_claims if wallet else [user_id]
    prediction = await FinalPrediction.find_one({"final_id": str(final.id), "user_id": user_id})
    embed = ipy.Embed(title=f"**{user}'s Final Profile**", color=embed_color(),
        description=(f"[{player.tag}](https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23{player.tag.lstrip('#')})\n" if player else "")
        + f"Season {final.season_number} Final\n"
        + (stats.placement or ("Entrant" if set(claims) & set(final.entrant_ids) else "No participation in this Final")))
    embed.add_field(name="__**Statistics**__", value=f"**Wins:** {stats.wins}\n**Losses:** {stats.losses}\n"
                    f"**Avg. Stars:** {stats.stars / stats.district_count if stats.district_count else 0:.2f}{STAR_EMOJI}\n"
                    f"**Avg. Destruction:** {stats.destruction / stats.district_count if stats.district_count else 0:.2f}%"
                    + (f"\n**Byes:** {stats.byes}" if stats.byes else "")
                    + (f"\n**Forfeits:** {stats.forfeits}" if stats.forfeits else ""))
    embed.add_field(name="__**Bets**__", value=betting_lines(
        (await betting_stats(str(final.id))).get(wallet.id) if wallet else None))
    if prediction and prediction.submitted_at is not None:
        embed.add_field(name="__**Predictions**__", value=f"`{prediction.points}/{prediction.eligible}` → "
            f"`{prediction.points / prediction.eligible * 100 if prediction.eligible else 0:.2f}%`")
    else:
        embed.add_field(name="__**Predictions**__", value="No predictions.")
    official = {node.result_id for node in final.nodes.values()}
    results = await FinalMatchResult.find({"_id": {"$in": list(official - {None})},
        "snapshot.contestants": {"$in": claims}}).sort("-created_at").limit(4).to_list()
    opponents = {contestant for result in results for contestant in result.snapshot.contestants
                 if contestant not in claims}
    names = await member_names(guild, opponents)
    recent_matches = ""
    for result in results:
        snapshot = result.snapshot
        own = next(contestant for contestant in snapshot.contestants if contestant in claims)
        opponent = next(contestant for contestant in snapshot.contestants if contestant != own)
        outcome = str(WIN_EMOJI) if snapshot.winner == own else str(LOSS_EMOJI)
        if snapshot.outcome == "Forfeit":
            outcome += " · Forfeit"
        scores, opponent_scores = snapshot.scores[own], snapshot.scores[opponent]
        recent_matches += f"vs. {names[opponent]} ({outcome})\n" \
            f"{sum(get_stars(score) for score in scores)}{STAR_EMOJI} " \
            f"{sum(get_destruction(score) for score in scores)}% - " \
            f"{sum(get_stars(score) for score in opponent_scores)}{STAR_EMOJI} " \
            f"{sum(get_destruction(score) for score in opponent_scores)}%\n"
    embed.add_field(name="__**Recent Matches**__", value=recent_matches or "No recent matches.")
    avatar = getattr(user, "avatar", None)
    if avatar:
        embed.set_thumbnail(url=avatar.url)
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    latest = {}
    for edition in await EloFinal.find({"schema_version": 2}).sort([("season_number", -1), ("_id", -1)]).to_list():
        latest.setdefault(edition.season_number, edition)
    editions = list(latest.values())[:25]
    controls = [ipy.ActionRow(ipy.StringSelectMenu(*[ipy.StringSelectOption(label=f"Season {edition.season_number} Final",
        value=str(edition.season_number), default=edition.id == final.id) for edition in editions],
        custom_id=f"finalprofile|{owner}|{user_id}", placeholder="🔍 Select a Final"))]
    return embed, controls

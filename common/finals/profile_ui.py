import asyncio

import interactions as ipy

from common.constants import COLOR, FOOTER_IMAGE, FOOTER_TEXT, STAR_EMOJI, WIN_EMOJI, LOSS_EMOJI, TOKEN_EMOJI
from common.utils import get_destruction, get_stars, mention_user
from common.tokens.wallets import ensure_wallet, wallet_summary
from database_models import EloFinal, Player
from database_models.final_models import FinalEditionStats, FinalMatchResult, FinalPlayerProfile, FinalPrediction
from database_models.token_models import TokenWallet


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
    embed = ipy.Embed(title=f"**{user}'s Final Profile**", color=COLOR,
        description=f"Season {final.season_number} Final\n" + (f"[{player.tag}](https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23{player.tag.lstrip('#')})\n" if player else "")
        + (stats.placement or ("Entrant" if set(claims) & set(final.entrant_ids) else "No participation in this Final")))
    embed.add_field(name="__**Statistics**__", value=f"**Wins:** {stats.wins}\n**Losses:** {stats.losses}\n"
                    f"**Total Matches:** {stats.played}\n"
                    f"**Avg. Stars:** {stats.stars / stats.district_count if stats.district_count else 0:.2f}{STAR_EMOJI}\n"
                    f"**Avg. Destruction:** {stats.destruction / stats.district_count if stats.district_count else 0:.2f}%"
                    + (f"\n**Byes:** {stats.byes}" if stats.byes else "")
                    + (f"\n**Forfeits:** {stats.forfeits}" if stats.forfeits else ""))
    if wallet:
        available, committed, debt = await wallet_summary(wallet)
        embed.add_field(name="__**Tokens**__", value=f"**Available:** {available:,} {TOKEN_EMOJI}\n"
                        f"**Committed:** {committed:,} {TOKEN_EMOJI}"
                        + (f"\n**Debt:** {debt:,} {TOKEN_EMOJI}" if debt else ""))
    if prediction and prediction.submitted_at is not None:
        embed.add_field(name="**Predictions**", value=f"{prediction.points}/{prediction.eligible} → "
            f"{prediction.points / prediction.eligible * 100 if prediction.eligible else 0:.2f}%")
    else:
        embed.add_field(name="**Predictions**", value="No predictions.")
    official = {node.result_id for node in final.nodes.values()}
    results = await FinalMatchResult.find({"_id": {"$in": list(official - {None})},
        "snapshot.contestants": {"$in": claims}}).sort("-created_at").limit(4).to_list()
    opponents = {contestant for result in results for contestant in result.snapshot.contestants
                 if contestant not in claims}
    members = await asyncio.gather(*(guild.fetch_member(opponent) for opponent in opponents))
    names = {opponent: mention_user(member, opponent) for opponent, member in zip(opponents, members)}
    recent_matches = ""
    for result in results:
        snapshot = result.snapshot
        own = next(contestant for contestant in snapshot.contestants if contestant in claims)
        opponent = next(contestant for contestant in snapshot.contestants if contestant != own)
        outcome = "Tie" if snapshot.winner is None else (
            f"{WIN_EMOJI}Win" if snapshot.winner == own else f"{LOSS_EMOJI}Loss")
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
    embed.set_footer(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE)
    editions = await EloFinal.find({"schema_version": 2}).sort("-season_number").limit(25).to_list()
    controls = [ipy.ActionRow(ipy.StringSelectMenu(*[ipy.StringSelectOption(label=f"Season {edition.season_number} Final",
        value=str(edition.season_number), default=edition.id == final.id) for edition in editions],
        custom_id=f"finalprofile|{owner}|{user_id}", placeholder="Select a Final")),
        ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.PRIMARY, label="Matches",
            custom_id=f"finalhist|{owner}|{final.season_number}|{user_id}|1"))]
    return embed, controls

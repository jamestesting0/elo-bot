import logging
from datetime import datetime

import interactions as ipy
from beanie.odm.operators.find.comparison import In

from elobot.config.emojis import CLASHING_SWORD_EMOJI, LOSS_EMOJI, STAR_EMOJI, WIN_EMOJI
from elobot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT
from elobot.core.elo import calculate_elo
from elobot.core.parsing import get_destruction, get_stars
from elobot.db import BaseLayout, Match, MatchDistrictStats, MatchPlayerStats, Player, RecordedMatch
from elobot.discord.members import fetch_member_otherwise_user
from elobot.discord.message_edits import edit_channel_message
from elobot.extensions.leaderboard import (
    get_avg_destruction,
    get_avg_stars,
    get_opponent_avg_destruction,
    get_opponent_avg_stars,
    get_wins,
)
from elobot.features.players.match_choices import fetch_user_match_id_choices
from elobot.features.finals.types import is_final_match
from elobot.features.hosting.self_hosted.invitations import invite_eligible_players
from elobot.features.results.manual import complete_manual_result, persist_manual_result
from elobot.features.results.operations import (
    ResultOperationConflict,
    begin_result_operation,
    completed_result_changes,
    persist_result_record,
)
from elobot.features.results.records import add_recorded_match


async def record_match(guild: ipy.Guild, bot: ipy.Client, player1_id: str, player2_id: str, response: dict[str, str],
                       final_districts: list[str], token: str = None, *, manual_key: str | None = None):
    # fetch players
    player1 = await Player.find_one(Player.user_id == player1_id, fetch_links=True)
    player2 = await Player.find_one(Player.user_id == player2_id, fetch_links=True)

    player1_results = response["player1"].split("\n")
    player1_stars = list(map(get_stars, player1_results))
    player1_total_stars = sum(player1_stars)
    player1_destruction = list(map(get_destruction, player1_results))
    player1_total_destruction = sum(player1_destruction)

    player2_results = response["player2"].split("\n")
    player2_stars = list(map(get_stars, player2_results))
    player2_total_stars = sum(player2_stars)
    player2_destruction = list(map(get_destruction, player2_results))
    player2_total_destruction = sum(player2_destruction)

    player1_elo = player1.elo
    player2_elo = player2.elo

    # check if the match is a challenge
    is_challenge = False
    match = await Match.find_one(Match.token == token)
    if token and not match:
        raise ResultOperationConflict(token, "The match no longer exists. Its result was not recorded.")
    if token and match:
        if is_final_match(match):
            raise ResultOperationConflict(token, "Final results use `/final match result`, without ranked recording.")
        is_challenge = match.is_challenge
        if match.result_operation and match.result_operation.completed:
            await begin_result_operation(match, response)
            return await completed_result_changes(match)

    # calculate player1 elo
    new_p1_elo = calculate_elo(player1_elo, player2_elo, player1_stars, player1_destruction,
                               player2_stars, player2_destruction, is_challenge)
    player1_elo_change = new_p1_elo - player1_elo
    player1_result_emoji = WIN_EMOJI if player1_elo_change >= 0 else LOSS_EMOJI
    player1_elo_change_sign = "+" if player1_elo_change >= 0 else ""

    # calculate player2 elo
    new_p2_elo = calculate_elo(player2_elo, player1_elo, player2_stars, player2_destruction,
                               player1_stars, player1_destruction, is_challenge)
    player2_elo_change = new_p2_elo - player2_elo
    player2_result_emoji = WIN_EMOJI if player2_elo_change >= 0 else LOSS_EMOJI
    player2_elo_change_sign = "+" if player2_elo_change >= 0 else ""

    # generate match data
    ts = int(datetime.now().timestamp())
    
    # Get next match ID
    last_match = await RecordedMatch.find_all().sort("-time").first_or_none()
    if last_match and last_match.id.isdigit():
        match_id = str(int(last_match.id) + 1)
    else:
        match_id = "0"

    p1_stats = MatchPlayerStats(
        stars=player1_total_stars,
        destruction=player1_total_destruction,
        opponent_stars=player2_total_stars,
        opponent_destruction=player2_total_destruction,
        opponent=player2_id,
        elo_change=player1_elo_change,
        won=player1_total_stars > player2_total_stars or (
                player1_total_stars == player2_total_stars and
                player1_total_destruction > player2_total_destruction),
        districts={
            district: MatchDistrictStats(stars=get_stars(result), destruction=get_destruction(result))
            for district, result in zip(final_districts[:3], player1_results[:3], strict=True)
        }
    )

    p2_stats = MatchPlayerStats(
        stars=player2_total_stars,
        destruction=player2_total_destruction,
        opponent_stars=player1_total_stars,
        opponent_destruction=player1_total_destruction,
        opponent=player1_id,
        elo_change=player2_elo_change,
        won=player2_total_stars > player1_total_stars or (
                player2_total_stars == player1_total_stars and
                player2_total_destruction > player1_total_destruction),
        districts={
            district: MatchDistrictStats(stars=get_stars(result), destruction=get_destruction(result))
            for district, result in zip(final_districts[:3], player2_results[:3], strict=True)
        }
    )

    if match:
        previous = match.result_operation.previous_record if match.result_operation else None
        for attacker_id, stats in ((player1_id, p1_stats), (player2_id, p2_stats)):
            if previous:
                for district, district_stats in stats.districts.items():
                    old_stats = previous.players[attacker_id].districts.get(district)
                    district_stats.base_quality = old_stats.base_quality if old_stats else None
            else:
                defender = next(p for p in match.participants if p.user_id != attacker_id)
                for index, district in enumerate(final_districts):
                    base = defender.bases[index]
                    stats.districts[district].base_quality = base.base_quality if base else None

    if match and match.confirmation_tracking:
        for participant in match.participants:
            stats = {player1_id: p1_stats, player2_id: p2_stats}.get(participant.user_id)
            if stats is not None:
                stats.confirmed_at = participant.confirmed_at
                stats.confirmation_method = participant.confirmation_method

    recorded_match = RecordedMatch(
        id=match_id,
        time=ts,
        matched_time=match.matched_time if match and match.confirmation_tracking else None,
        is_challenge=is_challenge,
        players={
            player1_id: p1_stats,
            player2_id: p2_stats
        }
    )
    if token and match:
        await begin_result_operation(match, response)
        recorded_match = await persist_result_record(match, recorded_match)
    elif manual_key is not None:
        recorded_match = await persist_manual_result(recorded_match, manual_key)
    else:
        await recorded_match.insert()

    player1_elo_change = recorded_match.players[player1_id].elo_change
    player2_elo_change = recorded_match.players[player2_id].elo_change
    player1_result_emoji = WIN_EMOJI if player1_elo_change >= 0 else LOSS_EMOJI
    player2_result_emoji = WIN_EMOJI if player2_elo_change >= 0 else LOSS_EMOJI
    player1_elo_change_sign = "+" if player1_elo_change >= 0 else ""
    player2_elo_change_sign = "+" if player2_elo_change >= 0 else ""

    committed_elos: list[int] = []
    for player_doc in [player1, player2]:
        committed_elos.append(await add_recorded_match(player_doc, recorded_match))
        while True:
            player_doc = await Player.find_one(Player.user_id == player_doc.user_id)
            expected_history = [record.to_ref() for record in player_doc.matches]
            player_matches = await RecordedMatch.find(In("_id", [ref.id for ref in expected_history])).to_list()
            stats_update = {
                "wins": get_wins(player_matches, player_doc.user_id),
                "avg_stars": get_avg_stars(player_matches, player_doc.user_id),
                "avg_destruction": get_avg_destruction(player_matches, player_doc.user_id),
                "opponent_avg_stars": get_opponent_avg_stars(player_matches, player_doc.user_id),
                "opponent_avg_destruction": get_opponent_avg_destruction(player_matches, player_doc.user_id),
            }
            result = await Player.get_pymongo_collection().update_one(
                {"user_id": player_doc.user_id, "matches": expected_history},
                {"$set": stats_update},
            )
            if result.matched_count:
                player_doc = player_doc.model_copy(update={**stats_update, "matches": player_matches})
                break

        # update leaderboard cache
        leaderboard_cog = bot.get_ext("LeaderboardCommands")
        if leaderboard_cog:
            await leaderboard_cog.update_player_cache(player_doc)

    new_p1_elo, new_p2_elo = committed_elos
    player1_elo_str = (f"{player1_result_emoji}{new_p1_elo - player1_elo_change} → {new_p1_elo} "
                       f"({player1_elo_change_sign}{player1_elo_change})")
    player2_elo_str = (f"{player2_result_emoji}{new_p2_elo - player2_elo_change} → {new_p2_elo} "
                       f"({player2_elo_change_sign}{player2_elo_change})")

    player1_user = await fetch_member_otherwise_user(guild, bot, player1_id)
    player2_user = await fetch_member_otherwise_user(guild, bot, player2_id)

    play_again_button = ipy.Button(
        style=ipy.ButtonStyle.GRAY,
        label="Play Again",
        custom_id="match_queue",
        emoji=CLASHING_SWORD_EMOJI
    )

    # notify player1
    player1_embed = ipy.Embed(
        title=f"**Match Against {player2_user.display_name}**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )
    player1_embed.description = \
        f"**Your Score:** {player1_total_stars}{STAR_EMOJI} {player1_total_destruction}%\n" \
        f"**Opponent Score:** {player2_total_stars}{STAR_EMOJI} {player2_total_destruction}%\n" \
        f"**Elo:** {player1_elo_str}"
    try:
        await player1_user.send(embed=player1_embed, components=play_again_button)
    except ipy.errors.Forbidden:
        pass

    # notify player2
    player2_embed = ipy.Embed(
        title=f"**Match Against {player1_user.display_name}**",
        footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
        color=COLOR
    )
    player2_embed.description = \
        f"**Your Score:** {player2_total_stars}{STAR_EMOJI} {player2_total_destruction}%\n" \
        f"**Opponent Score:** {player1_total_stars}{STAR_EMOJI} {player1_total_destruction}%\n" \
        f"**Elo:** {player2_elo_str}"
    try:
        await player2_user.send(embed=player2_embed, components=play_again_button)
    except ipy.errors.Forbidden:
        pass

    if manual_key is not None:
        await complete_manual_result(recorded_match)
        await invite_eligible_players(bot, (player1_id, player2_id))

    await fetch_user_match_id_choices(bot, guild, player1_id)
    await fetch_user_match_id_choices(bot, guild, player2_id)
    return (f"{player1_result_emoji}{player1_elo_change_sign}{player1_elo_change}",
            f"{player2_result_emoji}{player2_elo_change_sign}{player2_elo_change}")


def get_previously_recorded_match_id(
    recorded_matches: dict, player_id: str,
    opponent_id: str, players: dict, match: Match,
) -> str | None:

    # locate participants
    p_player = next((p for p in match.participants if p.user_id == player_id), None)
    p_oppo = next((p for p in match.participants if p.user_id == opponent_id), None)

    player_matches = players[player_id]["matches"]
    player_matches_reversed = list(reversed(player_matches))

    for i in range(5):
        if i >= len(player_matches_reversed):
            return None

        previous_match_id = player_matches_reversed[i]
        previous_match = recorded_matches[previous_match_id]

        if previous_match[player_id]["opponent"] != opponent_id:
            continue

        district_keys = list(previous_match[player_id]["districts"].keys())
        if district_keys != list(match.final_districts):
            continue

        player_res = p_player.result
        opponent_res = p_oppo.result

        player_stars = list(map(get_stars, player_res))
        player_total_stars = sum(player_stars)
        if previous_match[player_id]["stars"] != player_total_stars:
            continue

        player_destruction = list(map(get_destruction, player_res))
        player_total_destruction = sum(player_destruction)
        if previous_match[player_id]["destruction"] != player_total_destruction:
            continue

        opponent_stars = list(map(get_stars, opponent_res))
        opponent_total_stars = sum(opponent_stars)
        if previous_match[opponent_id]["stars"] != opponent_total_stars:
            continue

        opponent_destruction = list(map(get_destruction, opponent_res))
        opponent_total_destruction = sum(opponent_destruction)
        if previous_match[opponent_id]["destruction"] != opponent_total_destruction:
            continue

        return previous_match_id


async def record_base_stats(player_results: dict, match: Match):
    if is_final_match(match):
        raise ResultOperationConflict(match.token, "Final base scores belong to the dedicated Final result.")
    operation = match.result_operation
    for side, participant in enumerate(match.participants):
        opponent = next((p for p in match.participants if p.user_id != participant.user_id), None)
        if not opponent:
            continue

        opponent_result = player_results.get(opponent.user_id, []) or []

        for j in range(3):
            base = participant.bases[j] if j < len(participant.bases) else None
            district = match.final_districts[j] if j < len(match.final_districts) else None

            if not base or not district:
                continue
            if j >= len(opponent_result) or not opponent_result[j]:
                continue

            # filter out outliers
            stars = get_stars(opponent_result[j])
            destruction = get_destruction(opponent_result[j])
            if (stars == 0 and destruction == 0) or (destruction == 100 and stars < 3) or (destruction < 50 and stars > 1):
                continue

            # record attack result
            now = operation.created_at if operation else datetime.now().timestamp()
            new_record = [opponent.user_id, stars, destruction, now]
            if operation:
                receipt = f"{operation.key}:base:{side}:{j}"
                await BaseLayout.get_pymongo_collection().update_one(
                    {"link": base.link, "operation_receipts": {"$ne": receipt}},
                    {
                        "$push": {"records": new_record},
                        "$max": {"expiration_date": now + 15 * 86400},
                        "$addToSet": {"operation_receipts": receipt},
                    },
                )
                continue
            await BaseLayout.find_one(
                BaseLayout.link == base.link
            ).update(
                {
                    "$push": {"records": new_record},
                    "$max": {"expiration_date": now + 15 * 86400}
                }
            )


async def _mark_result_publication_incomplete(message: ipy.Message) -> None:
    pending_embed = ipy.Embed(
        title="**Result Publication Incomplete**",
        description="Results are recorded. This message is pending durable tracking and publication.",
        color=COLOR,
    )
    try:
        await edit_channel_message(message, embed=pending_embed, components=[])
    except ipy.errors.HTTPException:
        logging.exception("Unable to mark result message %s incomplete", message.id)

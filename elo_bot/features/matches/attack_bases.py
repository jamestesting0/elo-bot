import random
from datetime import datetime

import interactions as ipy
from beanie.odm.operators.find.comparison import NotIn

from elo_bot.config.emojis import IMPORTANT_EMOJI
from elo_bot.config.game import DISTRICT_CODES_REVERSED
from elo_bot.config.presentation import COLOR, FOOTER_IMAGE, FOOTER_TEXT, UNAVAILABLE_IMAGE
from elo_bot.db import BaseLayout, BaseLink, Match, Player
from elo_bot.core.finals import is_final_match
from elo_bot.features.matches.base_selection import district_base_quality, ensure_secondary_base_index, get_base_quality
from elo_bot.db.final_models import FinalMatchResult


def is_missing_scout_view(match: Match, player_id: str | int):
    player_id = str(player_id)
    participant = next((p for p in match.participants if p.user_id == player_id), None)
    if not participant:
        return None, None

    for i, base in enumerate(participant.bases):
        if not base:
            continue

        if not base.image or base.image == UNAVAILABLE_IMAGE:
            # Get district name and return the missing base link
            district = match.final_districts[i] if i < len(match.final_districts) else None
            if not district and base.link:
                # fallback if final_districts not filled properly
                district = DISTRICT_CODES_REVERSED.get(base.link.split('%')[2], "Unknown")

            return district, base.link

    return None, None


async def send_bases(bot: ipy.Client, token: str, player: int, opponent: int, scout: bool = True):
    match = await Match.find_one(Match.token == token)
    p_index = player
    player = await bot.fetch_user(match.participants[player].user_id)
    opponent = await bot.fetch_user(match.participants[opponent].user_id)

    base_embeds = await choose_bases(token, player, opponent)
    if not scout:
        return

    try:
        if (not match.is_war and not match.hosts[0]) or (match.is_war and not match.hosts[p_index]):
            await player.send(f"{IMPORTANT_EMOJI} It is 10 minutes before the match <#{match.thread}> and "
                              f"no host is available. Please reschedule your match, and make sure there is a "
                              f"host for it.")

            return

        await player.send(f"Bases you will attack for the match <#{match.thread}>", embeds=base_embeds)
    except ipy.errors.Forbidden:
        pass


async def choose_bases(token: str, player: ipy.User, opponent: ipy.User,
                       return_match_data: bool = False) -> list[ipy.Embed] | Match:
    # determine the quality of the bases
    player_doc = await Player.find_one(Player.user_id == str(player.id))
    opponent_doc = await Player.find_one(Player.user_id == str(opponent.id))
    match = await Match.find_one(Match.token == token)
    primary_quality, secondary_quality = get_base_quality(player_doc, opponent_doc, match)

    opponent_index = next(i for i, participant in enumerate(match.participants)
                          if participant.user_id == str(opponent.id))
    match = await ensure_secondary_base_index(match, opponent_index, primary_quality, secondary_quality)
    seed = match.participants[opponent_index].secondary_base_index
    embeds = []
    index = -1
    now = datetime.now().timestamp()
    opponent_participant = next(p for p in match.participants if p.user_id == str(opponent.id))
    player_participant = next(p for p in match.participants if p.user_id == str(player.id))
    for base in opponent_participant.bases:
        index += 1
        embed = ipy.Embed(
            title=f"**{match.final_districts[index]}**",
            footer=ipy.EmbedFooter(text=FOOTER_TEXT, icon_url=FOOTER_IMAGE),
            color=COLOR
        )

        # determine the base quality in case the players have different base quality
        if index == seed:
            base_quality = secondary_quality
        else:
            base_quality = primary_quality

        if not base:
            bases_for_opponent = [b.link for b in player_participant.bases if b]

            # find bases the player has just attacked
            on_going_bases = []
            on_going_matches = await Match.find(
                {"participants.user_id": str(player.id)}
            ).to_list()
            for m in on_going_matches:
                opp = next(p for p in m.participants if p.user_id != str(player.id))
                for base_data in opp.bases:
                    if base_data:
                        on_going_bases.append(base_data.link)

            # filter bases built the player themselves
            # filter bases identical to opponent's bases if it is not a war match
            # filter bases that are expired
            # filter bases on quality
            # filter bases that has just been attacked
            base_quality = district_base_quality(match.final_districts[index], base_quality)
            bases_filtered = await BaseLayout.find(
                BaseLayout.district == match.final_districts[index],
                BaseLayout.builder_id != str(player.id),
                BaseLayout.quality == base_quality,
                NotIn(BaseLayout.link, bases_for_opponent),
                NotIn(BaseLayout.link, on_going_bases),
                BaseLayout.expiration_date > now
            ).to_list()

            # no available base for the player
            if not bases_filtered:
                embed.set_image(UNAVAILABLE_IMAGE)
                embed.description = f"{IMPORTANT_EMOJI} Base missing.\n"
                continue

            final_hits = {}
            final_usage = {}
            if is_final_match(match):
                history = await FinalMatchResult.find({"snapshot.contestants": str(player.id), "snapshot.outcome": "Played"}).to_list()
                seen_attempts = set()
                for result in history:
                    attempt_key = (result.final_id, result.node_id, result.contestant_revision, result.attempt)
                    if attempt_key in seen_attempts:
                        continue
                    seen_attempts.add(attempt_key)
                    for owner, links in result.bases.items():
                        if owner == str(player.id):
                            continue
                        for link in filter(None, links):
                            final_hits[link] = max(final_hits.get(link, 0), result.created_at)
                            final_usage[link] = final_usage.get(link, 0) + 1

            def latest_player_attack_timestamp(base_layout: BaseLayout) -> int | float:
                latest_timestamp: int | float = final_hits.get(base_layout.link, 0)
                for record in base_layout.records:
                    if not isinstance(record, (list, tuple)) or len(record) < 4:
                        continue

                    timestamp = record[3]
                    if (
                            str(record[0]) == str(player.id)
                            and isinstance(timestamp, (int, float))
                            and not isinstance(timestamp, bool)
                            and timestamp > 0
                    ):
                        latest_timestamp = max(latest_timestamp, timestamp)

                return latest_timestamp

            # only bases tied for the player's oldest last hit are eligible; unseen bases have timestamp zero.
            bases_with_last_hit = [(base, latest_player_attack_timestamp(base)) for base in bases_filtered]
            oldest_last_hit = min(timestamp for _, timestamp in bases_with_last_hit)
            oldest_bases = [base for base, timestamp in bases_with_last_hit if timestamp == oldest_last_hit]

            # favor the least-used bases within the eligible pool.
            finalists = sorted(oldest_bases, key=lambda base: len(base.records) + final_usage.get(base.link, 0))[:3]

            # relative weights give 50/25/25 with three finalists and normalize for smaller pools.
            chosen_base = random.choices(finalists, weights=[2, 1, 1][:len(finalists)], k=1)[0]

            # set the bases for the opponent
            embed.set_image(chosen_base.image_url)
            opponent_participant.bases[index] = BaseLink(
                link=chosen_base.link,
                image=chosen_base.image_url,
                base_quality=chosen_base.quality,
            )
        else:
            embed.set_image(base.image)

        embeds.append(embed)

    await match.save_changes()

    if return_match_data:
        return match

    return embeds

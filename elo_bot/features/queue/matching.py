import math
import sys
import traceback
from datetime import datetime
from typing import Optional

import interactions as ipy

from elo_bot.config.ids import BOT_ID, ELO_BLACKLIST_ROLE, MAIN_GUILD
from elo_bot.db import Player, Queue, ServerState
from elo_bot.features.matches.lifecycle import get_district_options, initiate_match
from elo_bot.features.matches.occupancy import is_player_occupied
from elo_bot.features.matches.queued_districts import start_district_selection
from elo_bot.features.queue.policy import (
    QUEUE_MATCH_RANGES,
    QUEUE_OPERATION_LOCK,
    QUEUE_TIER_ADVANCEMENT,
    QUEUE_TIER_AGED,
    QUEUE_TIER_MIXED,
    QUEUE_TIER_NORMAL,
    QUEUE_TIER_PRIORITY,
    QUEUE_TIERS,
)
from elo_bot.features.queue.state import (
    QueueOutcome,
    QueueOutcomeStatus,
    add_player_to_queue,
    get_closest_elo_player,
    get_last_opponent,
    get_match_queue,
    is_autoqueue_active,
    move_queue_tier,
    normalize_queue_timestamps,
    promote_tier1_aged_players,
    reconcile_queue_memberships,
    remove_player_from_queue,
)


async def find_opponent(player: Player, last_opponent: Optional[str], candidates: list[Player],
                        max_elo_diff: int, tier: int, queue: Queue | None = None) -> Optional[Player]:
    for candidate in candidates:
        candidate_last_opponent_id = await get_last_opponent(candidate)
        if player.user_id == candidate.user_id:
            continue

        if candidate_last_opponent_id and candidate_last_opponent_id == player.user_id and tier != 1:
            continue

        if last_opponent and last_opponent == candidate.user_id and tier != 1:
            continue

        if abs(player.elo - candidate.elo) > max_elo_diff:
            continue

        if queue is not None and await is_player_occupied(str(candidate.user_id)):
            remove_player_from_queue(queue, candidate)
            await queue.save_changes()
            continue

        return candidate

    return None


async def find_queue_opponent(player: Player, last_opponent: Optional[str], queue: Queue,
                              max_elo_diff: int) -> Optional[Player]:
    candidates = sorted(getattr(queue, "players", []) or [], key=lambda x: abs(x.elo - player.elo))
    return await find_opponent(player, last_opponent, candidates, max_elo_diff,
                               getattr(queue, "tier", QUEUE_TIER_NORMAL), queue)


async def match_with_best_queue(bot: ipy.Client, player: Player, last_opponent: Optional[str],
                                queue_ranges: list[tuple[Queue, int]]) -> bool:
    matches: list[tuple[int, Queue, Player]] = []

    for queue, max_elo_diff in queue_ranges:
        opponent = await find_queue_opponent(player, last_opponent, queue, max_elo_diff)
        if opponent:
            matches.append((abs(opponent.elo - player.elo), queue, opponent))

    if not matches:
        return False

    _, queue, opponent = min(matches, key=lambda match: match[0])

    async def remove_matched_opponent() -> None:
        remove_player_from_queue(queue, opponent)
        await queue.save_changes()

    selection = queue.district_selections.get(str(opponent.user_id))
    if selection is None:
        await initiate_match(bot, player, opponent, on_inserted=remove_matched_opponent)
    else:
        await initiate_match(bot, player, opponent, on_inserted=remove_matched_opponent,
                             queue_selections=(None, selection))
    return True


async def run_queue_matching_trigger(bot: ipy.Client):
    now = datetime.now().timestamp()
    queues = {tier: await get_match_queue(tier) for tier in QUEUE_TIERS}
    await reconcile_queue_memberships(list(queues.values()))

    await promote_tier1_aged_players(queues[QUEUE_TIER_AGED], queues[QUEUE_TIER_MIXED], now)

    for tier in QUEUE_TIERS:
        await match_players(bot, tier, QUEUE_MATCH_RANGES[tier], queue=queues[tier])

    for source_tier, target_tier in QUEUE_TIER_ADVANCEMENT:
        await move_queue_tier(source_tier, target_tier, now)


async def match_players(bot: ipy.Client, tier: int, max_elo_diff: int, *, queue: Queue | None = None):
    if queue is None:
        queue = await get_match_queue(tier)
    normalize_queue_timestamps(queue)

    # sort the queue so that players with similar elo get matched
    iterations = math.floor(len(queue.players) / 2)

    for _ in range(iterations):
        available_players = list(queue.players)
        if len(available_players) < 2:
            break

        player1 = available_players[0]
        if await is_player_occupied(str(player1.user_id)):
            remove_player_from_queue(queue, player1)
            await queue.save_changes()
            continue
        last_opponent_id = await get_last_opponent(player1)

        candidates = sorted(available_players, key=lambda x: x.elo)
        player2 = await find_opponent(player1, last_opponent_id, candidates, max_elo_diff, tier, queue)

        if not player2:
            continue

        async def remove_matched_pair() -> None:
            remove_player_from_queue(queue, player1)
            remove_player_from_queue(queue, player2)
            await queue.save_changes()

        selections = (queue.district_selections.get(str(player1.user_id)),
                      queue.district_selections.get(str(player2.user_id)))
        if any(selection is not None for selection in selections):
            await initiate_match(bot, player1, player2, on_inserted=remove_matched_pair,
                                 queue_selections=selections)
        else:
            await initiate_match(bot, player1, player2, on_inserted=remove_matched_pair)

    await queue.save_changes()


async def queue_player(
        bot: ipy.Client,
        author: ipy.Member | ipy.User,
        priority: bool = False,
        *, district_picker: bool = False,
) -> QueueOutcome:
    async with QUEUE_OPERATION_LOCK:
        if isinstance(author, ipy.Member) and author.has_role(ELO_BLACKLIST_ROLE):
            return QueueOutcome(QueueOutcomeStatus.BLACKLISTED)

        if isinstance(author, ipy.User):
            guild = await bot.fetch_guild(MAIN_GUILD)
            member_obj = await guild.fetch_member(str(author.id))
            if member_obj.has_role(ELO_BLACKLIST_ROLE):
                return QueueOutcome(QueueOutcomeStatus.BLACKLISTED)

        if (await ServerState.current()).queue_disabled:
            return QueueOutcome(QueueOutcomeStatus.DISABLED)

        player = await Player.find_one(Player.user_id == str(author.id))
        if not player:
            return QueueOutcome(QueueOutcomeStatus.UNREGISTERED)

        queues = {tier: await get_match_queue(tier) for tier in QUEUE_TIERS}
        await reconcile_queue_memberships(list(queues.values()))
        now = datetime.now().timestamp()
        for queue in queues.values():
            if normalize_queue_timestamps(queue, now):
                await queue.save_changes()

        await promote_tier1_aged_players(queues[QUEUE_TIER_AGED], queues[QUEUE_TIER_MIXED], now)

        for queue in queues.values():
            if player.user_id in [queued_player.user_id for queued_player in queue.players]:
                return QueueOutcome(QueueOutcomeStatus.ALREADY_QUEUED)

        player_occupied = await is_player_occupied(str(author.id))
        if player_occupied:
            return QueueOutcome(QueueOutcomeStatus.OCCUPIED, player_occupied)

        last_opponent_id = await get_last_opponent(player)

        tier0_queue = queues[QUEUE_TIER_AGED]
        tier1_queue = queues[QUEUE_TIER_MIXED]
        tier2_queue = queues[QUEUE_TIER_PRIORITY]
        tier3_queue = queues[QUEUE_TIER_NORMAL]

        if await match_with_best_queue(bot, player, last_opponent_id, [(tier0_queue, 250)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        if await match_with_best_queue(bot, player, last_opponent_id, [(tier1_queue, 200)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        stored_priority = bool(player.priority)
        requested_priority = False
        if priority:
            closest_player = await get_closest_elo_player(player)
            closest_elo_diff = abs(closest_player.elo - player.elo) if closest_player else None
            requested_priority = closest_elo_diff is not None and closest_elo_diff > 100

        use_priority_queue = stored_priority or requested_priority

        if use_priority_queue:
            if await match_with_best_queue(
                bot,
                player,
                last_opponent_id,
                [(tier2_queue, 150), (tier3_queue, 150)],
            ):
                if stored_priority:
                    player.priority = False
                    await player.save_changes()

                return QueueOutcome(QueueOutcomeStatus.MATCHED)
        elif await match_with_best_queue(bot, player, last_opponent_id, [(tier2_queue, 150)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        if int(bot.user.id) != BOT_ID:
            add_player_to_queue(tier1_queue, player, now)
            selection = None
            if district_picker:
                selection = start_district_selection(
                    player, get_district_options(player.elo < 1200, player.capital_peak_enabled),
                )
                tier1_queue.district_selections[str(player.user_id)] = selection
            await tier1_queue.save_changes()
            return QueueOutcome(QueueOutcomeStatus.BETA_QUEUED, selection=selection)

        if not use_priority_queue and await match_with_best_queue(bot, player, last_opponent_id, [(tier3_queue, 200)]):
            return QueueOutcome(QueueOutcomeStatus.MATCHED)

        target_queue = tier2_queue if use_priority_queue else tier3_queue
        add_player_to_queue(target_queue, player, now)
        selection = None
        if district_picker:
            selection = start_district_selection(
                player, get_district_options(player.elo < 1200, player.capital_peak_enabled),
            )
            target_queue.district_selections[str(player.user_id)] = selection
        await target_queue.save_changes()

        if use_priority_queue:
            if stored_priority:
                player.priority = False
                await player.save_changes()

        if not use_priority_queue and len(tier3_queue.players) >= 4:
            await run_queue_matching_trigger(bot)
            refreshed_queues = [await get_match_queue(tier) for tier in QUEUE_TIERS]
            player_still_in_queue = any(
                queued_player.user_id == str(author.id)
                for queue in refreshed_queues
                for queued_player in queue.players
            )
            if not player_still_in_queue:
                return QueueOutcome(QueueOutcomeStatus.MATCHED)

        return QueueOutcome(QueueOutcomeStatus.QUEUED, selection=selection)


async def autoqueue_completed_match_participants(
        bot: ipy.Client,
        token: str,
        participant_ids: tuple[str, ...],
) -> None:
    now = int(datetime.now().timestamp())
    for participant_id in participant_ids:
        try:
            player = await Player.find_one(Player.user_id == participant_id)
            if not player or not is_autoqueue_active(player.auto_queue, now):
                continue

            guild = await bot.fetch_guild(MAIN_GUILD)
            member = await guild.fetch_member(participant_id)
            await queue_player(bot, member, priority=False)
        # This is the terminal automation boundary: a participant-specific Discord,
        # database, or queue-core failure is reported without undoing finalization.
        except Exception:  # noqa: BROAD_EXCEPT_OK
            print(f"Terminal autoqueue failed for match {token}, participant {participant_id}.", file=sys.stderr)
            traceback.print_exc()

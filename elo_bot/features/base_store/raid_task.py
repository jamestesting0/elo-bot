import logging
import random

import coc
import interactions as ipy
from pymongo.errors import PyMongoError

from elo_bot.features.base_store.channel_context import ScheduledBaseStoreContext
from elo_bot.features.base_store.models import BaseStoreRuntime, SelfBaseStoreRequest
from elo_bot.features.base_store.preparation import BASE_STORE_RESERVATION_LOCK
from elo_bot.features.base_store.raid_policy import raid_storage_skip_reason
from elo_bot.features.base_store.self_service import store_self_bases
from elo_bot.config.presentation import COLOR
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.game import HOST_CLAN_CHOICES
from elo_bot.config.ids import RAID_AUTOSTORE_LOGS_CHANNEL
from elo_bot.db import Match


async def store_before_raid_weekend(runtime: BaseStoreRuntime) -> None:
    lines: list[str] = []
    try:
        await _store_before_raid_weekend(runtime, lines)
    finally:
        await _send_run_log(runtime.bot, lines)


async def _store_before_raid_weekend(runtime: BaseStoreRuntime, lines: list[str]) -> None:
    matches = await Match.find({
        "thread": {"$ne": None},
        "$or": [{"times.0": None, "hosts.0": None}, {"times.1": None, "hosts.1": None}],
    }).to_list()
    lines.append(f"Checked **{len(matches)}** match(es) with an unstarted side.")
    # Discord snowflakes sort by creation time, rather than matchmaking time.
    for match in sorted(matches, key=lambda item: int(item.thread)):
        players = []
        for player in (0, 1):
            if (reason := raid_storage_skip_reason(match, player)) is None:
                players.append(player)
            elif reason != "already started":
                lines.append(f"Skipped <#{match.thread}> side {player + 1}: {reason}.")
        if not players:
            continue
        random.shuffle(players)
        try:
            thread = await runtime.bot.fetch_channel(int(match.thread))
            if thread is None:
                lines.append(f"Skipped <#{match.thread}>: thread not found.")
                continue
            ctx = ScheduledBaseStoreContext(runtime.bot, thread)
        except (ipy.errors.HTTPException, coc.ClashOfClansException, PyMongoError):
            logging.exception("Unable to open scheduled raid base storage for match %s", match.token)
            lines.append(f"{ERROR_EMOJI} Could not open <#{match.thread}>.")
            continue
        for player in players:
            try:
                async with BASE_STORE_RESERVATION_LOCK:
                    slots = await runtime._base_store_sync_slots()
                    free_slots = [slots.get(str(choice.value), []) for choice in HOST_CLAN_CHOICES]
                    if sum(len(available) for available in free_slots) <= 2 or sum(map(bool, free_slots)) < 2:
                        lines.append(
                            f"Stopped: {sum(map(len, free_slots))} free slot(s) in "
                            f"{sum(map(bool, free_slots))} clan(s); 2 slots in 2 clans are kept free."
                        )
                        return
                await store_self_bases(
                    runtime, ctx, SelfBaseStoreRequest(match, player, raid_autostore=True),
                )
            except (ipy.errors.HTTPException, coc.ClashOfClansException, PyMongoError):
                logging.exception("Scheduled raid base storage failed for match %s, side %s", match.token, player)
                lines.append(f"{ERROR_EMOJI} <#{match.thread}> side {player + 1}: storage could not be confirmed.")
                try:
                    await ctx.send(
                        f"{ERROR_EMOJI} Automatic base storage could not be confirmed for "
                        f"<@{match.participants[player].user_id}>. No reservation was released. Please contact a helper."
                    )
                except ipy.errors.HTTPException:
                    logging.exception("Unable to report scheduled raid storage failure for match %s", match.token)
                continue
            lines.append(await _reservation_line(match, player))


async def _reservation_line(match: Match, player: int) -> str:
    try:
        stored = await Match.find_one({"_id": match.id})
    except PyMongoError:
        logging.exception("Unable to read raid auto-store reservation for match %s", match.token)
        return f"{ERROR_EMOJI} <#{match.thread}> side {player + 1}: reservation could not be read back."
    store = stored.participants[player].base_store if stored else None
    if store is None:
        return f"{ERROR_EMOJI} <#{match.thread}> side {player + 1}: no slot reserved, see thread."
    return f"Stored <#{match.thread}> side {player + 1} in `{store.clan_tag}` {store.slot}."


async def _send_run_log(bot: ipy.Client, lines: list[str]) -> None:
    chunks = [""]
    for line in lines or ["No output."]:
        if len(chunks[-1]) + len(line) + 1 > 4000:
            chunks.append("")
        chunks[-1] += line + "\n"
    try:
        channel = await bot.fetch_channel(RAID_AUTOSTORE_LOGS_CHANNEL)
        for chunk in chunks:
            await channel.send(embed=ipy.Embed(title="**Raid Weekend Auto Store**", description=chunk, color=COLOR))
    except ipy.errors.HTTPException:
        logging.exception("Unable to send raid auto-store log")

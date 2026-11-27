from hashlib import sha256
import time
from urllib.parse import parse_qs, urlsplit

import anyio
import interactions as ipy

from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.core.finals import FINAL_SEATS, FinalError
from elo_bot.db import EloFinal
from elo_bot.db.season_models import FinalPickPrompt
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.discord.mentions import mention_user
from elo_bot.discord.message_edits import edit_channel_message
from elo_bot.features.finals.bracket import lineup_confirmed, seeded_players
from elo_bot.features.finals.lifecycle import reload_final
from elo_bot.features.finals.opponent_picks import pick_prompt, pick_prompt_key, pick_receipt, pick_turn, save_pick
from elo_bot.features.finals.posts import PUBLICATION_LOCKS, PostContent, PostReference, locate_post, publish_post


def lineup_embed(final: EloFinal) -> ipy.Embed:
    rows = []
    for seed, user in enumerate(seeded_players(final), 1):
        name = " ".join(final.player_nicknames.get(user, "").split()) or "Unknown player"
        rows.append(f"**#{seed}** {mention_user(None, user, display_name=name, include_wishlist_tab=False)}")
    if final.state in {"CANCELLING", "CANCELLED"}:
        rows = ["Final cancelled.", "", *rows] if final.opponent_picks and final.opponent_picks.seeds else ["Final cancelled."]
    elif final.state == "REGISTRATION" and not lineup_confirmed(final):
        rows = ["Lineup pending confirmation."]
    elif len(rows) < FINAL_SEATS:
        rows[:0] = [f"{len(rows)} of {FINAL_SEATS} confirmed", ""]
    embed = ipy.Embed(title=f"**Season {final.season_number} Elo Final · Lineup**",
                      description="\n".join(rows), color=embed_color())
    icon = footer_image()
    separator = "&" if "?" in icon else "?"
    embed.set_footer(text=FOOTER_TEXT, icon_url=f"{icon}{separator}elo_final={final.id}")
    return embed


def _lineup_ready(final: EloFinal) -> bool:
    if not final.active or final.state in {"CANCELLING", "CANCELLED"}:
        return False
    picks = final.opponent_picks
    return lineup_confirmed(final) or (final.state != "REGISTRATION" and picks is not None
                                      and (picks.confirmed is True or final.lineup_attempted))


def registration_posts_pending(final: EloFinal) -> bool:
    if final.opponent_picks is None:
        return False
    if any(not prompt.dm_retired for prompt in final.opponent_pick_prompts.values()):
        return True
    if final.lineup_message_id is None:
        return final.lineup_attempted or _lineup_ready(final)
    return final.lineup_signature != _lineup_signature(lineup_embed(final))


def _lineup_signature(embed: ipy.Embed) -> str:
    return sha256((embed.description or "").encode()).hexdigest()


async def _publish_lineup(bot: ipy.Client, final: EloFinal) -> None:
    picks = final.opponent_picks
    assert picks is not None
    can_send = _lineup_ready(final)
    if final.lineup_message_id is None and not final.lineup_attempted and not can_send:
        return
    embed = lineup_embed(final)
    signature = _lineup_signature(embed)
    if final.lineup_message_id and final.lineup_signature == signature:
        return
    channel = await bot.fetch_channel(final.channel_id)
    if not isinstance(channel, (ipy.GuildText, ipy.GuildNews, ipy.ThreadChannel)):
        raise FinalError("The Final channel is unavailable.")
    collection = EloFinal.get_pymongo_collection()
    initial = await collection.update_one({"_id": final.id, "entrant_ids": final.entrant_ids, "state": final.state,
        "opponent_picks.generation": picks.generation}, {"$set": {"lineup_attempted": True}})
    if not initial.matched_count:
        return
    nonce = sha256(f"final-lineup|{final.id}".encode()).hexdigest()[:24]

    def matches_lineup(message: ipy.Message) -> bool:
        return any(item.title == embed.title and item.footer is not None and item.footer.icon_url
                   and parse_qs(urlsplit(item.footer.icon_url).query).get("elo_final") == [str(final.id)]
                   for item in message.embeds)

    reference = PostReference(nonce, bot.user.id, final.lineup_message_id, final.lineup_attempted, matches_lineup)
    if not can_send:
        message = await locate_post(channel, reference)
        if message is None:
            await collection.update_one({"_id": final.id}, {"$set": {"lineup_attempted": False,
                                                                    "lineup_message_id": None}})
            return
        await edit_channel_message(message, embed=embed, allowed_mentions=ipy.AllowedMentions.none())
    else:
        message = await publish_post(channel, reference, PostContent(embed, []))
    await collection.update_one({"_id": final.id}, {"$set": {
        "lineup_message_id": str(message.id), "lineup_signature": signature,
    }})


async def _publish_turn(bot: ipy.Client, final: EloFinal, index: int) -> None:
    picks = final.opponent_picks
    assert picks is not None
    key = pick_prompt_key(picks.generation, index)
    prompt = final.opponent_pick_prompts.get(key, FinalPickPrompt())
    if prompt.dm_message_id or prompt.dm_unavailable:
        return
    field = f"opponent_pick_prompts.{key}"
    identity = {"_id": final.id, "opponent_picks.generation": picks.generation}
    collection = EloFinal.get_pymongo_collection()
    try:
        channel = await bot.cache.fetch_dm_channel(int(picks.seeds[index]))
        if not isinstance(channel, ipy.DM):
            raise FinalError("Opponent pick DM is unavailable.")
        claimed = await collection.update_one({**identity, "active": True, "state": "REGISTRATION",
                                               "opponent_picks.closed": {"$ne": True},
                                               "entrant_ids": final.entrant_ids,
                                               f"opponent_picks.turns.{index}.opponent": None}, {"$set": {
            f"{field}.dm_attempted": True, f"{field}.dm_channel_id": str(channel.id),
        }})
        if not claimed.matched_count:
            return
        embed, controls = pick_prompt(final, index)
        reference = _prompt_reference(bot, final, key, prompt)
        message = await publish_post(channel, reference, PostContent(embed, controls))
    except (ipy.errors.Forbidden, ipy.errors.NotFound):
        await collection.update_one(identity, {"$set": {f"{field}.dm_unavailable": True}})
        return
    await collection.update_one({"_id": final.id}, {"$set": {f"{field}.dm_message_id": str(message.id)}})


def _prompt_reference(bot: ipy.Client, final: EloFinal, key: str, prompt: FinalPickPrompt) -> PostReference:
    nonce = sha256(f"final-pick|{final.id}|{key}".encode()).hexdigest()[:24]
    select_id = f"finalpick|{final.id}|{key.replace(':', '|')}|select"

    def matches_prompt(message: ipy.Message) -> bool:
        return any(getattr(component, "custom_id", None) == select_id
                   for row in message.components for component in getattr(row, "components", ()))

    return PostReference(nonce, bot.user.id, prompt.dm_message_id, prompt.dm_attempted, matches_prompt)


async def _close_pick_prompts(bot: ipy.Client, final: EloFinal) -> bool:
    picks = final.opponent_picks
    assert picks is not None
    active = pick_turn(final)
    collection = EloFinal.get_pymongo_collection()
    pending = False
    for key, prompt in final.opponent_pick_prompts.items():
        generation, index = (int(value) for value in key.split(":"))
        current = generation == picks.generation
        if current and (index == active or prompt.dm_retired):
            continue
        try:
            channel = await bot.fetch_channel(int(prompt.dm_channel_id)) if prompt.dm_channel_id else None
            if channel is not None and not isinstance(channel, ipy.DM):
                raise FinalError("Opponent pick DM is unavailable.")
            if channel is not None and (message := await locate_post(channel, _prompt_reference(bot, final, key, prompt))):
                turn = picks.turns[index] if current else None
                if turn is None or turn.opponent is None:
                    await edit_channel_message(message, content="Opponent picks closed.", embeds=[], components=[])
                else:
                    embed = pick_receipt(final, turn.opponent, timed_out=turn.random)
                    await edit_channel_message(message, embed=embed, components=[])
        except ipy.errors.NotFound:
            pass
        except (ipy.errors.HTTPException, FinalError):
            pending = True
            continue
        field = f"opponent_pick_prompts.{key}"
        update = {"$set": {f"{field}.dm_retired": True}} if current else {"$unset": {field: ""}}
        await collection.update_one({"_id": final.id}, update)
    return pending


async def sync_registration_posts(bot: ipy.Client, final: EloFinal) -> list[str]:
    if final.opponent_picks is None or (not registration_posts_pending(final) and not _lineup_ready(final)):
        return []
    async with PUBLICATION_LOCKS.setdefault(f"registration:{final.id}", anyio.Lock()):
        final = await reload_final(final)
        picks = final.opponent_picks
        assert picks is not None
        pending = []
        index = pick_turn(final)
        if index is not None:
            deadline = picks.turns[index].deadline
            now = int(time.time())
            if deadline is not None and now >= deadline:
                await save_pick(final, index, now)
                final = await reload_final(final)
                index = pick_turn(final)
        try:
            await _publish_lineup(bot, final)
        except (ipy.errors.HTTPException, FinalError):
            pending.append("Lineup update pending.")
        dm_pending = False
        try:
            if index is not None:
                await _publish_turn(bot, final, index)
        except (ipy.errors.HTTPException, FinalError):
            dm_pending = True
        if await _close_pick_prompts(bot, final) or dm_pending:
            pending.append("Opponent pick message update pending.")
        return pending

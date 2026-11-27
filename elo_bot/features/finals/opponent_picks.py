from random import choice
import re

import interactions as ipy

from elo_bot.config.emojis import TICK_EMOJI
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.core.finals import FINAL_PICK_COUNT, FINAL_PICK_WINDOW, FinalError
from elo_bot.db import EloFinal
from elo_bot.db.season_models import FinalOpponentPicks
from elo_bot.discord.embeds import embed_color, footer_image
from elo_bot.features.finals.bracket import opponent_picks_pending


def pick_turn(final: EloFinal) -> int | None:
    if not opponent_picks_pending(final) or final.opponent_picks is None:
        return None
    return next((index for index, turn in enumerate(final.opponent_picks.turns)
                 if turn.deadline is not None and turn.opponent is None), None)


def available_opponents(picks: FinalOpponentPicks) -> list[str]:
    taken = {turn.opponent for turn in picks.turns}
    return [user for user in picks.seeds[4:] if user not in taken]


def pick_prompt_key(generation: int, index: int) -> str:
    return f"{generation}:{index}"


async def save_pick(final: EloFinal, index: int, now: int, opponent: str | None = None) -> bool:
    picks = final.opponent_picks
    if picks is None or pick_turn(final) != index:
        return False
    timed_out = opponent is None
    eligible = available_opponents(picks)
    if timed_out:
        opponent = choice(eligible)
    elif opponent not in eligible:
        raise FinalError("That opponent is unavailable.")
    field = f"opponent_picks.turns.{index}"
    fields = {f"{field}.opponent": opponent, f"{field}.random": timed_out}
    if index + 1 < FINAL_PICK_COUNT:
        fields[f"opponent_picks.turns.{index + 1}.deadline"] = now + FINAL_PICK_WINDOW
    saved = await EloFinal.get_pymongo_collection().update_one({
        "_id": final.id, "active": True, "state": "REGISTRATION", "opponent_picks.generation": picks.generation,
        "opponent_picks.closed": {"$ne": True}, "entrant_ids": final.entrant_ids, f"{field}.opponent": None,
        f"{field}.deadline": {"$lte" if timed_out else "$gt": now},
    }, {"$set": fields, "$inc": {"revision": 1, "setup_revision": 1}})
    return bool(saved.modified_count)


def pick_name(final: EloFinal, user: str) -> str:
    name = " ".join(final.player_nicknames.get(user, "").split())
    return re.sub(r"([\\`*_~|\[\]<>])", r"\\\1", name) if name else f"<@{user}>"


def pick_prompt(final: EloFinal, index: int, selected: str | None = None) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    picks = final.opponent_picks
    assert picks is not None
    turn = picks.turns[index]
    options = [ipy.StringSelectOption(
        label=f"#{picks.seeds.index(user) + 1} {final.player_nicknames.get(user, user)}"[:100],
        value=user, default=user == selected,
    ) for user in available_opponents(picks)]
    prefix = f"finalpick|{final.id}|{picks.generation}|{index}"
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Opponent Pick**", color=embed_color(),
                      description=f"Seed #{index + 1} · Closes <t:{turn.deadline}:R>")
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    select = ipy.StringSelectMenu(*options, placeholder="Choose opponent", custom_id=f"{prefix}|select")
    confirm = ipy.Button(label="Confirm", emoji=TICK_EMOJI, style=ipy.ButtonStyle.SUCCESS,
                         custom_id=f"{prefix}|{selected or 'none'}", disabled=selected is None)
    return embed, [ipy.ActionRow(select), ipy.ActionRow(confirm)]


def pick_receipt(final: EloFinal, opponent: str, *, timed_out: bool) -> ipy.Embed:
    action = "Assigned" if timed_out else "Opponent"
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Opponent Pick**", color=embed_color(),
                      description=f"**{action}** · {pick_name(final, opponent)}")
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    return embed

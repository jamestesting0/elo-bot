import interactions as ipy

from elo_bot.config.emojis import CROSS_EMOJI, TICK_EMOJI
from elo_bot.config.presentation import COLOR, FOOTER_TEXT
from elo_bot.discord.embeds import footer_image
from elo_bot.core.finals import NODE_IDS, FinalError
from elo_bot.db import EloFinal
from elo_bot.discord.mentions import mention_user
from elo_bot.features.finals.bracket import seeded_players


def swapped_pairs(final: EloFinal, slots: list[str]) -> dict[str, list[str | None]]:
    """Swap two complete draft slots without introducing a double bye."""
    valid = {f"{node}:{side}" for node in NODE_IDS[:4] for side in range(2)}
    if len(slots) != 2 or len(set(slots)) != 2 or not set(slots) <= valid:
        raise FinalError("Choose two slots to swap.")
    pairs = {key: list(pair) for key, pair in final.manual_draft.items()}
    (left, a), (right, b) = [slot.split(":") for slot in slots]
    pairs[left][int(a)], pairs[right][int(b)] = pairs[right][int(b)], pairs[left][int(a)]
    if any(not any(pair) for pair in pairs.values()):
        raise FinalError("A match needs at least one player.")
    return pairs


def editor_view(final: EloFinal, names: dict[str, str]) -> tuple[ipy.Embed, list[ipy.ActionRow]]:
    seeds = {user: index for index, user in enumerate(seeded_players(final), 1)}
    prefix = f"finalpair|{final.id}|{final.draft_owner}|{final.draft_revision}|"
    lines = []
    options = []
    for node in NODE_IDS[:4]:
        pair = final.manual_draft[node]
        lines.append(f"`{node}` " + " vs. ".join(
            f"({seeds[user]}) {names.get(user, mention_user(None, user))}" if user else "BYE" for user in pair))
        for side, user in enumerate(pair):
            nickname = final.player_nicknames.get(user, user) if user else "BYE"
            elo = final.seed_elo.get(user) if user else None
            description = (f"Seed {seeds[user]} · {elo} Elo" if elo is not None
                           else f"Seed {seeds[user]} · Elo unavailable") if user else "Bye"
            options.append(ipy.StringSelectOption(label=f"{node} · {nickname}"[:100],
                                                  value=f"{node}:{side}", description=description))
    embed = ipy.Embed(title=f"**Season {final.season_number} Final · Bracket**", color=COLOR,
                      description="Seeded by Elo. Swap any two slots, then confirm.\n\n" + "\n".join(lines))
    embed.set_footer(text=FOOTER_TEXT, icon_url=footer_image())
    controls = [
        ipy.ActionRow(ipy.StringSelectMenu(*options, min_values=2, max_values=2,
                                          placeholder="🔁 Choose two slots to swap", custom_id=prefix + "swap")),
        ipy.ActionRow(
            ipy.Button(label="Reset", style=ipy.ButtonStyle.SECONDARY, custom_id=prefix + "reset"),
            ipy.Button(label="Confirm", emoji=TICK_EMOJI, style=ipy.ButtonStyle.SUCCESS, custom_id=prefix + "confirm"),
            ipy.Button(label="Cancel", emoji=CROSS_EMOJI, style=ipy.ButtonStyle.DANGER, custom_id=prefix + "cancel"),
        ),
    ]
    return embed, controls

import logging
from io import BytesIO
from pathlib import Path
from typing import Final

import anyio
import interactions as ipy
from aiohttp import ClientError
from PIL import Image, ImageDraw, ImageFont

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.features.finals.bracket import seeded_players
from elo_bot.features.finals.rendering import fitted_label, paste_avatar

VERSUS_TEMPLATE: Final = Path("assets/finals/elo_head_to_head.png")
RECORD_TEMPLATE: Final = Path("assets/finals/elo_head_to_head_record.png")
FINAL_MATCHUP_FILENAME: Final = "final-matchup.png"
FINAL_MATCHUP_URL: Final = f"attachment://{FINAL_MATCHUP_FILENAME}"
NAME_FIELDS: Final = ((467, 840), (1205, 840))
AVATAR_BOXES: Final = ((82, 768, 223, 909), (1450, 768, 1591, 909))
RANK_CENTERS: Final = ((282, 782), (1390, 782))
SCORE_CENTERS: Final = ((782.5, 840), (889.5, 840))


def _center_text(draw: ImageDraw.ImageDraw, label: tuple[str, ImageFont.FreeTypeFont],
                 center: tuple[float, float], color: str) -> None:
    text, font = label
    mask, (x_offset, y_offset) = font.getmask2(text)
    bounds = mask.getbbox()
    if bounds is None:
        return
    left, top, right, bottom = bounds
    draw.text((round(center[0] - x_offset - (left + right) / 2),
               round(center[1] - y_offset - (top + bottom) / 2)), text, font=font, fill=color)


def render_head_to_head(names: tuple[str, str], avatars: tuple[bytes | None, bytes | None],
                        ranks: tuple[int, int], record: tuple[int, int] | None = None) -> bytes:
    with (Image.open(VERSUS_TEMPLATE if record is None else RECORD_TEMPLATE) as artwork,
          artwork.convert("RGB") as image, BytesIO() as output):
        draw = ImageDraw.Draw(image)
        for name, avatar, rank, name_center, avatar_box, rank_center in zip(
            names, avatars, ranks, NAME_FIELDS, AVATAR_BOXES, RANK_CENTERS, strict=True,
        ):
            if avatar is not None:
                paste_avatar(image, avatar, avatar_box)
            _center_text(draw, fitted_label(name, 438, 42), name_center, "#3b2c2b")
            _center_text(draw, fitted_label(f"#{rank}", 76, 22), rank_center, "#ffedc1")
        if record is not None:
            scores = [fitted_label(str(wins), 67, 47) for wins in record]
            score_font = min((font for _, font in scores), key=lambda font: font.size)
            for (score, _), center in zip(scores, SCORE_CENTERS, strict=True):
                _center_text(draw, (score, score_font), center, "#ffedc1")
        image.save(output, format="PNG")
        return output.getvalue()


async def matchup_avatar(player: ipy.User) -> bytes | None:
    try:
        with anyio.fail_after(5):
            return await player.display_avatar.fetch(extension=".png", size=256)
    except (ClientError, ipy.errors.HTTPException, OSError, TimeoutError):
        return None


async def final_matchup_attachment(final: EloFinal, players: tuple[ipy.User, ipy.User]) -> ipy.File | None:
    first, second = players
    names = (final.player_nicknames.get(str(first.id), first.display_name),
             final.player_nicknames.get(str(second.id), second.display_name))
    seeds = {user: seed for seed, user in enumerate(seeded_players(final), 1)}
    ranks = (seeds[str(first.id)], seeds[str(second.id)])
    avatars = (await matchup_avatar(first), await matchup_avatar(second))
    try:
        png = await anyio.to_thread.run_sync(render_head_to_head, names, avatars, ranks)
    except (OSError, FinalError):
        logging.exception("Could not render the Final matchup graphic")
        return None
    return ipy.File(BytesIO(png), file_name=FINAL_MATCHUP_FILENAME,
                    description=f"{names[0]} (seed #{ranks[0]}) vs. {names[1]} (seed #{ranks[1]})")

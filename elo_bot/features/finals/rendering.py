from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageOps

from elo_bot.core.finals import FinalError, NODE_IDS, FINAL_DISTRICTS
from elo_bot.db import EloFinal
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.db.final_models import FinalResultSnapshot

TEMPLATE = Path("assets/finals/elo_final_bracket.png")
SCOREBOARD_TEMPLATE = Path("assets/finals/elo_final_scoreboard.png")
FONT = Path("assets/finals/supercell_magic_webfont.ttf")
FALLBACK_FONTS = (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
TEMPLATE_SIZE = (1672, 941)
ENTRANT_CENTERS = ((404, 519), (404, 568), (404, 618), (404, 667),
                   (404, 718), (404, 769), (404, 817), (404, 866))
SEMIFINAL_CENTERS = ((691, 554), (691, 651), (691, 743), (691, 838))
FINAL_CENTERS = ((974, 612), (974, 784))
CHAMPION_CENTER = (1258, 664)
# Safe interiors measured on the 1672 x 941 scoreboard artwork.
SCOREBOARD_NAMES = ((434, 214, 640, 258), (1032, 214, 1238, 258))
SCOREBOARD_AVATARS = ((286, 150, 416, 280), (1255, 150, 1385, 280))
SCOREBOARD_ROWS = (342, 409, 476, 543, 610, 677, 744)
SCOREBOARD_COLUMNS = (((448, 532), (552, 704)), ((1076, 1160), (1180, 1332)))
SCOREBOARD_TOTALS = (((425, 509), (533, 685)), ((1102, 1186), (1204, 1352)))


def bracket_labels(final: EloFinal, picks: dict[str, str] | None = None) -> list[str]:
    if not final.nodes:
        raise FinalError("Create the bracket first.")

    def name(user: str | None, empty: str = "TBD") -> str:
        return final.player_nicknames.get(user, user) if user is not None else empty

    labels = []
    for node_id in NODE_IDS[:4]:
        node = final.nodes[node_id]
        labels.extend(name(user, "BYE" if node.bye else "TBD") for user in node.contestants)
    for node_id in NODE_IDS[:4]:
        node = final.nodes[node_id]
        winner = node.winner if picks is None else picks.get(node_id)
        labels.append(name(winner, "TIE" if picks is None and node.state == "TIED" else "TBD"))
    for node_id in NODE_IDS[4:6]:
        node = final.nodes[node_id]
        winner = node.winner if picks is None else picks.get(node_id)
        labels.append(name(winner, "TIE" if picks is None and node.state == "TIED" else "TBD"))
    champion_empty = "TIE" if picks is None and final.nodes["F1"].state == "TIED" else "TBD"
    labels.append(name(final.champion if picks is None else picks.get("F1"), champion_empty))
    return labels


def fitted_label(text: str, width: int = 168, size: int = 24) -> tuple[str, ImageFont.FreeTypeFont]:
    text = " ".join(text.split())
    candidates = (FONT,) if text.isascii() else FALLBACK_FONTS
    font_path = None
    for path in candidates:
        if not path.is_file():
            continue
        probe = ImageFont.truetype(str(path), 24)
        missing = bytes(probe.getmask(chr(0x10FFFF)))
        if all(char.isspace() or bytes(probe.getmask(char)) != missing for char in text):
            font_path = path
            break
    if font_path is None:
        raise FinalError("A bracket font for these names is unavailable. The text bracket is still available.")
    font = ImageFont.truetype(str(font_path), 14)
    for font_size in range(size, 13, -1):
        font = ImageFont.truetype(str(font_path), font_size)
        bounds = font.getbbox(text, stroke_width=1)
        if bounds[2] - bounds[0] <= width:
            return text, font
    bounds = font.getbbox(text + "…", stroke_width=1)
    while text and bounds[2] - bounds[0] > width:
        text = text[:-1]
        bounds = font.getbbox(text + "…", stroke_width=1)
    return text + "…", font


def render_bracket(final: EloFinal, picks: dict[str, str] | None = None,
                   statuses: dict[str, str] | None = None) -> bytes:
    try:
        with Image.open(TEMPLATE) as source:
            image = source.convert("RGB")
    except (FileNotFoundError, OSError) as error:
        raise FinalError("The bracket image is unavailable. The text bracket is still available.") from error
    if image.size != TEMPLATE_SIZE:
        raise FinalError("The bracket template dimensions changed. The text bracket is still available.")
    draw = ImageDraw.Draw(image)
    centers = (*ENTRANT_CENTERS, *SEMIFINAL_CENTERS, *FINAL_CENTERS, CHAMPION_CENTER)
    plates = [(user, node_id) for node_id in NODE_IDS[:4] for user in final.nodes[node_id].contestants]
    plates.extend((final.nodes[key].winner if picks is None else picks.get(key),
                   next((later for later in NODE_IDS[4:] if key in final.nodes[later].feeders), "F1"))
                  for key in NODE_IDS[:6])
    plates.append((final.champion if picks is None else picks.get("F1"), "F1"))
    for label, center, (user, node_id) in zip(bracket_labels(final, picks), centers, plates, strict=True):
        text, font = fitted_label(label, width=188 if center == CHAMPION_CENTER else 168)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        position = (center[0] - (left + right) / 2, center[1] - (top + bottom) / 2)
        color = "white"
        if picks is None:
            node = final.nodes[node_id]
            if label == "BYE" or user is not None and node.winner is not None and user != node.winner:
                color = "#8d90c2"
            if center == CHAMPION_CENTER and final.champion is not None:
                color = "#ffd75e"
        draw.text(position, text, font=font, fill=color, stroke_width=1, stroke_fill="#1b1940")
    if picks is not None and statuses:
        for node_id, center in zip(NODE_IDS, (*SEMIFINAL_CENTERS, *FINAL_CENTERS, CHAMPION_CENTER), strict=True):
            status = statuses.get(node_id)
            if node_id not in picks or status not in ("correct", "incorrect", "excluded"):
                continue
            x, y = center[0] + (115 if node_id == "F1" else 102), center[1]
            color = {"correct": "#147a38", "incorrect": "#a32727", "excluded": "#4b5260"}[status]
            draw.ellipse((x - 14, y - 14, x + 14, y + 14), fill=color, outline="white", width=2)
            if status == "correct":
                draw.line(((x - 7, y), (x - 2, y + 5), (x + 8, y - 6)), fill="white", width=3)
            elif status == "incorrect":
                draw.line(((x - 6, y - 6), (x + 6, y + 6)), fill="white", width=3)
                draw.line(((x - 6, y + 6), (x + 6, y - 6)), fill="white", width=3)
            else:
                draw.line(((x - 7, y), (x + 7, y)), fill="white", width=3)
    with BytesIO() as output:
        image.save(output, format="PNG")
        image.close()
        return output.getvalue()


def render_scoreboard(final: EloFinal, snapshot: FinalResultSnapshot, avatars: dict[str, bytes]) -> bytes:
    """Render the saved scores in district order, preserving all template artwork."""
    try:
        with Image.open(SCOREBOARD_TEMPLATE) as source:
            image = source.convert("RGB")
        if image.size != TEMPLATE_SIZE:
            raise FinalError("The scoreboard template dimensions changed. The text result is still available.")
        draw = ImageDraw.Draw(image)
        labels = []
        for side, user in enumerate(snapshot.contestants):
            color = "#8d90c2" if snapshot.winner is not None and user != snapshot.winner else "white"
            labels.append((final.player_nicknames.get(user, user), SCOREBOARD_NAMES[side], color, 26))
            if user in avatars:
                try:
                    with Image.open(BytesIO(avatars[user])) as source:
                        avatar = ImageOps.fit(source.convert("RGBA"), (130, 130), method=Image.Resampling.LANCZOS)
                except (OSError, Image.DecompressionBombError):
                    avatar = None
                if avatar is not None:
                    with avatar, Image.new("L", (520, 520)) as large_mask:
                        ImageDraw.Draw(large_mask).ellipse((0, 0, 519, 519), fill=255)
                        with large_mask.resize((130, 130), Image.Resampling.LANCZOS) as mask:
                            with ImageChops.multiply(mask, avatar.getchannel("A")) as alpha:
                                image.paste(avatar, SCOREBOARD_AVATARS[side][:2], alpha)
            stars, destruction = [], []
            for index, (_district, y) in enumerate(zip(FINAL_DISTRICTS, SCOREBOARD_ROWS, strict=True)):
                score = snapshot.scores[user][index]
                stars.append(get_stars(score))
                destruction.append(get_destruction(score))
                for value, (left, right) in zip((str(stars[-1]), f"{destruction[-1]}%"),
                                               SCOREBOARD_COLUMNS[side], strict=True):
                    labels.append((value, (left, y - 22, right, y + 22), color, 34))
            for value, (left, right) in zip((str(sum(stars)), f"{sum(destruction)}%"),
                                           SCOREBOARD_TOTALS[side], strict=True):
                labels.append((value, (left, 802, right, 856), color, 36))
        for label, box, color, size in labels:
            text, font = fitted_label(label, box[2] - box[0], size)
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
            position = ((box[0] + box[2] - left - right) / 2, (box[1] + box[3] - top - bottom) / 2)
            draw.text(position, text, font=font, fill=color, stroke_width=1, stroke_fill="#1b1940")
        with image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()
    except OSError as error:
        raise FinalError("The scoreboard image is unavailable. The text result is still available.") from error

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common.finals.types import FinalError, NODE_IDS
from common.utils import mention_user
from database_models import EloFinal


TEMPLATE = Path("assets/elo_final_bracket.png")
FONT = Path("assets/supercell_magic_webfont.ttf")
FALLBACK_FONTS = (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
TEMPLATE_SIZE = (1672, 941)
ENTRANT_CENTERS = ((404, 519), (404, 568), (404, 618), (404, 667),
                   (404, 718), (404, 769), (404, 817), (404, 866))
SEMIFINAL_CENTERS = ((691, 554), (691, 651), (691, 743), (691, 838))
FINAL_CENTERS = ((974, 612), (974, 784))
CHAMPION_CENTER = (1258, 664)


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
        labels.append(name(winner, "Awaiting decision" if picks is None and node.state == "TIED" else "TBD"))
    for node_id in NODE_IDS[4:6]:
        node = final.nodes[node_id]
        winner = node.winner if picks is None else picks.get(node_id)
        labels.append(name(winner, "Needs manager" if picks is None and node.state == "NEEDS_MANAGER" else "TBD"))
    labels.append(name(final.champion if picks is None else picks.get("F1")))
    return labels


def bracket_text(final: EloFinal, names: dict[str, str] | None = None) -> str:
    names = names or {}
    return "\n".join(
        f"{node_id}: " + " vs. ".join(names.get(user, mention_user(None, user))
                                    if user else ("BYE" if node.bye else "TBD")
                                    for user in node.contestants)
        + (f" → {names.get(node.winner, mention_user(None, node.winner))}" if node.winner else f" · {node.state}")
        for node_id, node in final.nodes.items()
    )


def fitted_label(text: str, width: int = 168) -> tuple[str, ImageFont.FreeTypeFont]:
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
    for size in range(24, 13, -1):
        font = ImageFont.truetype(str(font_path), size)
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
    for label, center in zip(bracket_labels(final, picks), centers, strict=True):
        text, font = fitted_label(label, width=188 if center == CHAMPION_CENTER else 168)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        position = (center[0] - (left + right) / 2, center[1] - (top + bottom) / 2)
        draw.text(position, text, font=font, fill="white", stroke_width=1, stroke_fill="#1b1940")
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

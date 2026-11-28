from io import BytesIO
from pathlib import Path
from collections.abc import Mapping, Sequence

from PIL import Image, ImageDraw, ImageChops, ImageOps

from elo_bot.core.finals import FinalError, NODE_IDS, FINAL_DISTRICTS
from elo_bot.db import EloFinal
from elo_bot.core.parsing import get_destruction, get_stars
from elo_bot.db.final_models import FinalResultSnapshot
from elo_bot.features.finals.graphic_labels import fitted_label

TEMPLATE = Path("assets/finals/elo_final_bracket.png")
SCOREBOARD_TEMPLATE = Path("assets/finals/elo_final_scoreboard.png")
ELO_KINGS_TEMPLATE = Path("assets/finals/elo_kings_leaderboard.png")
CAPITAL_TROPHY = Path("assets/finals/capital_trophy.png")
TEMPLATE_SIZE = (1672, 941)
BRACKET_CROP = (180, 0, 1492, 941)
SCOREBOARD_TEMPLATE_SIZE = (1672, 1033)
ENTRANT_CENTERS = ((224, 520), (224, 569), (224, 619), (224, 668),
                   (224, 719), (224, 770), (224, 818), (224, 867))
SEMIFINAL_CENTERS = ((511, 555), (511, 652), (511, 744), (511, 839))
FINAL_CENTERS = ((794, 613), (794, 785))
CHAMPION_CENTER = (1078, 666)
SCOREBOARD_NAMES = ((453, 336, 739, 386), (933, 336, 1219, 386))
SCOREBOARD_AVATARS = ((359, 325, 431, 397), (1241, 325, 1313, 397))
SCOREBOARD_ROWS = (473, 535, 597, 659, 721, 783, 845)
SCOREBOARD_STAR_RIGHTS = (441, 1005)
SCOREBOARD_DESTRUCTION_CENTERS = (655.5, 1219.5)
SCOREBOARD_TOTAL_CENTER = 922
SCOREBOARD_TOTAL_STAR_RIGHTS = (451.5, 1015.5)
SCOREBOARD_TOTAL_DESTRUCTION_CENTERS = (655.5, 1219.5)
ELO_KINGS_AVATAR_BOXES = ((475, 728, 507, 760), (475, 772, 507, 804), (475, 817, 507, 849), (475, 862, 507, 894))
ELO_KINGS_ROWS = len(ELO_KINGS_AVATAR_BOXES)


def bracket_labels(final: EloFinal, picks: dict[str, str] | None = None) -> list[str]:
    if not final.nodes:
        raise FinalError("Create the bracket first.")

    def name(user: str | None, empty: str = "") -> str:
        return final.player_nicknames.get(user, user) if user is not None else empty

    labels = []
    for node_id in NODE_IDS[:4]:
        node = final.nodes[node_id]
        labels.extend(name(user, "BYE" if node.bye else "") for user in node.contestants)
    for node_id in NODE_IDS[:6]:
        labels.append(name(final.nodes[node_id].winner if picks is None else picks.get(node_id)))
    labels.append(name(final.champion if picks is None else picks.get("F1")))
    return labels


# Pillow draws shapes without anti-aliasing, so badges are drawn this many times larger and scaled down.
BADGE_SUPERSAMPLE = 8
PREDICTION_MARKS = {
    "correct": ((8, 17), (11, 14), (16, 19), (25, 10), (28, 13), (16, 25)),
    "incorrect": ((13, 9), (18, 14), (23, 9), (26, 12), (21, 17), (26, 22),
                  (23, 25), (18, 20), (13, 25), (10, 22), (15, 17), (10, 12)),
    "excluded": ((10, 15), (26, 15), (26, 19), (10, 19)),
}
PREDICTION_ENAMEL = {"correct": "#245a46", "incorrect": "#843828", "excluded": "#4b5260"}


def status_badge(status: str) -> Image.Image:
    scale = BADGE_SUPERSAMPLE
    badge_size = 36
    size = badge_size * scale
    with Image.new("RGBA", (size, size)) as large:
        draw = ImageDraw.Draw(large)
        shield = ((7, 2), (29, 2), (34, 7), (34, 19), (30, 26), (18, 35), (6, 26), (2, 19), (2, 7))
        bevel = ((7, 3.5), (29, 3.5), (32.5, 7), (32.5, 19), (29, 25), (18, 33), (7, 25), (3.5, 19), (3.5, 7))
        face = ((8, 6.5), (28, 6.5), (30, 8.5), (30, 18), (27, 24), (18, 31), (9, 24), (6, 18), (6, 8.5))
        highlight = ((7, 3.5), (29, 3.5), (32.5, 7), (30, 8.5), (28, 6.5), (8, 6.5), (6, 8.5), (3.5, 7))
        for points, color in ((shield, "#4b342c"), (bevel, "#bd8f50"), (highlight, "#ffe6a0"),
                              (face, PREDICTION_ENAMEL[status]), (PREDICTION_MARKS[status], "#fff3d3")):
            draw.polygon([(x * scale, y * scale) for x, y in points], fill=color)
        return large.resize((badge_size, badge_size), Image.Resampling.LANCZOS)


def render_bracket(final: EloFinal, picks: dict[str, str] | None = None,
                   statuses: dict[str, str] | None = None, usernames: Mapping[str, str] | None = None) -> bytes:
    try:
        with Image.open(TEMPLATE) as source:
            if source.size != TEMPLATE_SIZE:
                raise FinalError("The bracket template dimensions changed. The text bracket is still available.")
            image = source.crop(BRACKET_CROP).convert("RGB")
    except (FileNotFoundError, OSError) as error:
        raise FinalError("The bracket image is unavailable. The text bracket is still available.") from error
    draw = ImageDraw.Draw(image)
    centers = (*ENTRANT_CENTERS, *SEMIFINAL_CENTERS, *FINAL_CENTERS, CHAMPION_CENTER)
    plates = [(user, node_id) for node_id in NODE_IDS[:4] for user in final.nodes[node_id].contestants]
    plates.extend((final.nodes[key].winner if picks is None else picks.get(key),
                   next((later for later in NODE_IDS[4:] if key in final.nodes[later].feeders), "F1"))
                  for key in NODE_IDS[:6])
    plates.append((final.champion if picks is None else picks.get("F1"), "F1"))
    usernames = usernames or {}
    for label, center, (user, node_id) in zip(bracket_labels(final, picks), centers, plates, strict=True):
        if user is None and not label:
            continue
        text, font = fitted_label(label, width=188 if center == CHAMPION_CENTER else 168,
                                  username=usernames.get(user) if user else None, default=user or label,
                                  prefer_plain_name=True)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
        position = (center[0] - (left + right) / 2, center[1] - (top + bottom) / 2)
        color = "#fff6e5"
        if picks is None:
            node = final.nodes[node_id]
            if label == "BYE" or user is not None and node.winner is not None and user != node.winner:
                color = "#e0c29d"
            if center == CHAMPION_CENTER and final.champion is not None:
                color = "#ffe6a0"
        draw.text(position, text, font=font, fill=color, stroke_width=1, stroke_fill="#413024")
    if picks is not None and statuses:
        for node_id, center in zip(NODE_IDS, (*SEMIFINAL_CENTERS, *FINAL_CENTERS, CHAMPION_CENTER), strict=True):
            status = statuses.get(node_id)
            if node_id not in picks or status not in ("correct", "incorrect", "excluded"):
                continue
            x, y = center[0] + (115 if node_id == "F1" else 102), center[1]
            with status_badge(status) as badge:
                image.paste(badge, (x - badge.width // 2, y - badge.height // 2), badge)
    with BytesIO() as output:
        image.save(output, format="PNG")
        image.close()
        return output.getvalue()


def render_scoreboard(final: EloFinal, snapshot: FinalResultSnapshot, avatars: dict[str, bytes],
                      usernames: Mapping[str, str] | None = None) -> bytes:
    """Render the saved scores in district order, preserving all template artwork."""
    try:
        with Image.open(SCOREBOARD_TEMPLATE) as source:
            image = source.convert("RGB")
        if image.size != SCOREBOARD_TEMPLATE_SIZE:
            raise FinalError("The scoreboard template dimensions changed. The text result is still available.")
        draw = ImageDraw.Draw(image)
        centered_labels = []
        usernames = usernames or {}
        star_labels = []
        for side, user in enumerate(snapshot.contestants):
            color = "#e2cfad" if user != snapshot.winner else "#fff0d5"
            name_color = "#e2cfad" if user != snapshot.winner else "#fff0d7"
            centered_labels.append((final.player_nicknames.get(user, user), SCOREBOARD_NAMES[side],
                                    name_color, 25, user))
            if user in avatars:
                paste_avatar(image, avatars[user], SCOREBOARD_AVATARS[side])
            stars, destruction = [], []
            for index, (_district, y) in enumerate(zip(FINAL_DISTRICTS, SCOREBOARD_ROWS, strict=True)):
                score = snapshot.scores[user][index]
                stars.append(get_stars(score))
                destruction.append(get_destruction(score))
                star_labels.append((str(stars[-1]), SCOREBOARD_STAR_RIGHTS[side], y, color, 27))
                center = SCOREBOARD_DESTRUCTION_CENTERS[side]
                centered_labels.append((f"{destruction[-1]}%", (center - 80, y - 22, center + 80, y + 22),
                                        color, 27, None))
            star_labels.append((str(sum(stars)), SCOREBOARD_TOTAL_STAR_RIGHTS[side], SCOREBOARD_TOTAL_CENTER,
                                color, 31))
            center = SCOREBOARD_TOTAL_DESTRUCTION_CENTERS[side]
            centered_labels.append((f"{sum(destruction)}%", (center - 90, SCOREBOARD_TOTAL_CENTER - 28,
                                        center + 90, SCOREBOARD_TOTAL_CENTER + 28), color, 31, None))
        for label, box, color, size, user in centered_labels:
            text, font = fitted_label(label, box[2] - box[0], size, username=usernames.get(user) if user else None,
                                      default=user or "Player", prefer_plain_name=True)
            left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=1)
            position = ((box[0] + box[2] - left - right) / 2, (box[1] + box[3] - top - bottom) / 2)
            draw.text(position, text, font=font, fill=color, stroke_width=1, stroke_fill="#3b2c2b")
        for label, right, center, color, size in star_labels:
            text, font = fitted_label(label, 80, size)
            _, top, _, bottom = font.getbbox(text, stroke_width=1, anchor="rs")
            draw.text((right, center - (top + bottom) / 2), text, font=font, fill=color, stroke_width=1,
                      stroke_fill="#3b2c2b", anchor="rs")
        with image, BytesIO() as output:
            image.save(output, format="PNG")
            return output.getvalue()
    except OSError as error:
        raise FinalError("The scoreboard image is unavailable. The text result is still available.") from error


def paste_avatar(image: Image.Image, data: bytes, box: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = box
    size = (right - left, bottom - top)
    large_size = (4 * size[0], 4 * size[1])
    try:
        with Image.open(BytesIO(data)) as source:
            avatar = ImageOps.fit(source.convert("RGBA"), size, method=Image.Resampling.LANCZOS)
    except (OSError, Image.DecompressionBombError):
        return
    with avatar, Image.new("L", large_size) as large_mask:
        ImageDraw.Draw(large_mask).ellipse((0, 0, large_size[0] - 1, large_size[1] - 1), fill=255)
        with large_mask.resize(size, Image.Resampling.LANCZOS) as mask:
            with ImageChops.multiply(mask, avatar.getchannel("A")) as alpha:
                image.paste(avatar, (left, top), alpha)


def render_elo_kings(rows: Sequence[tuple[str, int, bytes | None]]) -> bytes:
    with (Image.open(ELO_KINGS_TEMPLATE) as artwork, artwork.convert("RGB") as image,
          Image.open(CAPITAL_TROPHY) as icon, ImageOps.contain(icon, (26, 26)) as trophy, BytesIO() as output):
        draw = ImageDraw.Draw(image)
        gap = 8
        value_width = 100
        score_left = 1143 - (value_width + gap + trophy.width) // 2
        value_center = score_left + value_width / 2
        trophy_x = score_left + value_width + gap
        for index, ((name, peak_elo, avatar), avatar_box) in enumerate(zip(rows, ELO_KINGS_AVATAR_BOXES, strict=False)):
            y = (avatar_box[1] + avatar_box[3]) // 2
            if avatar is not None:
                paste_avatar(image, avatar, avatar_box)
            label, label_font = fitted_label(name, 507, 25)
            left, _, _, _ = label_font.getbbox(label)
            _, cap_top, _, cap_bottom = label_font.getbbox("H")
            label_x, label_y = 519 - left, y - (cap_top + cap_bottom) / 2
            player, separator, title = label.partition(" | ")
            draw.text((label_x, label_y), player, font=label_font, fill="#3b2b25")
            if separator:
                bar_x = label_x + draw.textlength(f"{player} ", font=label_font)
                bar_x += draw.textlength("|", font=label_font) / 2
                draw.line((bar_x, y - 10, bar_x, y + 10), fill="#3b2b25", width=2)
                title_x = label_x + draw.textlength(f"{player} | ", font=label_font)
                draw.text((title_x, label_y), title, font=label_font, fill="#3b2b25")

            value, value_font = fitted_label(str(peak_elo), value_width, 27)
            left, top, right, bottom = draw.textbbox((0, 0), value, font=value_font)
            score_y = 743 + 45 * index
            draw.text((value_center - (left + right) / 2, score_y - (top + bottom) / 2), value,
                      font=value_font, fill="#fff3d3")
            image.paste(trophy, (trophy_x, score_y - trophy.height // 2), trophy)
        image.save(output, format="PNG")
        return output.getvalue()

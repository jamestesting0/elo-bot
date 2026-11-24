from pathlib import Path
from unicodedata import category, normalize

from PIL import ImageFont

from elo_bot.core.finals import FinalError

FONT = Path("assets/finals/supercell_magic_webfont.ttf")
FALLBACK_FONTS = (Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
                  Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"))
MIN_FONT_SIZE = 14
JOINERS = "\u200c\u200d"


def _single_line(text: str) -> str:
    clean = "".join(char for char in text if char.isspace() or char in JOINERS
                    or category(char) not in {"Cc", "Cf", "Cs"})
    return normalize("NFC", " ".join(clean.split()))


def _plain_name(text: str) -> str:
    return " ".join("".join(
        char if char.isascii() and (char.isalnum() or char in "._-'") else " "
        for char in normalize("NFKD", text) if not category(char).startswith("M")
    ).split()).strip("._-'")


def _fit(text: str, probe: ImageFont.FreeTypeFont, width: float, size: int,
         suffix: str) -> tuple[str, ImageFont.FreeTypeFont] | None:
    max_height = size + 12
    for font_size in range(size, MIN_FONT_SIZE - 1, -1):
        font = probe.font_variant(size=font_size)
        left, top, right, bottom = font.getbbox(text, stroke_width=1)
        if right - left <= width and bottom - top <= max_height:
            return text, font
        if font_size > MIN_FONT_SIZE or bottom - top > max_height:
            continue
        for end in range(len(text) - 1, 0, -1):
            shortened = text[:end].rstrip(f" {JOINERS}")
            left, top, right, bottom = font.getbbox(shortened + suffix, stroke_width=1)
            if right - left <= width and bottom - top <= max_height and font.getmask(shortened).getbbox():
                return shortened + suffix, font
    return None


def fitted_label(text: str, width: float = 168, size: int = 24, *, username: str | None = None,
                 default: str = "Player") -> tuple[str, ImageFont.FreeTypeFont]:
    text = _single_line(text)
    candidates = (text, normalize("NFKC", text), _single_line(username or ""), _plain_name(text), default, "Player")

    paths = tuple(path for path in (FONT, *FALLBACK_FONTS) if path.is_file())
    probes: dict[Path, tuple[ImageFont.FreeTypeFont, bytes]] = {}
    for label in dict.fromkeys(candidates):
        visible = set(label).difference(f" {JOINERS}")
        if not any(category(char)[0] in "LNPS" for char in visible):
            continue
        for path in paths:
            if path not in probes:
                font = ImageFont.truetype(str(path), 24)
                probes[path] = font, bytes(font.getmask("\U0010ffff"))
            font, missing = probes[path]
            if any(bytes(font.getmask(char)) == missing for char in visible) or not font.getmask(label).getbbox():
                continue
            suffix = "..." if bytes(font.getmask("…")) == missing else "…"
            if fitted := _fit(label, font, width, size, suffix):
                return fitted
    raise FinalError("A bracket font for these names is unavailable. The text bracket is still available.")

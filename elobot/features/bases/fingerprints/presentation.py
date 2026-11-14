from pathlib import Path
from typing import Final

from PIL import Image, ImageDraw, ImageFont


BACKGROUND: Final = "#111827"
CARD: Final = "#1e293b"
BORDER: Final = "#334155"
TEXT: Final = "#e2e8f0"
ACCENTS: Final = ("#38bdf8", "#a78bfa")
GAP: Final = 16
PADDING: Final = 12
HEADER: Final = 48


def render_panels(panels: tuple[Image.Image, Image.Image], output: Path) -> None:
    """Frame uploaded and found screenshots with headings outside their image area."""
    width = max(panel.width for panel in panels)
    height = max(panel.height for panel in panels)
    card_width = width + 2 * PADDING
    card_height = height + HEADER + PADDING
    font = ImageFont.load_default(size=24)
    with Image.new("RGB", (2 * card_width + 3 * GAP, card_height + 2 * GAP), BACKGROUND) as canvas:
        draw = ImageDraw.Draw(canvas)
        for side, (panel, title) in enumerate(zip(panels, ("Uploaded Base", "Found Base"), strict=True)):
            x = GAP + side * (card_width + GAP)
            draw.rounded_rectangle((x, GAP, x + card_width - 1, GAP + card_height - 1),
                                   radius=14, fill=CARD, outline=BORDER)
            draw.rounded_rectangle((x + PADDING, GAP + 13, x + PADDING + 3, GAP + 32),
                                   radius=2, fill=ACCENTS[side])
            draw.text((x + PADDING + 14, GAP + 10), title, fill=TEXT, font=font)
            canvas.paste(panel, (x + PADDING + (width - panel.width) // 2,
                                 GAP + HEADER + (height - panel.height) // 2))
        canvas.save(output, format="PNG")

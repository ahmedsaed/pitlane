"""Spoiler-safe 1280×720 title cards (docs/replays.md "Title cards"). Never contain results."""

from __future__ import annotations

import io
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from ..models import Event, Session
from .spoilers import safe_summary


@lru_cache(maxsize=8)
def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)  # Pillow's bundled scalable font
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def render_card(event: Event, session: Session | None, title: str) -> bytes:
    img = Image.new("RGB", (1280, 720), (18, 20, 28))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 12], fill=(225, 6, 0))
    # Round badge
    d.rounded_rectangle([80, 90, 300, 190], radius=18, fill=(225, 6, 0))
    d.text((190, 140), f"R{event.round}", font=_font(64), fill="white", anchor="mm")
    d.text((80, 260), event.name, font=_font(72), fill="white")
    d.text((80, 360), title, font=_font(56), fill=(230, 230, 230))
    d.text((80, 470), safe_summary(event, session).replace("·", "|"), font=_font(32), fill=(170, 170, 180))
    d.text((80, 620), str(event.season), font=_font(40), fill=(120, 120, 130))
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

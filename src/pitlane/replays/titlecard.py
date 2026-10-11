"""Spoiler-safe 1280×720 title cards (docs/replays.md "Title cards"). Never contain results."""

from __future__ import annotations

import io
from datetime import tzinfo
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
    return _png(img)


def render_next_card(event: Event, session: Session, tz: tzinfo) -> bytes:
    """Guide artwork for F1 filler: what is on next and when (local time), plus the rest of the weekend."""
    img = Image.new("RGB", (1280, 720), (18, 20, 28))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 12], fill=(225, 6, 0))
    d.rounded_rectangle([80, 80, 380, 160], radius=16, fill=(225, 6, 0))
    d.text((230, 120), "NEXT UP", font=_font(48), fill="white", anchor="mm")
    d.text((80, 220), event.name, font=_font(64), fill="white")
    local = session.starts_at.astimezone(tz)
    d.text((80, 310), session.title, font=_font(56), fill=(230, 230, 230))
    d.text((80, 390), f"{local:%A %d %B, %H:%M} {local.tzname()}", font=_font(40), fill=(225, 6, 0))
    y = 480
    for s in [s for s in event.sessions if s.starts_at > session.starts_at][:4]:
        t = s.starts_at.astimezone(tz)
        d.text((80, y), f"{t:%a %H:%M}", font=_font(30), fill=(170, 170, 180))
        d.text((260, y), s.title, font=_font(30), fill=(170, 170, 180))
        y += 44
    d.text((1200, 660), f"Round {event.round} · {event.season}".replace("·", "|"), font=_font(32), fill=(120, 120, 130), anchor="rs")
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

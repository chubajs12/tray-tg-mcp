"""Своя простая иконка приложения (облачко-реплика с тремя точками), цвет по состоянию. Не логотип Telegram."""

from __future__ import annotations

from PIL import Image, ImageDraw

COLORS = {
    "no_api": (128, 128, 128),
    "no_accounts": (128, 128, 128),
    "need_login": (230, 140, 20),
    "connecting": (220, 190, 30),
    "online": (51, 144, 236),
    "error": (210, 50, 50),
}


def make_icon(state: str, size: int = 64) -> Image.Image:
    color = COLORS.get(state, COLORS["error"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64
    d.rounded_rectangle((4 * s, 6 * s, 60 * s, 46 * s), radius=12 * s, fill=color + (255,),
                        outline=(255, 255, 255, 255), width=max(1, int(2 * s)))
    d.polygon([(14 * s, 44 * s), (12 * s, 60 * s), (28 * s, 45 * s)], fill=color + (255,))
    for cx in (18, 32, 46):
        r = 5 * s
        d.ellipse((cx * s - r, 26 * s - r, cx * s + r, 26 * s + r), fill=(255, 255, 255, 255))
    return img

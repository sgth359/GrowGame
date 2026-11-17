"""Генерирует картинки локаций в images/<ключ локации>.jpg по game.json.
Запуск: python make_images.py   (нужен Pillow: pip install pillow)
Любую картинку можно заменить своей (из нейросети): положи файл с тем же именем."""
import json
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(__file__).parent
OUT = BASE / "images"
OUT.mkdir(exist_ok=True)
CFG = json.loads((BASE / "game.json").read_text(encoding="utf-8"))
S = 2
W, H = 900 * S, 640 * S
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
GROUND_Y = int(H * 0.60)

# небо верх/низ, земля верх/низ
THEMES = {
    "пляж":     dict(sky=("#5ec6f0", "#d8f1fb"), ground=("#efd9a2", "#c9a86a")),
    "лес":      dict(sky=("#1d3b2a", "#4f7d57"), ground=("#4a3a26", "#2a1f14")),
    "холмы":    dict(sky=("#7fb8e8", "#e6f2fb"), ground=("#6fae4a", "#3f7a2c")),
    "пустыня":  dict(sky=("#f0a35a", "#fde0a8"), ground=("#e2b25f", "#b8863a")),
    "кладбище": dict(sky=("#1a1030", "#5b4a7d"), ground=("#34303d", "#1c1a22")),
    "пещеры":   dict(sky=("#0b0d14", "#1c2233"), ground=("#3a3d4a", "#1a1c24")),
    "руины":    dict(sky=("#9fb7c9", "#e9eef2"), ground=("#b7ab8c", "#8a7f64")),
    "остров":   dict(sky=("#2a7fc4", "#bfe6f7"), ground=("#e6cf96", "#bd9a58")),
}


def rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    a, b = rgb(a), rgb(b)
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def vgrad(d, y0, y1, c1, c2):
    for y in range(y0, y1):
        d.line([(0, y), (W, y)], fill=mix(c1, c2, (y - y0) / max(1, y1 - y0)))


def draw_props(key, d, rnd):
    gy = GROUND_Y
    if key == "пляж":
        d.rectangle([0, int(gy - 70 * S), W, gy - 10 * S], fill=rgb("#2b8fd1"))
        d.rectangle([0, int(gy - 10 * S), W, gy + 6 * S], fill=rgb("#cfeefc"))
        d.ellipse([W * .76, 50 * S, W * .76 + 110 * S, 160 * S], fill=rgb("#fff3b0"))
    elif key == "лес":
        for i in range(14):
            x = rnd.randint(0, W)
            h = rnd.randint(200, 330) * S
            col = mix("#173a24", "#2d6a3c", rnd.random())
            d.rectangle([x - 8 * S, gy - 40 * S, x + 8 * S, gy], fill=rgb("#3b2a1a"))
            for k in range(3):
                d.polygon([(x - (90 - 18 * k) * S, gy - (40 + 70 * k) * S), (x + (90 - 18 * k) * S, gy - (40 + 70 * k) * S),
                           (x, gy - h + 50 * k * S)], fill=col)
    elif key == "холмы":
        for cx, r, c in [(.2, 260, "#7cbc54"), (.55, 330, "#5fa043"), (.88, 250, "#7cbc54")]:
            d.ellipse([W * cx - r * S, gy - r * S * .55, W * cx + r * S, gy + r * S * .6], fill=rgb(c))
        d.ellipse([W * .1, 40 * S, W * .1 + 120 * S, 150 * S], fill=rgb("#fff6c4"))
    elif key == "пустыня":
        for cx, r, c in [(.18, 330, "#e9b968"), (.6, 420, "#dba755"), (.95, 300, "#e9b968")]:
            d.ellipse([W * cx - r * S, gy - r * S * .30, W * cx + r * S, gy + r * S * .7], fill=rgb(c))
        d.ellipse([W * .8, 40 * S, W * .8 + 130 * S, 170 * S], fill=rgb("#fff0b0"))
        x = int(W * .14)
        d.rounded_rectangle([x, gy - 150 * S, x + 26 * S, gy], 12 * S, fill=rgb("#2f7a3a"))
        d.rounded_rectangle([x - 40 * S, gy - 110 * S, x - 20 * S, gy - 50 * S], 10 * S, fill=rgb("#2f7a3a"))
        d.rounded_rectangle([x - 40 * S, gy - 60 * S, x, gy - 44 * S], 8 * S, fill=rgb("#2f7a3a"))
    elif key == "кладбище":
        d.ellipse([W * .72, 40 * S, W * .72 + 120 * S, 160 * S], fill=rgb("#f1eed8"))
        for i in range(9):
            x = rnd.randint(40 * S, W - 40 * S)
            h = rnd.randint(60, 120) * S
            d.rounded_rectangle([x - 28 * S, gy - h, x + 28 * S, gy + 8 * S], 24 * S, fill=rgb("#6e6a7a"))
            d.rectangle([x - 4 * S, gy - h + 14 * S, x + 4 * S, gy - h + 46 * S], fill=rgb("#4a4756"))
            d.rectangle([x - 14 * S, gy - h + 24 * S, x + 14 * S, gy - h + 32 * S], fill=rgb("#4a4756"))
    elif key == "пещеры":
        for i in range(16):
            x = rnd.randint(0, W)
            h = rnd.randint(60, 190) * S
            d.polygon([(x - 28 * S, 0), (x + 28 * S, 0), (x, h)], fill=rgb("#2a2f40"))
        for i in range(9):
            x = rnd.randint(40 * S, W - 40 * S)
            h = rnd.randint(50, 120) * S
            c = rnd.choice(["#4fe3ff", "#9b7bff", "#5dffb0"])
            d.polygon([(x - 18 * S, gy), (x, gy - h), (x + 18 * S, gy)], fill=rgb(c))
    elif key == "руины":
        for i in range(6):
            x = int(W * (.1 + i * .16))
            h = rnd.randint(130, 260) * S
            d.rectangle([x - 22 * S, gy - h, x + 22 * S, gy], fill=rgb("#cfc6a8"))
            d.rectangle([x - 32 * S, gy - h - 14 * S, x + 32 * S, gy - h + 6 * S], fill=rgb("#b9af90"))
            for k in range(3):
                d.line([(x - 12 * S + k * 12 * S, gy - h + 10 * S), (x - 12 * S + k * 12 * S, gy)], fill=rgb("#a69c80"), width=3 * S)
    elif key == "остров":
        d.rectangle([0, int(gy - 90 * S), W, gy - 10 * S], fill=rgb("#1f78b8"))
        x = int(W * .78)
        d.line([(x, gy), (x - 20 * S, gy - 190 * S)], fill=rgb("#6b4a2a"), width=14 * S)
        for a in (-150, -110, -70, -30, 10):
            ang = math.radians(a)
            ex, ey = x - 20 * S + math.cos(ang) * 120 * S, gy - 190 * S + math.sin(ang) * 70 * S
            d.line([(x - 20 * S, gy - 190 * S), (ex, ey)], fill=rgb("#2f8a3a"), width=12 * S)
        d.ellipse([W * .1, 50 * S, W * .1 + 110 * S, 160 * S], fill=rgb("#fff3b0"))


def draw_dig_site(d, key):
    """Яма с сундуком и лопата — общий передний план."""
    cx, cy = int(W * .5), int(H * .74)
    d.ellipse([cx - 230 * S, cy - 48 * S, cx + 230 * S, cy + 70 * S], fill=rgb("#2a1c10"))      # края ямы
    d.ellipse([cx - 190 * S, cy - 36 * S, cx + 190 * S, cy + 52 * S], fill=rgb("#120a05"))      # дно
    # свечение
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - 230 * S, cy - 130 * S, cx + 230 * S, cy + 60 * S], fill=(255, 205, 90, 120))
    return glow, cx, cy


def chest(d, cx, cy):
    x0, x1 = cx - 78 * S, cx + 78 * S
    d.rounded_rectangle([x0, cy - 62 * S, x1, cy + 22 * S], 10 * S, fill=rgb("#8a5a2b"))
    d.rounded_rectangle([x0, cy - 92 * S, x1, cy - 50 * S], 26 * S, fill=rgb("#a76d33"))
    d.rectangle([x0, cy - 54 * S, x1, cy - 44 * S], fill=rgb("#e2b84a"))
    d.rectangle([cx - 8 * S, cy - 58 * S, cx + 8 * S, cy - 24 * S], fill=rgb("#f3d36b"))
    for x in (x0 + 16 * S, x1 - 30 * S):
        d.rectangle([x, cy - 92 * S, x + 14 * S, cy + 22 * S], fill=rgb("#e2b84a"))
    for i in range(7):                                                                       # монеты
        px = cx - 60 * S + i * 20 * S
        d.ellipse([px, cy - 104 * S, px + 22 * S, cy - 86 * S], fill=rgb("#ffd54a"))


def shovel(d):
    x, y = int(W * .84), int(H * .9)
    d.line([(x, y), (x - 90 * S, y - 330 * S)], fill=rgb("#7a4f26"), width=14 * S)
    d.rounded_rectangle([x - 110 * S, y - 360 * S, x - 70 * S, y - 330 * S], 8 * S, fill=rgb("#7a4f26"))
    d.polygon([(x - 36 * S, y - 20 * S), (x + 40 * S, y - 20 * S), (x + 52 * S, y + 50 * S),
               (x + 2 * S, y + 90 * S), (x - 48 * S, y + 50 * S)], fill=rgb("#aeb6bf"))


def make(key, loc):
    rnd = random.Random(key)
    th = THEMES.get(key, THEMES["холмы"])
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    vgrad(d, 0, GROUND_Y, *th["sky"])
    vgrad(d, GROUND_Y, H, *th["ground"])
    draw_props(key, d, rnd)
    glow, cx, cy = draw_dig_site(d, key)
    chest(d, cx, cy)
    shovel(d)
    glow = glow.filter(ImageFilter.GaussianBlur(40 * S))
    img = Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")
    d = ImageDraw.Draw(img, "RGBA")
    bar = 112 * S
    d.rectangle([0, H - bar, W, H], fill=(0, 0, 0, 150))
    d.text((40 * S, H - bar + 12 * S), loc["name"], font=ImageFont.truetype(FONT_B, 46 * S), fill=(255, 255, 255))
    d.text((40 * S, H - bar + 70 * S), f"Уровень {loc['min_level']}+ · " + loc["desc"],
           font=ImageFont.truetype(FONT_R, 24 * S), fill=(225, 225, 225))
    img = img.resize((W // S, H // S), Image.LANCZOS)
    img.save(OUT / f"{key}.jpg", quality=90)
    print("saved", OUT / f"{key}.jpg")


if __name__ == "__main__":
    for k, l in CFG["locations"].items():
        make(k, l)

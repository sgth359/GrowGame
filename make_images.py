"""Генерирует картинки сортов в images/. Запуск: python make_images.py
Заменить любую картинку своей (нейросеть) можно, просто положив файл с тем же именем."""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "images"
OUT.mkdir(exist_ok=True)
S = 2                      # суперсэмплинг для сглаживания
W, H = 900 * S, 700 * S
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

STRAINS = {
    "дыня":   dict(name="Кислая Дыня",       sub="Уровень 1 · быстрый",  bg=("#1b3a2a", "#4b8f4a"), leaf=("#7fd957", "#3f9a3a"), bud=None),
    "лимон":  dict(name="Лимонный Туман",    sub="Уровень 2 · средний",  bg=("#2d3a14", "#a3b832"), leaf=("#c8e04a", "#6f9a1f"), bud="#f4e04d"),
    "гелато": dict(name="Фиолетовое Гелато", sub="Уровень 4 · редкий",   bg=("#241339", "#6a3fa0"), leaf=("#8a5bd0", "#3f8f4a"), bud="#d98cff"),
}


def hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def mix(a, b, t):
    a, b = hex2rgb(a), hex2rgb(b)
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(c1, c2):
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=mix(c1, c2, y / H))
    return img


def leaflet(d, base, angle, length, width, c_dark, c_light):
    pts_l, pts_r = [], []
    n = 18
    for i in range(n + 1):
        t = i / n
        w = width * math.sin(math.pi * t ** 0.8) * (1 - 0.15 * t)
        x = base[0] + math.cos(angle) * length * t
        y = base[1] + math.sin(angle) * length * t
        nx, ny = -math.sin(angle), math.cos(angle)
        pts_l.append((x + nx * w, y + ny * w))
        pts_r.append((x - nx * w, y - ny * w))
    poly = pts_l + pts_r[::-1]
    d.polygon(poly, fill=hex2rgb(c_dark))
    # светлая половина листа + жилка
    half = pts_l + [(base[0] + math.cos(angle) * length * (i / n), base[1] + math.sin(angle) * length * (i / n))
                    for i in range(n, -1, -1)]
    d.polygon(half, fill=hex2rgb(c_light))
    d.line([base, (base[0] + math.cos(angle) * length * 0.95, base[1] + math.sin(angle) * length * 0.95)],
           fill=mix(c_dark, "#000000", 0.25), width=max(2, S * 2))


def leaf(d, base, rot, size, colors):
    angles = [0, 28, -28, 56, -56, 82, -82]
    lens = [1.0, 0.9, 0.9, 0.7, 0.7, 0.45, 0.45]
    for a, l in zip(angles, lens):
        leaflet(d, base, rot + math.radians(a), size * l, size * 0.13 * (0.6 + l * 0.4), colors[1], colors[0])


def make(key, cfg):
    random.seed(key)
    img = gradient(*cfg["bg"])
    # мягкое свечение за растением
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([W * .22, H * .05, W * .78, H * .75], fill=mix(cfg["bg"][1], "#ffffff", 0.35))
    glow = glow.filter(ImageFilter.GaussianBlur(120 * S))
    img = Image.blend(img, Image.composite(glow, img, glow.convert("L")), 0.55)
    d = ImageDraw.Draw(img, "RGBA")

    cx = W // 2
    # пол
    d.rectangle([0, int(H * .82), W, H], fill=(0, 0, 0, 90))

    # горшок
    top, bot = int(H * .66), int(H * .86)
    d.polygon([(cx - 170 * S, top), (cx + 170 * S, top), (cx + 125 * S, bot), (cx - 125 * S, bot)],
              fill=hex2rgb("#b5603a"))
    d.polygon([(cx - 170 * S, top), (cx + 170 * S, top), (cx + 165 * S, top + 26 * S), (cx - 165 * S, top + 26 * S)],
              fill=hex2rgb("#d17a4d"))
    d.ellipse([cx - 165 * S, top - 10 * S, cx + 165 * S, top + 22 * S], fill=hex2rgb("#3b2416"))

    # стебель
    stem_top = int(H * .22)
    d.line([(cx, top), (cx, stem_top)], fill=hex2rgb("#2f6b2a"), width=10 * S)

    # листья по высоте стебля
    levels = [(0.0, 150), (0.25, 190), (0.5, 175), (0.75, 135)]
    for i, (t, size) in enumerate(levels):
        y = int(top - 30 * S - (top - stem_top - 40 * S) * t)
        for side, base_ang in ((-1, math.radians(200)), (1, math.radians(-20))):
            d.line([(cx, y), (cx + side * 50 * S, y - 25 * S)], fill=hex2rgb("#2f6b2a"), width=6 * S)
            leaf(d, (cx + side * 50 * S, y - 25 * S), base_ang + random.uniform(-.1, .1),
                 size * S, cfg["leaf"])
    leaf(d, (cx, stem_top + 10 * S), math.radians(-90), 175 * S, cfg["leaf"])

    # соцветия
    if cfg["bud"]:
        for _ in range(9):
            by = random.randint(int(stem_top + 20 * S), int(top - 80 * S))
            bx = cx + random.choice([-1, 1]) * random.randint(12 * S, 55 * S)
            r = random.randint(9 * S, 16 * S)
            d.ellipse([bx - r, by - r, bx + r, by + r], fill=hex2rgb(cfg["bud"]) + (230,))
            d.ellipse([bx - r // 2, by - r // 2, bx, by], fill=(255, 255, 255, 120))

    # плашка с названием
    bar_h = 110 * S
    d.rectangle([0, H - bar_h, W, H], fill=(0, 0, 0, 150))
    f1 = ImageFont.truetype(FONT_B, 46 * S)
    f2 = ImageFont.truetype(FONT_R, 26 * S)
    d.text((40 * S, H - bar_h + 12 * S), cfg["name"], font=f1, fill=(255, 255, 255))
    d.text((40 * S, H - bar_h + 68 * S), cfg["sub"], font=f2, fill=(220, 220, 220))

    img = img.resize((W // S, H // S), Image.LANCZOS)
    img.save(OUT / f"{key}.jpg", quality=90)
    print("saved", OUT / f"{key}.jpg")


if __name__ == "__main__":
    for k, c in STRAINS.items():
        make(k, c)

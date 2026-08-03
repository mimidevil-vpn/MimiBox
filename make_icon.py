# -*- coding: utf-8 -*-
"""Генерация иконки MimiBox (логотип «M» с градиентом на тёмной плитке).
Создаёт ui/app.ico (для exe и проводника) и ui/app_icon.png (превью).
Запуск:  py -3.12 make_icon.py   (нужен Pillow)
"""
import os

from PIL import Image, ImageDraw, ImageFilter

S = 1024
R = int(S * 0.24)                 # радиус скругления плитки
PINK = (255, 122, 205, 255)       # #FF7ACD
VIOLET = (200, 132, 255, 255)     # #C884FF
TILE = (15, 15, 16, 255)          # #0F0F10

CX = CY = S / 2
MW = S * 0.16                     # половина ширины буквы M
MY0, MY1 = S * 0.26, S * 0.72     # верх/низ M
W = max(2, int(S * 0.082))        # толщина штрихов


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_m(d, color, width):
    x0, x1 = CX - MW, CX + MW
    jt = (CX, MY1)  # нижняя точка V (базовая линия)
    for a, b in (
        ((x0, MY0), (x0, MY1)),   # левая стойка
        ((x1, MY0), (x1, MY1)),   # правая стойка
        ((x0, MY0), jt),          # левая диагональ
        ((x1, MY0), jt),          # правая диагональ
    ):
        d.line([a, b], fill=color, width=width)
        for (ex, ey) in (a, b):
            r = width / 2
            d.ellipse([ex - r, ey - r, ex + r, ey + r], fill=color)


def hgrad(ca, cb):
    px = Image.new("RGB", (S, S))
    d = ImageDraw.Draw(px)
    for x in range(S):
        t = x / max(1, S - 1)
        c = tuple(int(ca[i] + (cb[i] - ca[i]) * t) for i in range(3))
        d.line([(x, 0), (x, S)], fill=c)
    return px


# фон-плитка
base = Image.new("RGBA", (S, S), (0, 0, 0, 0))
tile = Image.new("RGBA", (S, S), TILE)
grad = Image.new("L", (1, S))
for y in range(S):
    grad.putpixel((0, y), int(46 * (1 - y / S)))
sheen = Image.new("RGBA", (S, S), (255, 255, 255, 0))
sheen.putalpha(grad.resize((S, S)))
tile = Image.alpha_composite(tile, sheen)

mask = rounded_mask(S, R)
base.paste(tile, (0, 0), mask)

# мягкое свечение за буквой
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
dg = ImageDraw.Draw(glow)
draw_m(dg, (255, 122, 205, 110), W + 8)
glow = glow.filter(ImageFilter.GaussianBlur(24))
base = Image.alpha_composite(base, glow)

# сама буква: градиент розовый -> фиолетовый
letter = Image.new("L", (S, S), 0)
dl = ImageDraw.Draw(letter)
draw_m(dl, 255, W)
base.paste(hgrad(PINK, VIOLET), (0, 0), letter)

# тонкая светлая рамка
d = ImageDraw.Draw(base)
d.rounded_rectangle([3, 3, S - 4, S - 4], radius=R, outline=(255, 255, 255, 46), width=6)

# обрезаем по маске (на случай выхода линий)
out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
out.paste(base, (0, 0), mask)

here = os.path.dirname(os.path.abspath(__file__))
png = os.path.join(here, "ui", "app_icon.png")
ico = os.path.join(here, "ui", "app.ico")
out.save(png)
out.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("saved:", png)
print("saved:", ico)

#!/usr/bin/env python3
"""產生第一階段「種子」的零件圖（可重複產生，結果固定）。

輸出：assets/parts/seed/body.png（球體光影）、seams.png（分割線）、antenna_l.png、antenna_r.png
用法：python3 tools/gen_seed_parts.py
只需要 Pillow，不需要 numpy。
"""

import math
import os

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "parts", "seed")

SS = 2            # 超取樣倍率
R = 150           # 球半徑（1x）
PAD = 6
BASE = (132, 184, 226)    # 淡藍金屬色
DARK = (40, 74, 118)
SEAM = (44, 78, 120)


def lerp(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def make_body():
    """回傳 (shell, seams)：shell 是有光影的球（不跟著滾動轉），seams 是分割線（滾動時會轉）。"""
    size = (R + PAD) * 2 * SS
    c = size / 2
    rad = R * SS
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    # 光源（左上前方）
    lx, ly, lz = -0.45, -0.6, 0.66
    n = math.sqrt(lx * lx + ly * ly + lz * lz)
    lx, ly, lz = lx / n, ly / n, lz / n
    for y in range(size):
        ny = (y + 0.5 - c) / rad
        for x in range(size):
            nx = (x + 0.5 - c) / rad
            d2 = nx * nx + ny * ny
            if d2 > 1.0:
                continue
            nz = math.sqrt(1.0 - d2)
            diff = max(0.0, nx * lx + ny * ly + nz * lz)
            # 反射向量的 z 分量 → 鏡面高光
            rz = 2 * (nx * lx + ny * ly + nz * lz) * nz - lz
            spec = max(0.0, rz) ** 28
            # 金屬感：環境反射帶（水平方向的明暗條紋）
            env = 0.5 + 0.5 * math.cos((ny * 1.3 + 0.15) * math.pi)
            shade = 0.38 + 0.5 * diff + 0.18 * env
            col = lerp(DARK, BASE, min(1.0, shade))
            if shade > 1.0:
                col = lerp(BASE, (225, 240, 252), min(1.0, shade - 1.0))
            # 邊緣反光（rim light）
            rim = (1.0 - nz) ** 3
            col = lerp(col, (170, 210, 240), rim * 0.55)
            col = lerp(col, (255, 255, 255), min(1.0, spec * 0.95))
            px[x, y] = col + (255,)

    # 分割線：赤道（略往下彎）與左右兩條經線
    seams = Image.new("L", (size, size), 0)
    sd = ImageDraw.Draw(seams)
    w = max(2, int(2.2 * SS))
    k = 0.18  # 赤道傾斜（從稍高的角度看）
    eq_y = c + rad * 0.08
    sd.arc((c - rad, eq_y - rad * k, c + rad, eq_y + rad * k), 0, 180, fill=255, width=w)
    for sgn in (-1, 1):
        sw = rad * 0.62
        box = (c - sw, c - rad, c + sw, c + rad)
        if sgn < 0:
            sd.arc(box, 100, 260, fill=255, width=w)
        else:
            sd.arc(box, 280, 440, fill=255, width=w)
    # 只保留球內的分割線
    disc = Image.new("L", (size, size), 0)
    ImageDraw.Draw(disc).ellipse((c - rad + 3, c - rad + 3, c + rad - 3, c + rad - 3), fill=255)
    seams = ImageChops.multiply(seams, disc)
    seam_layer = Image.new("RGBA", (size, size), SEAM + (0,))
    seam_layer.putalpha(seams.point(lambda v: int(v * 0.85)))
    # 刻痕下緣的細亮線，看起來像凹槽
    hl = ImageChops.offset(seams, 0, int(1.5 * SS))
    hl = ImageChops.subtract(hl, seams)
    hl_layer = Image.new("RGBA", (size, size), (200, 228, 250, 0))
    hl_layer.putalpha(hl.point(lambda v: int(v * 0.45)))
    seam_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    seam_img.alpha_composite(hl_layer)
    seam_img.alpha_composite(seam_layer)

    # 柔和的深色外框
    alpha = img.getchannel("A")
    edge = ImageChops.subtract(alpha, alpha.filter(ImageFilter.MinFilter(5)))
    edge_layer = Image.new("RGBA", (size, size), (28, 52, 86, 0))
    edge_layer.putalpha(edge.point(lambda v: int(v * 0.7)))
    img.alpha_composite(edge_layer)
    small = (size // SS, size // SS)
    return img.resize(small, Image.LANCZOS), seam_img.resize(small, Image.LANCZOS)


def make_antenna() -> Image.Image:
    """細長的葉片形呆毛天線（左邊那根），底部中心是錨點。"""
    W, H = 44, 118
    s = SS * 2
    img = Image.new("RGBA", (W * s, H * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    cx = W * s / 2
    base_y = (H - 6) * s
    tip = (cx - 8 * s, 4 * s)          # 尖端略往外彎
    half = 9 * s
    # 左右兩個面（亮面、暗面）做出稜線
    mid = [(cx + (tip[0] - cx) * t + math.sin(t * math.pi) * -3 * s, base_y + (tip[1] - base_y) * t)
           for t in [i / 20 for i in range(21)]]
    left = [(x - half * (1 - i / 20) ** 0.9, y) for i, (x, y) in enumerate(mid)]
    right = [(x + half * (1 - i / 20) ** 0.9, y) for i, (x, y) in enumerate(mid)]
    d.polygon(left + mid[::-1], fill=(172, 214, 244, 255))
    d.polygon(mid + right[::-1], fill=(92, 140, 192, 255))
    d.line(mid, fill=(220, 238, 252, 255), width=max(1, s // 2))
    # 外框
    outline = left + right[::-1]
    d.line(outline + [outline[0]], fill=(40, 70, 110, 255), width=s, joint="curve")
    return img.resize((W, H), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    body, seams = make_body()
    body.save(os.path.join(OUT, "body.png"))
    seams.save(os.path.join(OUT, "seams.png"))
    ant = make_antenna()
    ant.save(os.path.join(OUT, "antenna_l.png"))
    ant.transpose(Image.FLIP_LEFT_RIGHT).save(os.path.join(OUT, "antenna_r.png"))
    print("body", body.size, "anchor", (body.size[0] // 2, body.size[1] // 2))
    print("antenna", ant.size, "anchor", (ant.size[0] // 2, ant.size[1] - 6))


if __name__ == "__main__":
    main()

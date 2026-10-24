"""Generate Nemo's icon. Supersampled tile + mark, saved as multi-size .ico."""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONT_DIR = Path("C:/Windows/Fonts")

TILE = 256
SS = 4                      # supersample
AMBER = (242, 178, 62)
TILE_TOP = (30, 36, 44)
TILE_BOT = (14, 18, 24)
EDGE = (255, 255, 255, 22)


def font(name: str, px: int):
    p = FONT_DIR / name
    if not p.exists():
        return None
    try:
        return ImageFont.truetype(str(p), px)
    except Exception:
        return None


def tile(radius_frac: float = 0.22) -> Image.Image:
    """Rounded tile with a vertical gradient and a hairline edge."""
    n, r = TILE * SS, int(TILE * SS * radius_frac)
    grad = Image.new("RGB", (n, n))
    gd = ImageDraw.Draw(grad)
    for y in range(n):
        t = y / (n - 1)
        gd.line([(0, y), (n, y)], fill=tuple(
            int(TILE_TOP[i] + (TILE_BOT[i] - TILE_TOP[i]) * t) for i in range(3)))
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, n - 1, n - 1], radius=r, fill=255)
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    img.paste(grad, (0, 0), mask)
    ImageDraw.Draw(img).rounded_rectangle([0, 0, n - 1, n - 1], radius=r,
                                          outline=EDGE, width=SS)
    return img


def mark_n(style: str, line: bool = True) -> Image.Image:
    img = tile()
    d = ImageDraw.Draw(img)
    n = TILE * SS
    if style == "bahnschrift_bold":
        f = font("bahnschrift.ttf", int(n * 0.66))
        if f is not None:
            try:
                f.set_variation_by_name("Bold")
            except Exception:
                pass
    elif style == "segoe_bold":
        f = font("segoeuib.ttf", int(n * 0.62))
    elif style == "segoe_semibold":
        f = font("seguisb.ttf", int(n * 0.62))
    else:
        f = font("bahnschrift.ttf", int(n * 0.66))
    if f is None:
        f = ImageFont.load_default()
    d.text((n / 2, n * 0.50), "n", font=f, fill=AMBER, anchor="mm")
    if line:
        _level_line(d, n)
    return img.resize((TILE, TILE), Image.LANCZOS)


def mark_wave() -> Image.Image:
    img = tile()
    d = ImageDraw.Draw(img)
    n = TILE * SS
    heights = [0.34, 0.58, 0.82, 0.58, 0.34]
    bw = n * 0.078
    gap = n * 0.076
    total = len(heights) * bw + (len(heights) - 1) * gap
    x = (n - total) / 2
    for hf in heights:
        hgt = n * hf
        d.rounded_rectangle([x, (n - hgt) / 2, x + bw, (n + hgt) / 2],
                            radius=bw / 2, fill=AMBER)
        x += bw + gap
    return img.resize((TILE, TILE), Image.LANCZOS)


def _level_line(d: ImageDraw.ImageDraw, n: int) -> None:
    """A short amber bar under the monogram: the pill's level meter, reduced."""
    w, h = n * 0.30, n * 0.045
    x = (n - w) / 2
    y = n * 0.735
    d.rounded_rectangle([x, y, x + w, y + h], radius=h / 2, fill=(242, 178, 62, 150))


def sheet(images: dict[str, Image.Image]) -> Image.Image:
    sizes = [128, 48, 32, 16]
    col_w = 150
    width = col_w * len(images) + 20
    height = 150 + sum(s + 24 for s in sizes) + 20
    out = Image.new("RGB", (width, height), (245, 245, 247))
    d = ImageDraw.Draw(out)
    for i, (name, img) in enumerate(images.items()):
        x = 10 + i * col_w
        big = img.resize((128, 128), Image.LANCZOS)
        out.paste(big, (x, 10), big)
        d.text((x, 142), name, fill=(20, 20, 20))
        y = 160
        for s in sizes:
            small = img.resize((s, s), Image.LANCZOS)
            out.paste(small, (x, y), small)
            d.text((x + s + 8, y), f"{s}px", fill=(90, 90, 90))
            y += s + 24
    return out


if __name__ == "__main__":
    variants = {
        "n_plain": mark_n("segoe_bold", line=False),
        "n_semibold": mark_n("seg_semibold", line=False),
        "n_bahn": mark_n("bahnschrift", line=False),
        "wave": mark_wave(),
    }
    for name, img in variants.items():
        img.save(HERE / f"preview_{name}.png")
    sheet(variants).save(HERE / "preview_sheet.png")
    print("wrote previews:", ", ".join(variants))

    # the shipped icon: the wave, because it stays legible at 16px and is the
    # same motif as the pill's level meter
    chosen = variants["wave"]
    chosen.resize((512, 512), Image.LANCZOS).save(HERE / "nemo.png")
    chosen.save(HERE / "nemo.ico", format="ICO",
                sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40),
                       (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote nemo.ico, nemo.png")

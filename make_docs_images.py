"""Generate the README screenshots and the GitHub social preview.

Every pill here comes out of overlay.render(), the same function the live overlay
draws with, so these images cannot drift from the app the way a mockup would.

Note on sizing: the overlay's layout constants are expressed in final pixels (they
multiply by SS and are divided by SS again on downscale), so rendering on a larger
canvas gives the same 15 px text with more empty space around it. Native size then
upscale is the only way to get a bigger pill that is still the pill.

    python make_docs_images.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overlay

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
BG_TOP, BG_BOT = overlay.BG_TOP, overlay.BG_BOT
FG, ACCENT, MUTED = overlay.FG, overlay.ACCENT, overlay.MUTED
LEVELS = [0.2, 0.62, 0.9, 0.45, 0.78, 0.3, 0.66, 0.88, 0.52]


def pill(label: str, hist, phase: float = 0.55, zoom: int = 2) -> Image.Image:
    img = overlay.render(label, phase, list(hist))
    return img.resize((img.width * zoom, img.height * zoom),
                      Image.Resampling.LANCZOS)


def gradient(size, top=BG_TOP, bottom=BG_BOT) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(
            int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return img


def pill_states() -> Path:
    rows = [pill("listening", LEVELS), pill("transcribing", LEVELS),
            pill("done", LEVELS), pill("paused", LEVELS), pill("error", LEVELS)]
    pad, gap = 52, 26
    w = rows[0].width + pad * 2
    h = len(rows) * rows[0].height + (len(rows) - 1) * gap + pad * 2
    out = gradient((w, h))
    ImageDraw.Draw(out).rounded_rectangle(
        [2, 2, w - 3, h - 3], radius=18, outline=(44, 52, 62), width=2)
    y = pad
    for row in rows:
        out.paste(row, (pad, y), row)
        y += row.height + gap
    path = DOCS / "pill-states.png"
    out.save(path)
    print(f"  wrote {path.relative_to(ROOT)}  {out.size}")
    return path


def listening_closeup() -> Path:
    p = pill("listening", LEVELS, zoom=3)
    out = Image.new("RGBA", (p.width + 72, p.height + 72), (0, 0, 0, 0))
    out.paste(p, (36, 36), p)
    path = DOCS / "pill-listening.png"
    out.save(path)
    print(f"  wrote {path.relative_to(ROOT)}  {out.size}")
    return path


def social_preview() -> Path:
    """1280x640: GitHub's social preview, and a usable Open Graph image."""
    w, h = 1280, 640
    out = gradient((w, h))
    d = ImageDraw.Draw(out)

    big = overlay._font(124)
    mid = overlay._font(44)
    small = overlay._font(30)

    title = "Nemo"
    tw = d.textlength(title, font=big)
    dot_r = 15
    x = (w - (tw + dot_r * 3.2)) / 2
    cy = 118
    d.ellipse([x, cy - dot_r, x + dot_r * 2, cy + dot_r], fill=ACCENT)
    d.text((x + dot_r * 3.2, cy), title, font=big, fill=FG, anchor="lm")

    d.text((w / 2, 232), "Hold a key. Speak. Let go.", font=mid, fill=MUTED,
           anchor="mm")

    p = pill("listening", LEVELS, zoom=3)
    out.paste(p, (int(w / 2 - p.width / 2), 300), p)

    d.text((w / 2, 566), "Offline  \u00b7  local model  \u00b7  ~150 ms to text",
           font=small, fill=ACCENT, anchor="mm")

    path = DOCS / "social-preview.png"
    out.save(path)
    print(f"  wrote {path.relative_to(ROOT)}  {out.size}")
    return path


if __name__ == "__main__":
    DOCS.mkdir(exist_ok=True)
    print("generating docs images from the real renderer:")
    pill_states()
    listening_closeup()
    social_preview()

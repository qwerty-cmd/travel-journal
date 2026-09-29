"""Generate the PWA icons in frontend/public/ (a white bicycle on solid green).

Build-time only, not a runtime dependency. Run from the repo root:
    uv run --no-project --with pillow python frontend/scripts/generate-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

BG = (31, 92, 64)
FG = (255, 255, 255)
SUPERSAMPLE = 4
OUT = Path(__file__).resolve().parent.parent / "public"

# Bicycle in unit coordinates centred on (0, 0); extents x ±0.45, y ±0.27.
WHEELS = [(-0.25, 0.07), (0.25, 0.07)]
WHEEL_R = 0.2
REAR, BB, SEAT, HEAD, FRONT = (-0.25, 0.07), (-0.05, 0.07), (-0.12, -0.2), (0.18, -0.2), (0.25, 0.07)
FRAME = [(REAR, BB), (BB, SEAT), (SEAT, REAR), (SEAT, HEAD), (HEAD, BB), (HEAD, FRONT),
         ((-0.19, -0.22), (-0.05, -0.22)), (HEAD, (0.14, -0.27)), ((0.14, -0.27), (0.23, -0.27))]


def render(size: int, scale: float) -> Image.Image:
    """`scale` is the artwork's size as a fraction of the icon edge."""
    big = size * SUPERSAMPLE
    img = Image.new("RGB", (big, big), BG)
    draw = ImageDraw.Draw(img)
    unit = big * scale
    stroke = max(1, round(unit * 0.045))

    def pt(p: tuple[float, float]) -> tuple[float, float]:
        return big / 2 + p[0] * unit, big / 2 + p[1] * unit

    for cx, cy in WHEELS:
        x, y = pt((cx, cy))
        r = WHEEL_R * unit
        draw.ellipse((x - r, y - r, x + r, y + r), outline=FG, width=stroke)
    for a, b in FRAME:
        draw.line((pt(a), pt(b)), fill=FG, width=stroke)
    for joint in (REAR, BB, SEAT, HEAD, FRONT):
        x, y = pt(joint)
        draw.ellipse((x - stroke / 2, y - stroke / 2, x + stroke / 2, y + stroke / 2), fill=FG)
    return img.resize((size, size), Image.LANCZOS)


# Maskable: the artwork's farthest corner (~0.525 * scale) must stay inside the
# 40%-radius safe-zone circle, so scale <= 0.76; 0.7 leaves margin.
ICONS = {
    "pwa-192x192.png": (192, 0.9),
    "pwa-512x512.png": (512, 0.9),
    "pwa-maskable-512x512.png": (512, 0.7),
    "apple-touch-icon.png": (180, 0.8),
}

if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for name, (size, scale) in ICONS.items():
        render(size, scale).save(OUT / name, optimize=True)
        print(f"wrote {OUT / name}")

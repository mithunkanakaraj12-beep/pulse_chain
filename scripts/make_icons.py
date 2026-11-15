"""Generate the PWA icons (PNG) from code, so no binary design assets need to be hand-made.
Run: python scripts/make_icons.py   (needs Pillow)"""
from pathlib import Path
from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "frontend" / "icons"
NAVY, NAVY2, TEAL = (10, 35, 66), (18, 51, 92), (56, 208, 207)


def draw(size: int, maskable: bool = False) -> Image.Image:
    s = size * 4  # supersample for smooth edges
    img = Image.new("RGBA", (s, s), NAVY if maskable else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if not maskable:
        d.rounded_rectangle((0, 0, s, s), radius=int(s * 0.23), fill=NAVY2)
    pad = 0.30 if maskable else 0.16  # maskable icons need a larger safe zone
    pts = [(0, .52), (.20, .52), (.30, .30), (.46, .78), (.60, .42), (.68, .56), (1, .56)]
    pts = [(s * (pad + (1 - 2 * pad) * x), s * (0.18 + 0.64 * y)) for x, y in pts]
    d.line(pts, fill=TEAL, width=int(s * 0.07), joint="curve")
    r = int(s * 0.035)
    for p in (pts[0], pts[-1]):
        d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=TEAL)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(192).save(OUT / "icon-192.png")
    draw(512).save(OUT / "icon-512.png")
    draw(512, maskable=True).save(OUT / "icon-maskable-512.png")
    (OUT / "icon.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"><rect width="48" height="48" rx="11" fill="#12335c"/>'
        '<path d="M5 26h9l4-10 6 20 5-14 3 4h11" fill="none" stroke="#38d0cf" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>\n')
    print("icons written to", OUT)

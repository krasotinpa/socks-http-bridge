"""Regenerate the bundled tray icon PNGs (maintainer tool, issue #45).

Run from the repo root: uv run python scripts/make_icons.py
The app itself never draws icons; it loads these static assets.
"""

from pathlib import Path

from PIL import Image, ImageDraw

from socks_http_bridge.health import BridgeState
from socks_http_bridge.tray import STATE_COLORS

ASSETS = (Path(__file__).resolve().parent.parent
          / "src" / "socks_http_bridge" / "assets")


def draw_glyph(colour: tuple[int, int, int, int]) -> Image.Image:
    """Suspension-bridge silhouette, mirror-symmetric on a 64x64 canvas.

    Thick strokes stay legible at 16 px tray size; towers rise above the
    cable anchors so the glyph reads as a bridge, not an "M".
    """
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rectangle((2, 42, 61, 50), fill=colour)           # deck
    draw.rectangle((15, 6, 21, 50), fill=colour)           # left tower
    draw.rectangle((42, 6, 48, 50), fill=colour)           # right tower
    draw.line([(18, 16), (24, 26), (32, 33), (39, 26), (45, 16)],
              fill=colour, width=5, joint="curve")         # main cable
    draw.line([(18, 16), (4, 44)], fill=colour, width=5)   # side cables
    draw.line([(45, 16), (59, 44)], fill=colour, width=5)
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    for state in BridgeState:
        path = ASSETS / f"icon-{state.name.lower()}.png"
        draw_glyph((*STATE_COLORS[state], 255)).save(path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()

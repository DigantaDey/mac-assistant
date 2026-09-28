#!/usr/bin/env python3
"""Build Aura's app icon: one 1024×1024 master PNG + a packed .icns.

The master is drawn procedurally (Pillow) so the icon is deterministic and
matches the in-app orb: a dark rounded square with a glowing indigo-blue
orb, three conic rings and a fine waveform tick ring.

Usage (on a dev machine):
    python scripts/make_icon.py            # writes assets/icon.png + assets/icon.icns
Requires Pillow:  pip install pillow
The repo ships the generated assets/icon.png + assets/icon.icns, so a Mac install
never needs this script — it's for regenerating the art.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFilter
except Exception:
    sys.exit("This script needs Pillow:  pip install pillow")

ROOT = Path(__file__).resolve().parent.parent
SIZE = 1024


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1],
                                           radius=radius, fill=255)
    return mask


def _radial(size: int, inner, outer) -> Image.Image:
    """Soft radial gradient: `inner` at the center fading to transparent.
    Computed at 256px and upscaled — indistinguishable, ~100× faster."""
    small = 256
    img = Image.new("RGBA", (small, small), (0, 0, 0, 0))
    cx = cy = small / 2
    r = small / 2
    px = img.load()
    for y in range(small):
        for x in range(small):
            d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / r
            if d >= 1:
                continue
            t = 1.0 - d
            t = t * t * (3 - 2 * t)  # smoothstep falloff
            color = tuple(int(inner[i] * t + outer[i] * (1 - t) * 0.25) for i in range(3))
            px[x, y] = color + (int(255 * t),)
    return img.resize((size, size), Image.BILINEAR)


def build_master(path: Path) -> None:
    S = SIZE
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    mask = _rounded_mask(S, int(S * 0.2237))   # Apple's continuous-corner ratio

    # Background: vertical gradient slate.
    bg = Image.new("RGBA", (S, S))
    top, bottom = (23, 25, 34), (10, 11, 15)
    for y in range(S):
        t = y / S
        color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,)
        ImageDraw.Draw(bg).line([(0, y), (S, y)], fill=color)
    # Faint top glow.
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([S * 0.18, -S * 0.28, S * 0.82, S * 0.42], fill=(94, 92, 230, 26))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.09))
    bg = Image.alpha_composite(bg, glow)
    bg.putalpha(mask)
    img = Image.alpha_composite(img, bg)

    cx = cy = S / 2
    orb_r = S * 0.235

    # Orb body — radial indigo→blue.
    orb = _radial(int(orb_r * 2), (126, 124, 255), (10, 132, 255))
    img.paste(orb, (int(cx - orb_r), int(cy - orb_r)), orb)

    # Warm core so the orb reads as luminous, not flat.
    core_size = int(orb_r * 1.3)
    core = _radial(core_size, (240, 240, 255), (126, 124, 255))
    img.paste(core, (int(cx - core_size / 2), int(cy - core_size / 2)), core)

    # Three conic rings — the signature look, approximated with arcs.
    ring_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring_layer)
    rings = [
        (S * 0.335, (122, 120, 255, 210), 10, 30, 210),
        (S * 0.385, (10, 132, 255, 170), 8, 120, 330),
        (S * 0.44, (160, 158, 255, 120), 6, 250, 30),
    ]
    for radius, color, width, a0, a1 in rings:
        rd.arc([cx - radius, cy - radius, cx + radius, cy + radius],
               a0, a1, fill=color, width=width)
    ring_layer = ring_layer.filter(ImageFilter.GaussianBlur(3))
    img = Image.alpha_composite(img, ring_layer)

    # Fine waveform tick ring — the listening detail.
    tick_layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    td = ImageDraw.Draw(tick_layer)
    import math
    n = 72
    r1, r2 = S * 0.30, S * 0.318
    for i in range(n):
        ang = (i / n) * 2 * math.pi
        wobble = 0.35 + 0.65 * abs(math.sin(i * 0.55)) * abs(math.sin(i * 0.21))
        r_end = r1 + (r2 - r1) * wobble * 1.6
        x0 = cx + math.cos(ang) * r1
        y0 = cy + math.sin(ang) * r1
        x1 = cx + math.cos(ang) * r_end
        y1 = cy + math.sin(ang) * r_end
        alpha = int(60 + 150 * wobble)
        td.line([(x0, y0), (x1, y1)], fill=(140, 160, 255, alpha), width=4)
    tick_layer = tick_layer.filter(ImageFilter.GaussianBlur(1.2))
    img = Image.alpha_composite(img, tick_layer)

    # Hairline edge so the squircle sits crisply on light docks.
    edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle([1, 1, S - 2, S - 2],
                                           radius=int(S * 0.2237) - 1,
                                           outline=(255, 255, 255, 22), width=3)
    img = Image.alpha_composite(img, edge)
    img.putalpha(mask)

    img.save(path)


def pack_icns(png: Path, icns: Path) -> None:
    """Pack PNG entries into a classic .icns container (no iconutil needed)."""
    master = Image.open(png).convert("RGBA")

    def at(px: int) -> Image.Image:
        return master.resize((px, px), Image.LANCZOS)

    entries = [
        ("ic07", 128), ("ic08", 256), ("ic09", 512), ("ic10", 1024),
        ("ic11", 32), ("ic12", 64), ("ic13", 256), ("ic14", 512),
    ]
    import io
    blobs = []
    for tag, px in entries:
        buf = io.BytesIO()
        at(px).save(buf, "PNG")
        data = buf.getvalue()
        blobs.append(tag.encode() + struct.pack(">I", len(data) + 8) + data)
    body = b"".join(blobs)
    icns.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def main() -> None:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    build_master(assets / "icon.png")
    pack_icns(assets / "icon.png", assets / "icon.icns")
    print(f"wrote {assets / 'icon.png'} and {assets / 'icon.icns'}")


if __name__ == "__main__":
    main()

"""Rasterise app/web/static/favicon.svg to favicon.ico + apple-touch-icon.png.

Pure stdlib: the shapes are simple enough to sample directly, which is
cheaper than adding Pillow/cairosvg to a project that renders no images.
Geometry here mirrors the SVG; change one, change the other.
"""
import binascii
import math
import struct
import sys
from pathlib import Path
import zlib

VB = 32.0                      # viewBox units
TILE_R = 7.0
GRAD_TOP = (0x6f, 0xb6, 0xef)
GRAD_BOT = (0x3d, 0x8c, 0xcb)
INK = (0x14, 0x16, 0x1a)
PLATES = [(8.5, 4.8, 15.0, 2.8, 1.4), (8.5, 24.4, 15.0, 2.8, 1.4)]
TRI = [(12.6, 11.5), (22.2, 16.0), (12.6, 20.5)]
TRI_R = 0.75                   # half of stroke-width 1.5, round join
SS = 4                         # supersample factor per axis

# At 16px one viewBox unit is half a pixel, so the shared geometry lands the
# plates on half-pixel boundaries and they antialias to grey mush. This size
# gets its own pass, snapped to the 16px grid: thinner plates, a taller
# triangle, and 1.25px of clear air between them.
OVERRIDES = {
    16: {
        "PLATES": [(6.0, 5.0, 20.0, 3.0, 1.0), (6.0, 24.0, 20.0, 3.0, 1.0)],
        "TRI": [(11.0, 11.0), (23.0, 16.0), (11.0, 21.0)],
        "TRI_R": 0.5,
    },
}


def rrect_inside(px, py, x, y, w, h, r):
    qx = abs(px - (x + w / 2)) - (w / 2 - r)
    qy = abs(py - (y + h / 2)) - (h / 2 - r)
    outer = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outer + min(max(qx, qy), 0.0) - r <= 0.0


def seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    denom = vx * vx + vy * vy
    t = 0.0 if denom == 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / denom))
    return math.hypot(px - (ax + t * vx), py - (ay + t * vy))


def tri_inside(px, py):
    sgn = None
    for i in range(3):
        ax, ay = TRI[i]
        bx, by = TRI[(i + 1) % 3]
        cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        if cross == 0:
            continue
        s = cross > 0
        if sgn is None:
            sgn = s
        elif sgn != s:
            return False
    return True


def ink_inside(px, py):
    for (x, y, w, h, r) in PLATES:
        if rrect_inside(px, py, x, y, w, h, r):
            return True
    if tri_inside(px, py):
        return True
    return min(seg_dist(px, py, *TRI[i], *TRI[(i + 1) % 3]) for i in range(3)) <= TRI_R


def render(size, full_bleed=False):
    """Return RGBA bytes for a size x size icon."""
    global PLATES, TRI, TRI_R
    saved = (PLATES, TRI, TRI_R)
    ov = OVERRIDES.get(size)
    if ov:
        PLATES, TRI, TRI_R = ov["PLATES"], ov["TRI"], ov["TRI_R"]
    try:
        return _render(size, full_bleed)
    finally:
        PLATES, TRI, TRI_R = saved


def _render(size, full_bleed=False):
    scale = VB / size
    step = 1.0 / (SS * 2)
    offs = [(k * 2 + 1) * step for k in range(SS)]
    rows = []
    for j in range(size):
        row = bytearray()
        for i in range(size):
            tile_hits = 0
            ink_hits = 0
            gy_acc = 0.0
            for dy in offs:
                py = (j + dy) * scale
                for dx in offs:
                    px = (i + dx) * scale
                    on_tile = full_bleed or rrect_inside(px, py, 0, 0, VB, VB, TILE_R)
                    if not on_tile:
                        continue
                    tile_hits += 1
                    gy_acc += py
                    if ink_inside(px, py):
                        ink_hits += 1
            n = SS * SS
            if tile_hits == 0:
                row += b"\x00\x00\x00\x00"
                continue
            t = (gy_acc / tile_hits) / VB
            base = [GRAD_TOP[c] + (GRAD_BOT[c] - GRAD_TOP[c]) * t for c in range(3)]
            k = ink_hits / tile_hits
            rgb = [base[c] * (1 - k) + INK[c] * k for c in range(3)]
            a = int(round(255 * tile_hits / n))
            row += bytes(int(round(v)) for v in rgb) + bytes([a])
        rows.append(bytes(row))
    return rows


def png(rows, size):
    raw = b"".join(b"\x00" + r for r in rows)
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(pngs):
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs = b"", b""
    offset = 6 + 16 * len(pngs)
    for size, blob in pngs:
        d = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", d, d, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    return head + entries + blobs


DEFAULT_OUT = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    pngs = []
    for size in (16, 32, 48):
        pngs.append((size, png(render(size), size)))
        print("rendered", size)
    with open(out / "favicon.ico", "wb") as fh:
        fh.write(ico(pngs))
    with open(out / "apple-touch-icon.png", "wb") as fh:
        fh.write(png(render(180, full_bleed=True), 180))
    print(f"wrote favicon.ico + apple-touch-icon.png to {out}")

"""Draw the application icon and write icon.ico and icon.png.

    python assets/make_icon.py

Pillow rather than an SVG renderer, because every one of those on Windows
wants a Cairo build and this is twenty lines of geometry. `icon.svg` beside
this file is the reference drawing; the two are kept in step by hand.

Everything is drawn at 1024 and downsampled per size with LANCZOS, which is
what gives clean edges at 16 px. Drawing straight into a 16x16 canvas gives
stair-stepped diagonals, and the bolt is all diagonals.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent

SUPER = 1024          # working resolution
VIEW = 512.0          # the coordinate space the shapes are written in
K = SUPER / VIEW

PLATE = (0x12, 0x14, 0x1A, 255)
BODY = (0x6E, 0xA8, 0xFF, 255)
SPARK = (0xFF, 0xC7, 0x66, 255)

# The bolt, in the 512 space. Also the shape that gets cut out of the camera.
_BOLT_RAW = [(300, -10), (118, 288), (252, 288), (214, 528), (406, 212), (272, 212)]

# Drawn at full size the bolt runs the whole canvas while the camera only
# occupies the middle third, so at 48 px it reads as a lightning icon with
# some blue behind it. Pulled in about its own centre until the camera body
# and the lens block both survive the downsample.
BOLT_SCALE = 0.78


def _shrink(points, factor):
    cx = sum(x for x, _ in points) / len(points)
    cy = sum(y for _, y in points) / len(points)
    return [(cx + (x - cx) * factor, cy + (y - cy) * factor) for x, y in points]


BOLT = _shrink(_BOLT_RAW, BOLT_SCALE)

# Half the width of the gap left around the bolt, in the 512 space.
GAP = 13.0

ICO_SIZES = [256, 128, 64, 48, 32, 24, 16]


def s(points):
    return [(x * K, y * K) for x, y in points]


def quad(p0, p1, p2, steps=24):
    """Quadratic Bezier as a point list. The prism corners are curves."""
    out = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        out.append((
            u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
        ))
    return out


def prism():
    """The lens block: a wedge with both right-hand corners rounded."""
    pts = [(375, 192), (468, 140)]
    pts += quad((468, 140), (505, 122), (505, 165))
    pts.append((505, 347))
    pts += quad((505, 347), (505, 390), (468, 372))
    pts.append((375, 320))
    return pts


def rounded(draw, box, radius, fill):
    draw.rounded_rectangle([(box[0] * K, box[1] * K), (box[2] * K, box[3] * K)],
                           radius=radius * K, fill=fill)


def build() -> Image.Image:
    img = Image.new("RGBA", (SUPER, SUPER), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    rounded(d, (0, 0, 512, 512), 102, PLATE)

    # The camera, on its own layer so the bolt can be cut out of it without
    # taking the plate with it.
    cam = Image.new("RGBA", (SUPER, SUPER), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cam)

    # translate(45,45) scale(0.824) in the svg, applied here by hand.
    def t(points):
        return [(45 + x * 0.824, 45 + y * 0.824) for x, y in points]

    body = [(8, 105), (345, 105), (345, 405), (8, 405)]
    bl, bt = t([body[0]])[0]
    br, bb = t([body[2]])[0]
    cd.rounded_rectangle([(bl * K, bt * K), (br * K, bb * K)],
                         radius=62 * 0.824 * K, fill=BODY)
    cd.polygon(s(t(prism())), fill=BODY)

    # The gap. A filled bolt blurred and thresholded is a true outset, which
    # scaling the polygon about its centre is not: that pulls the long thin
    # arms sideways and leaves the tips uncovered.
    bolt_pts = s(t(BOLT))
    grow = Image.new("L", (SUPER, SUPER), 0)
    ImageDraw.Draw(grow).polygon(bolt_pts, fill=255)
    grow = grow.filter(ImageFilter.GaussianBlur(GAP * 0.824 * K * 0.62))
    grow = grow.point(lambda v: 255 if v > 26 else 0)

    cam.putalpha(Image.composite(
        Image.new("L", (SUPER, SUPER), 0), cam.getchannel("A"), grow))
    img.alpha_composite(cam)

    d.polygon(bolt_pts, fill=SPARK)
    return img


def main() -> None:
    art = build()
    art.resize((512, 512), Image.LANCZOS).save(HERE / "icon.png")

    frames = [art.resize((n, n), Image.LANCZOS) for n in ICO_SIZES]
    # Pillow rescales internally if handed one image; handing it the list means
    # every size in the file was downsampled from 1024 rather than from 256.
    frames[0].save(HERE / "icon.ico", format="ICO",
                   sizes=[(n, n) for n in ICO_SIZES], append_images=frames[1:])

    for n in (256, 48, 16):
        art.resize((n, n), Image.LANCZOS).save(HERE / f"preview-{n}.png")

    print(f"wrote {HERE / 'icon.ico'} with {', '.join(str(n) for n in ICO_SIZES)}")


if __name__ == "__main__":
    main()

"""Generate the AID Helpdesk logo as a PNG.

We ship a PNG (not SVG) because the SVG wordmark rendered inconsistently across
browsers — the curved "D" stroke drifted vertically out of line with the "AI"
letters. Rendering with PIL lets us measure the text box and place the D-arc so
its vertical centre exactly matches the cap height of the letters.

Run:  python tools/make_logo.py
Output: cloud/static/AIDLogo.png   (picked up automatically by the /logo.png route)
"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# Render at high resolution then downsample for crisp anti-aliasing.
SCALE = 4
S = 256 * SCALE          # canvas size
C = S // 2               # centre

# Palette (matches the dashboard --accent / --accent2 / --bg)
DISC      = (7, 6, 26, 255)        # #07061a
RING      = (99, 102, 241, 255)    # #6366f1
RING_SOFT = (129, 140, 248, 90)    # #818cf8 @ ~0.35
LETTERS   = (221, 214, 254, 255)   # #ddd6fe
GLOW      = (99, 102, 241)         # #6366f1


def _font(px):
    return ImageFont.truetype("C:/Windows/Fonts/arialbd.ttf", px)


def main():
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── Soft outer glow ──
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gdraw = ImageDraw.Draw(glow)
    gr = int(S * 0.47)
    gdraw.ellipse([C - gr, C - gr, C + gr, C + gr], fill=GLOW + (110,))
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.06))
    img.alpha_composite(glow)

    # ── Discs / rings ──
    r_outer = int(S * 0.45)
    ring_w  = int(S * 0.030)
    draw.ellipse([C - r_outer, C - r_outer, C + r_outer, C + r_outer], fill=DISC)
    draw.ellipse([C - r_outer, C - r_outer, C + r_outer, C + r_outer],
                 outline=RING, width=ring_w)
    r_soft = int(S * 0.415)
    draw.ellipse([C - r_soft, C - r_soft, C + r_soft, C + r_soft],
                 outline=RING_SOFT, width=max(1, int(S * 0.008)))

    # ── Wordmark "AI" + curved D ──
    font = _font(int(S * 0.30))
    text = "AI"
    tb = draw.textbbox((0, 0), text, font=font)        # (l, t, r, b) ink box
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    top = tb[1]

    # The D-arc is the right half of a circle whose diameter == cap height.
    arc_d   = th                       # arc diameter matches letter height
    gap     = int(S * 0.012)           # space between "I" and the D
    arc_w   = int(th * 0.17)           # stroke weight, ~matches Arial-bold stems

    # Centre the whole "AI)" lockup horizontally.
    total_w = tw + gap + arc_d / 2 + arc_w / 2
    start_x = C - total_w / 2

    # Draw letters: shift so ink box lands at start_x and is vertically centred.
    tx = start_x - tb[0]
    ty = C - th / 2 - top
    draw.text((tx, ty), text, font=font, fill=LETTERS)

    # Draw the D-arc, vertically aligned to the letter cap box.
    arc_left = start_x + tw + gap
    box_top  = C - arc_d / 2
    # A right-bulging semicircle (the curved side of a "D").
    draw.arc([arc_left - arc_d / 2, box_top, arc_left + arc_d / 2, box_top + arc_d],
             start=-90, end=90, fill=LETTERS, width=arc_w)

    # ── Downsample ──
    out = img.resize((256, 256), Image.LANCZOS)
    dest = os.path.join(os.path.dirname(__file__), "..", "cloud", "static", "AIDLogo.png")
    dest = os.path.abspath(dest)
    out.save(dest)
    print("wrote", dest)


if __name__ == "__main__":
    main()

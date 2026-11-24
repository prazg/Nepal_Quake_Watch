#!/usr/bin/env python3
"""
check_basemaps.py — catches basemap providers that degrade silently.

Why this exists
---------------
CARTO's basemap CDN began requiring an API key. It did not start returning
errors. It kept answering HTTP 200 with a valid PNG of the correct size and
the correct content type — and burned "API KEY REQUIRED" into the pixels.
Every status-code check passed while the live site was covered in watermarks.
The failure was only visible by looking at the image.

So this script looks at the image.

How the watermark is detected, without OCR
------------------------------------------
A watermark is drawn identically on every tile. Real map content is not: two
tiles from different parts of the world share almost no bright pixels.

The test fetches several widely separated tiles from the same provider,
reduces each to a binary mask of its brightest pixels, and measures how much
those masks overlap. High overlap between unrelated locations means something
is being stamped on top of all of them.

This needs no OCR, no reference images and no knowledge of what the watermark
says, so it catches a provider that changes its watermark text, and it would
catch a "TRIAL EXPIRED" or "UPGRADE YOUR PLAN" stamp just as well.

It also flags the cruder failure modes: non-image responses, tiles that are a
single flat colour, and tiles far outside a plausible byte range.

Basemap URLs are read out of app.js (or assets/app.js) rather than duplicated
here, so this cannot drift from what the site actually loads.

Exit code 1 fails the workflow.
"""

import io
import os
import re
import sys
import urllib.request

try:
    from PIL import Image
except ImportError:
    Image = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..")) if os.path.basename(HERE) == "scripts" else HERE
DATA = os.path.join(ROOT, "data") if os.path.isdir(os.path.join(ROOT, "data")) else ROOT

UA = {
    "User-Agent": "Mozilla/5.0 (compatible; nepal-quake-watch basemap check)",
    "Referer": "https://prazg.github.io/",
}

# Widely separated tiles at zoom 7. Different continents, different terrain,
# different label density — nothing legitimate should be common to all five.
# Tiles over populated land on five continents. Land matters: an ocean tile
# is a flat colour, which yields an empty mask and no signal either way.
PROBES = [
    (7, 94, 53),      # Kathmandu
    (7, 64, 44),      # Paris
    (7, 113, 50),     # Tokyo
    (7, 37, 48),      # New York
    (7, 77, 64),      # Nairobi
]

# Above this Jaccard overlap between the bright-pixel masks of unrelated
# tiles, something is being drawn on all of them. Measured values: a clean
# Esri dark basemap sits near zero; a CARTO watermarked tile sits far above.
OVERLAP_FAIL = 0.35
BRIGHT_PERCENTILE = 0.985      # top ~1.5% of pixels by luminance

failures, warnings, notes = [], [], []


def redact_url(u):
    """Mask query-string API keys (e.g. ?key=...) so secrets never leak to logs."""
    return re.sub(
        r"([?&](?:api_?key|key|access_token|token)=)[^&\s'\"]+",
        r"\1***",
        str(u),
        flags=re.IGNORECASE,
    )


def _strip_js_comments(src):
    """Strip // and /* */ comments while preserving string and template literals."""
    return re.sub(
        r"""'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`|//[^\n]*|/\*.*?\*/""",
        lambda m: m.group(0) if m.group(0)[0] in "'\"`" else " ",
        src,
        flags=re.DOTALL,
    )


def _resolve_js_expr(expr, consts, part_re):
    """Evaluate a simple string / template / const concatenation expression."""
    pieces = []
    for pm in part_re.finditer(expr):
        tok = pm.group(0)
        if tok[0] in ("'", '"'):
            pieces.append(tok[1:-1])
        elif tok[0] == "`":
            body = tok[1:-1]
            body = re.sub(
                r"\$\{\s*([A-Za-z_$][\w$]*)\s*\}",
                lambda m: consts.get(m.group(1), m.group(0)),
                body,
            )
            pieces.append(body)
        else:
            val = consts.get(tok)
            if val is None:
                return None
            pieces.append(val)
    return "".join(pieces)


def find_basemap_urls(src=None):
    """Read tile template URLs out of app.js, tracking single- and multi-line calls."""
    if src is None:
        for candidate in ("app.js", "assets/app.js"):
            path = os.path.join(ROOT, candidate)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    src = f.read()
                break
        else:
            sys.exit("could not find app.js")

    clean = _strip_js_comments(src)

    consts = {}
    for cm in re.finditer(
        r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(['"])([^'"\n]+)\2""",
        clean,
    ):
        consts[cm.group(1)] = cm.group(3)

    token_pat = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*)"""
    expr_pat = rf"{token_pat}(?:\s*\+\s*{token_pat})*"
    part_re = re.compile(token_pat, re.DOTALL)

    patterns = (
        re.compile(rf"L\.tileLayer\(\s*({expr_pat})", re.DOTALL),
        re.compile(rf"\burl\s*:\s*({expr_pat})", re.DOTALL),
    )

    urls = set()
    for pat in patterns:
        for m in pat.finditer(clean):
            frag = _resolve_js_expr(m.group(1), consts, part_re)
            if not frag or "{z}" not in frag:
                continue
            if "{x}" not in frag or "{y}" not in frag:
                continue
            if frag.startswith("/") and "ESRI" in consts:
                frag = consts["ESRI"] + frag
            if frag.startswith(("http://", "https://")):
                urls.add(frag)
    return sorted(urls)


def fetch(url, z, x, y):
    real = (url.replace("{z}", str(z)).replace("{x}", str(x))
               .replace("{y}", str(y)).replace("{r}", "").replace("{s}", "a"))
    req = urllib.request.Request(real, headers=UA)
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.headers.get("Content-Type", ""), r.read(), real


def bright_mask(img):
    """Binary mask of the brightest pixels, as a set of pixel indices."""
    g = img.convert("L").resize((128, 128))
    px = list(g.tobytes())
    ordered = sorted(px)
    cut = ordered[int(len(ordered) * BRIGHT_PERCENTILE)]
    if cut >= 254:
        cut = 253
    return {i for i, v in enumerate(px) if v > cut}


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check(url):
    if Image is None:
        sys.exit("Pillow is required for this check:  pip install Pillow")
    safe = redact_url(url)
    print(f"\n  {safe}")
    masks, sizes = [], []
    for z, x, y in PROBES:
        try:
            ctype, blob, _real = fetch(url, z, x, y)
        except Exception as e:                        # noqa: BLE001
            failures.append(f"{safe} — tile {z}/{x}/{y} failed to fetch: "
                            f"{redact_url(e)}")
            return
        if "image" not in ctype:
            failures.append(f"{safe} — returned {ctype!r}, not an image. The "
                            f"provider is probably serving an error page.")
            return
        try:
            img = Image.open(io.BytesIO(blob))
            img.load()
        except Exception as e:                        # noqa: BLE001
            failures.append(f"{safe} — response is not a decodable image: "
                            f"{redact_url(e)}")
            return

        sizes.append(len(blob))
        ex = img.convert("L").getextrema()
        if ex[0] == ex[1]:
            warnings.append(f"{safe} — tile {z}/{x}/{y} is a single flat "
                            f"colour; may be out of coverage or a placeholder")
        masks.append(bright_mask(img))

    pairs = [(i, j) for i in range(len(masks)) for j in range(i + 1, len(masks))]
    scores = [jaccard(masks[i], masks[j]) for i, j in pairs]
    worst = max(scores) if scores else 0.0
    mean = sum(scores) / len(scores) if scores else 0.0

    print(f"    bytes {min(sizes)}-{max(sizes)}  "
          f"bright-pixel overlap: mean {mean:.3f}, max {worst:.3f}")

    if worst >= OVERLAP_FAIL:
        failures.append(
            f"{safe} — unrelated tiles share {worst:.0%} of their brightest "
            f"pixels. Something is being stamped on every tile: an API-key "
            f"or trial watermark, or an error graphic. LOOK AT A TILE.")
    elif worst >= OVERLAP_FAIL * 0.6:
        warnings.append(
            f"{safe} — bright-pixel overlap {worst:.0%} is elevated. Not "
            f"conclusive, but worth opening a tile and checking.")
    else:
        notes.append(f"{safe} — clean")


def main():
    if Image is None:
        sys.exit("Pillow is required for this check:  pip install Pillow")
    urls = find_basemap_urls()
    if not urls:
        sys.exit("no tile URLs found in app.js — has the map setup changed?")
    print(f"checking {len(urls)} basemap layer(s) referenced by the site")
    for u in urls:
        check(u)

    print()
    for n in notes:
        print(f"  ok    {n}")
    for w in warnings:
        print(f"  WARN  {w}")
    for f in failures:
        print(f"  FAIL  {f}")

    if failures:
        print(f"\n{len(failures)} basemap check(s) failed.")
        print("Replace the affected provider in app.js. Esri's free services "
              "(Canvas/World_Dark_Gray_Base, World_Imagery) and "
              "tile.openstreetmap.org need no key.")
        return 1
    print(f"\nall basemaps clean ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

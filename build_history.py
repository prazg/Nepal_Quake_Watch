#!/usr/bin/env python3
"""
build_history.py — observed historical seismicity for the Nepal region.

IMPORTANT FRAMING
-----------------
What this produces is a map of *where earthquakes have been recorded*, from
the USGS catalogue. That is not the same thing as a seismic hazard zonation.

A hazard zonation is a modelled product: it accounts for fault geometry and
slip rates, recurrence, site conditions, and catalogue incompleteness, and it
expresses ground motion at a stated probability of exceedance. Nepal's
authoritative zonation is in the national building code, NBC 105:2020, which
defines the seismic zoning factor used for design. No verified machine-readable
copy of that zonation was available when this was written, so it is not
included, and nothing here should be presented as a substitute for it.

Catalogue completeness is also strongly time-varying: the network densified
after 2015, so recent decades record many more small events than earlier ones.
Counting raw events would therefore map instrumentation, not seismicity. This
script defaults to M>=4.0 to reduce that bias, and states the limitation in
the output so the interface can display it.
"""

import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}
FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"

BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)
START = "1900-01-01"
MIN_MAG = 4.0
CELL = 0.10


def get(url, timeout=300, retries=3):
    last = None
    for a in range(retries):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(3 * (a + 1))
    raise last


def main():
    url = (f"{FDSN}?format=geojson&orderby=time&starttime={START}"
           f"&minmagnitude={MIN_MAG}"
           f"&minlatitude={BBOX['minlat']}&maxlatitude={BBOX['maxlat']}"
           f"&minlongitude={BBOX['minlon']}&maxlongitude={BBOX['maxlon']}")
    print(f"querying USGS catalogue since {START}, M>={MIN_MAG} ...", flush=True)
    fc = get(url)
    feats = fc["features"]
    print(f"  {len(feats)} events")

    grid = defaultdict(lambda: {"n": 0, "maxmag": 0.0, "energy": 0.0})
    by_decade = defaultdict(int)
    significant = []

    for ft in feats:
        p, g = ft["properties"], ft["geometry"]
        mag = p.get("mag")
        if mag is None:
            continue
        lon, lat = g["coordinates"][0], g["coordinates"][1]
        i = math.floor((lon - BBOX["minlon"]) / CELL)
        j = math.floor((lat - BBOX["minlat"]) / CELL)
        c = grid[(i, j)]
        c["n"] += 1
        c["maxmag"] = max(c["maxmag"], mag)
        # Gutenberg-Richter energy proxy, log10(E) ~ 1.5M. Used only to weight
        # the display so a single large event is not lost among small ones.
        c["energy"] += 10 ** (1.5 * mag)
        yr = time.gmtime(p["time"] / 1000).tm_year
        by_decade[(yr // 10) * 10] += 1
        if mag >= 6.0:
            significant.append({
                "id": ft["id"], "t": p["time"], "mag": round(mag, 1),
                "depth": round(g["coordinates"][2], 1),
                "lon": round(lon, 4), "lat": round(lat, 4),
                "place": p.get("place"), "url": p.get("url"),
            })

    significant.sort(key=lambda r: -r["t"])

    cells = []
    for (i, j), c in sorted(grid.items()):
        cells.append([
            round(BBOX["minlon"] + (i + 0.5) * CELL, 3),
            round(BBOX["minlat"] + (j + 0.5) * CELL, 3),
            c["n"],
            round(c["maxmag"], 1),
            round(math.log10(c["energy"]), 2),
        ])

    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "query": {"start": START, "min_magnitude": MIN_MAG, "bbox": BBOX,
                  "cell_deg": CELL},
        "counts": {"events": len(feats), "cells": len(cells),
                   "significant_m6plus": len(significant)},
        "events_by_decade": dict(sorted(by_decade.items())),
        "caveats": [
            "This is observed instrumental seismicity, not a seismic hazard "
            "zonation.",
            "Catalogue completeness varies strongly with time. Coverage before "
            "the 1960s is sparse and improved again after 2015, so apparent "
            "changes over time partly reflect instrumentation rather than "
            "seismicity.",
            "Nepal's authoritative seismic zoning factor is defined in NBC "
            "105:2020 and is not reproduced here.",
        ],
        "cells": cells,
        "significant": significant,
    }
    path = os.path.join(DATA, "history.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print(f"wrote history.json ({os.path.getsize(path)/1024:.0f} KB): "
          f"{len(cells)} cells, {len(significant)} events M>=6")
    print("  events by decade:", dict(sorted(by_decade.items())))


if __name__ == "__main__":
    sys.exit(main())

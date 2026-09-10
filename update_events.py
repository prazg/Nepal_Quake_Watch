#!/usr/bin/env python3
"""
update_events.py — refreshes the live earthquake feed.

Runs on a schedule in CI. Standard library only, so the workflow needs no
dependency install step.

What it does
------------
1. Queries the USGS FDSN event service for the Nepal region.
2. For each event that has a USGS ShakeMap, downloads the gridded MMI
   coverage and samples it against the pre-built population grid to estimate
   how many people felt each level of shaking.
3. Writes data/live.json.

What it deliberately does not do
--------------------------------
It does not estimate shaking for events without a ShakeMap. Applying a
ground-motion or intensity-prediction equation calibrated elsewhere to the
Himalaya, and presenting the output as if it were measured, would be
inventing data. Events without a ShakeMap are published with magnitude,
depth and location only, and the interface says so.
"""

import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}
FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"

BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)
LOOKBACK_DAYS = 365
MIN_MAG = 2.5
MMI_BINS = [4, 5, 6, 7, 8, 9]


def get(url, timeout=120, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


# ------------------------------------------------------- point in polygon

def rings_of(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def in_polygon(lon, lat, polys):
    """Ray casting. Outer ring counts, holes subtract."""
    for coords in polys:
        inside = False
        for r, ring in enumerate(coords):
            hit = False
            n = len(ring)
            j = n - 1
            for i in range(n):
                xi, yi = ring[i][0], ring[i][1]
                xj, yj = ring[j][0], ring[j][1]
                if (yi > lat) != (yj > lat):
                    x = (xj - xi) * (lat - yi) / (yj - yi) + xi
                    if lon < x:
                        hit = not hit
                j = i
            if r == 0:
                inside = hit
            elif hit:
                inside = False
        if inside:
            return True
    return False


# ------------------------------------------------------------- shakemap

def sample_mmi(cov, lon, lat):
    """Nearest-cell lookup on a CoverageJSON regular grid."""
    ax = cov["domain"]["axes"]
    x0, x1, nx = ax["x"]["start"], ax["x"]["stop"], ax["x"]["num"]
    y0, y1, ny = ax["y"]["start"], ax["y"]["stop"], ax["y"]["num"]
    if not (min(x0, x1) <= lon <= max(x0, x1)):
        return None
    if not (min(y0, y1) <= lat <= max(y0, y1)):
        return None
    key = "mmi" if "mmi" in cov["ranges"] else list(cov["ranges"])[0]
    rng = cov["ranges"][key]
    i = round((lon - x0) / ((x1 - x0) / (nx - 1)))
    j = round((lat - y0) / ((y1 - y0) / (ny - 1)))
    if not (0 <= i < nx and 0 <= j < ny):
        return None
    v = rng["values"][j * nx + i]
    return v


def exposure_from_shakemap(cov, cells, district_names):
    """
    Sum estimated population by integer MMI band.

    Population comes from our dasymetric grid (district census totals spread
    by settlement density), so these are estimates. The shaking values are
    USGS's own modelled ShakeMap output, not ours.
    """
    bands = {b: 0 for b in MMI_BINS}
    districts = {}
    maxmmi = 0.0
    for lon, lat, pop, pcode in cells:
        v = sample_mmi(cov, lon, lat)
        if v is None:
            continue
        maxmmi = max(maxmmi, v)
        band = int(math.floor(v))
        if band < MMI_BINS[0]:
            continue
        band = min(band, MMI_BINS[-1])
        for b in MMI_BINS:
            if band >= b:
                bands[b] += pop
        d = districts.setdefault(pcode, {"pop": 0, "mmi": 0.0})
        if v >= MMI_BINS[0]:
            d["pop"] += pop
        d["mmi"] = max(d["mmi"], v)
    top = sorted(
        ({"pcode": k, "name": district_names.get(k, k),
          "pop": v["pop"], "mmi": round(v["mmi"], 1)}
         for k, v in districts.items() if v["pop"] > 0),
        key=lambda r: -r["pop"])[:12]
    return {
        "bands": {str(k): v for k, v in bands.items() if v > 0},
        "districts": top,
        "max_mmi_over_population": round(maxmmi, 1),
    }


def main():
    with open(os.path.join(DATA, "popgrid.json"), encoding="utf-8") as f:
        grid = json.load(f)
    cells = grid["cells"]

    with open(os.path.join(DATA, "districts.json"), encoding="utf-8") as f:
        dj = json.load(f)
    district_names = {ft["properties"]["pcode"]: ft["properties"]["name"]
                      for ft in dj["features"]}

    with open(os.path.join(DATA, "nepal_outline.json"), encoding="utf-8") as f:
        outline = rings_of(json.load(f)["features"][0]["geometry"])

    now = datetime.now(timezone.utc)
    start = now.timestamp() - LOOKBACK_DAYS * 86400
    url = (f"{FDSN}?format=geojson&orderby=time"
           f"&starttime={datetime.fromtimestamp(start, timezone.utc):%Y-%m-%d}"
           f"&minmagnitude={MIN_MAG}"
           f"&minlatitude={BBOX['minlat']}&maxlatitude={BBOX['maxlat']}"
           f"&minlongitude={BBOX['minlon']}&maxlongitude={BBOX['maxlon']}")
    print("querying USGS ...", flush=True)
    fc = get(url)
    print(f"  {len(fc['features'])} events in the last {LOOKBACK_DAYS} days")

    events, enriched = [], 0
    for ft in fc["features"]:
        p, g = ft["properties"], ft["geometry"]
        lon, lat, depth = g["coordinates"][0], g["coordinates"][1], g["coordinates"][2]
        ev = {
            "id": ft["id"],
            "t": p["time"],
            "mag": p["mag"],
            "magType": p.get("magType"),
            "depth": depth,
            "lon": lon,
            "lat": lat,
            "place": p.get("place"),
            "url": p.get("url"),
            "felt": p.get("felt"),
            "cdi": p.get("cdi"),
            "mmi": p.get("mmi"),
            "alert": p.get("alert"),
            "sig": p.get("sig"),
            "status": p.get("status"),
            "tsunami": p.get("tsunami"),
            "in_nepal": in_polygon(lon, lat, outline),
            "shakemap": None,
            "exposure": None,
        }

        # Only larger events are worth the extra requests, and only those
        # tend to have a ShakeMap at all.
        if (p.get("mag") or 0) >= 4.5:
            try:
                detail = get(p["detail"])
                prods = detail["properties"].get("products", {})
                sm = (prods.get("shakemap") or [None])[0]
                if sm:
                    c = sm.get("contents", {})
                    cont = c.get("download/cont_mmi.json") or c.get("download/cont_mi.json")
                    covk = ("download/coverage_mmi_medium_res.covjson"
                            if "download/coverage_mmi_medium_res.covjson" in c
                            else "download/coverage_mmi_low_res.covjson")
                    ev["shakemap"] = {
                        "contour_url": cont["url"] if cont else None,
                        "max_mmi": float(sm["properties"].get("maxmmi", 0) or 0),
                    }
                    if covk in c:
                        cov = get(c[covk]["url"], timeout=180)
                        ev["exposure"] = exposure_from_shakemap(
                            cov, cells, district_names)
                        enriched += 1
                lp = (prods.get("losspager") or [None])[0]
                if lp:
                    ev["pager"] = {
                        "alert": lp["properties"].get("alertlevel"),
                        "max_mmi": lp["properties"].get("maxmmi"),
                    }
            except Exception as e:                    # noqa: BLE001
                print(f"  note: no shakemap detail for {ft['id']}: {e}")

        events.append(ev)

    out = {
        "generated": now.isoformat(timespec="seconds"),
        "source": "USGS FDSN event service",
        "query": {"bbox": BBOX, "days": LOOKBACK_DAYS, "min_magnitude": MIN_MAG},
        "counts": {"events": len(events), "with_exposure_estimate": enriched},
        "events": events,
    }
    path = os.path.join(DATA, "live.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print(f"wrote live.json: {len(events)} events, "
          f"{enriched} with exposure estimates "
          f"({os.path.getsize(path)/1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())

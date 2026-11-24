#!/usr/bin/env python3
"""
update_events.py — refreshes the live earthquake feed.

Runs on a schedule in CI. Standard library only, so the workflow needs no
dependency install step.

What it does
------------
1. Queries the USGS FDSN event service for the Nepal region, with an automatic
   fallback to the USGS Edge CDN 30-day summary GeoJSON feed if FDSN fails.
2. Queries the EMSC SeismicPortal FDSN event service for the same bounding box
   to capture regional (EMSC / NDI / partner) detections quickly.
3. Deduplicates USGS and EMSC events within 90 seconds and 65 km (haversine),
   keeping USGS as the canonical primary record (preserving ShakeMap, PAGER,
   and population exposure estimates) while tagging multi-agency confirmation.
4. For each M >= 4.5 USGS event with a ShakeMap, downloads the gridded MMI
   coverage and samples it against the pre-built ward-based population grid to
   estimate how many people felt each level of shaking.
5. Atomically writes live.json (preserving any existing live.json on total
   network failure).

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
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..")) if os.path.basename(HERE) == "scripts" else HERE
DATA = os.path.join(ROOT, "data") if os.path.isdir(os.path.join(ROOT, "data")) else ROOT

UA = {"User-Agent": "nepal-quake-watch/0.2 (research; contact via repository)"}
FDSN = "https://earthquake.usgs.gov/fdsnws/event/1/query"
USGS_SUMMARY = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_month.geojson"
EMSC_FDSN = "https://www.seismicportal.eu/fdsnws/event/1/query"
EMSC_EVENT_URL = "https://www.emsc-csem.org/Earthquake_information/earthquake.php?id={id}"

BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)
LOOKBACK_DAYS = 365
MIN_MAG = 2.5
MMI_BINS = [4, 5, 6, 7, 8, 9]

DEDUP_TIME_SEC = 90
DEDUP_DIST_KM = 65.0


def get(url, timeout=120, retries=3, allow_empty=False):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if allow_empty and (getattr(r, "status", 200) == 204 or not raw.strip()):
                    return None
                return json.loads(raw.decode("utf-8"))
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise last


def atomic_write_json(path, obj):
    """Write JSON via a same-directory temp file and os.replace."""
    parent = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".live_", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------- geometry & helpers

def in_bbox(lon, lat):
    return (BBOX["minlon"] <= lon <= BBOX["maxlon"]
            and BBOX["minlat"] <= lat <= BBOX["maxlat"])


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two WGS84 points in kilometres."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2.0) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2)
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def parse_iso_ms(val):
    """Convert an ISO 8601 UTC timestamp string (or numeric epoch ms) to epoch ms."""
    if isinstance(val, (int, float)):
        return int(round(val))
    s = str(val).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(round(dt.timestamp() * 1000))


def title_case_region(region):
    """Title-case ALL-CAPS Flynn-Engdahl region labels while preserving mixed case."""
    if not region:
        return None
    text = str(region).strip()
    if not text:
        return None
    if text == text.upper():
        small = {"of", "and", "in", "on", "the", "near", "to", "off"}
        parts = text.title().split(" ")
        out = [parts[0]] + [
            w.lower() if w.lower() in small else w
            for w in parts[1:]
        ]
        return " ".join(out)
    return text


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

    Population comes from our census ward population grid, so these are
    estimates. The shaking values are USGS's own modelled ShakeMap output,
    not ours.
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


# ------------------------------------------------------- parsers & fusion

def parse_usgs_feature(ft, outline):
    p = ft.get("properties") or {}
    g = ft.get("geometry") or {}
    c = g.get("coordinates") or []
    if len(c) < 2 or c[0] is None or c[1] is None:
        return None
    lon, lat = float(c[0]), float(c[1])
    if not in_bbox(lon, lat):
        return None
    mag = p.get("mag")
    if mag is not None and float(mag) < MIN_MAG:
        return None
    depth = c[2] if len(c) > 2 else None
    t_val = p.get("time")
    if t_val is None:
        return None
    return {
        "id": str(ft["id"]),
        "t": int(t_val),
        "mag": float(mag) if mag is not None else None,
        "magType": p.get("magType"),
        "depth": float(depth) if depth is not None else None,
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
        "source": "USGS",
        "sources": ["USGS"],
        "shakemap": None,
        "exposure": None,
    }


def enrich_usgs_event(ev, detail_url, cells, district_names):
    """Attach USGS ShakeMap, PAGER, and ward population grid exposure for M >= 4.5."""
    if (ev.get("mag") or 0) < 4.5 or not detail_url:
        return False
    enriched = False
    try:
        detail = get(detail_url)
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
                enriched = True
        lp = (prods.get("losspager") or [None])[0]
        if lp:
            ev["pager"] = {
                "alert": lp["properties"].get("alertlevel"),
                "max_mmi": lp["properties"].get("maxmmi"),
            }
    except Exception as e:                            # noqa: BLE001
        print(f"  note: no shakemap detail for {ev['id']}: {e}")
    return enriched


def parse_emsc_feature(ft, outline):
    """Parse one EMSC SeismicPortal GeoJSON feature into the live.json schema."""
    p = ft.get("properties") or {}
    g = ft.get("geometry") or {}
    c = g.get("coordinates") or []

    lon = p.get("lon") if p.get("lon") is not None else (c[0] if len(c) > 0 else None)
    lat = p.get("lat") if p.get("lat") is not None else (c[1] if len(c) > 1 else None)
    if lon is None or lat is None:
        return None
    lon, lat = float(lon), float(lat)
    if not in_bbox(lon, lat):
        return None

    mag = p.get("mag")
    if mag is not None and float(mag) < MIN_MAG:
        return None

    raw_time = p.get("time")
    if raw_time is None:
        return None
    try:
        t_ms = parse_iso_ms(raw_time)
    except ValueError:
        return None

    # EMSC GeoJSON encodes coordinates[2] as negative elevation; depth must
    # strictly be non-negative kilometres below the surface.
    raw_depth = p.get("depth") if p.get("depth") is not None else (c[2] if len(c) > 2 else None)
    depth = abs(float(raw_depth)) if raw_depth is not None else None

    unid = str(p.get("unid") or ft.get("id") or p.get("source_id") or "").strip()
    if not unid:
        return None
    page_id = str(p.get("source_id") or unid).strip()
    ev_id = unid if unid.startswith("emsc-") else f"emsc-{unid}"

    return {
        "id": ev_id,
        "t": t_ms,
        "mag": float(mag) if mag is not None else None,
        "magType": p.get("magtype") or p.get("magType"),
        "depth": depth,
        "lon": lon,
        "lat": lat,
        "place": title_case_region(p.get("flynn_region") or p.get("place")),
        "url": EMSC_EVENT_URL.format(id=page_id) if page_id else None,
        "felt": None,
        "cdi": None,
        "mmi": None,
        "alert": None,
        "sig": None,
        "status": p.get("status"),
        "tsunami": 0,
        "in_nepal": in_polygon(lon, lat, outline),
        "source": "EMSC",
        "sources": ["EMSC"],
        "agency": str(p.get("auth") or "EMSC").upper(),
        "shakemap": None,
        "exposure": None,
    }


def merge_events(usgs_events, emsc_events,
                 max_dt_sec=DEDUP_TIME_SEC, max_dist_km=DEDUP_DIST_KM):
    """
    Fuse USGS and EMSC event lists.

    Matches within <= max_dt_sec seconds and <= max_dist_km km (haversine) keep
    the USGS event as canonical primary (preserving ShakeMap, PAGER, and
    exposure) and record sources=["USGS", "EMSC"]. Unmatched regional EMSC/NDI
    events are appended as-is. Returns reverse-chronologically sorted events.
    """
    merged = []
    seen_ids = set()
    usgs_canonical = []
    for u in usgs_events:
        if u["id"] in seen_ids:
            continue
        ev = dict(u)
        ev["source"] = ev.get("source") or "USGS"
        ev["sources"] = list(ev.get("sources") or ["USGS"])
        seen_ids.add(ev["id"])
        merged.append(ev)
        usgs_canonical.append(ev)

    max_dt_ms = max_dt_sec * 1000
    for em in emsc_events:
        best_match = None
        best_key = None
        for u in usgs_canonical:
            dt_ms = abs(em["t"] - u["t"])
            if dt_ms > max_dt_ms:
                continue
            dist = haversine_km(em["lat"], em["lon"], u["lat"], u["lon"])
            if dist <= max_dist_km:
                key = (dist, dt_ms)
                if best_key is None or key < best_key:
                    best_key = key
                    best_match = u
        if best_match is not None:
            if "EMSC" not in best_match["sources"]:
                best_match["sources"].append("EMSC")
            em_agency = em.get("agency")
            if (em_agency and em_agency not in ("EMSC", "CSEM", "USGS")
                    and not best_match.get("agency")):
                best_match["agency"] = em_agency
            continue
        if em["id"] in seen_ids:
            continue
        ev = dict(em)
        ev["sources"] = list(em.get("sources") or ["EMSC"])
        seen_ids.add(ev["id"])
        merged.append(ev)

    merged.sort(key=lambda e: (-e["t"], str(e["id"])))
    return merged


# --------------------------------------------------------------- fetchers

def fetch_usgs(start_iso, outline, cells, district_names):
    """Query USGS FDSN with automatic fallback to the USGS Edge CDN summary."""
    fdsn_url = (
        f"{FDSN}?format=geojson&orderby=time"
        f"&starttime={start_iso}"
        f"&minmagnitude={MIN_MAG}"
        f"&minlatitude={BBOX['minlat']}&maxlatitude={BBOX['maxlat']}"
        f"&minlongitude={BBOX['minlon']}&maxlongitude={BBOX['maxlon']}"
    )
    fc = None
    source_label = None
    print("querying USGS FDSN ...", flush=True)
    try:
        fc = get(fdsn_url)
        source_label = "USGS FDSN"
        print(f"  USGS FDSN: {len(fc.get('features', []))} events "
              f"in the last {LOOKBACK_DAYS} days")
    except Exception as e:                            # noqa: BLE001
        print(f"  USGS FDSN failed ({e}); falling back to USGS Edge CDN Summary ...",
              flush=True)
        try:
            fc = get(USGS_SUMMARY, timeout=90)
            source_label = "USGS Edge CDN Summary"
            print(f"  USGS Edge CDN Summary: {len(fc.get('features', []))} global "
                  f"events fetched; filtering to Nepal bbox")
        except Exception as e2:                       # noqa: BLE001
            print(f"  USGS Edge CDN Summary also failed: {e2}", flush=True)
            return [], 0, None

    events, enriched = [], 0
    for ft in fc.get("features", []):
        ev = parse_usgs_feature(ft, outline)
        if ev is None:
            continue
        detail_url = (ft.get("properties") or {}).get("detail")
        if enrich_usgs_event(ev, detail_url, cells, district_names):
            enriched += 1
        events.append(ev)

    if source_label == "USGS Edge CDN Summary":
        print(f"  USGS Edge CDN Summary: {len(events)} events inside Nepal bbox")
    return events, enriched, source_label


def fetch_emsc(start_iso, outline):
    """Query EMSC SeismicPortal FDSN for regional earthquake detections."""
    emsc_url = (
        f"{EMSC_FDSN}?format=json"
        f"&minlat={BBOX['minlat']}&maxlat={BBOX['maxlat']}"
        f"&minlon={BBOX['minlon']}&maxlon={BBOX['maxlon']}"
        f"&minmag={MIN_MAG}&limit=250"
        f"&start={start_iso}"
    )
    print("querying EMSC SeismicPortal FDSN ...", flush=True)
    try:
        fc = get(emsc_url, timeout=90, allow_empty=True)
    except Exception as e:                            # noqa: BLE001
        print(f"  EMSC SeismicPortal FDSN failed: {e}", flush=True)
        return [], None

    features = (fc or {}).get("features", [])
    events = []
    for ft in features:
        ev = parse_emsc_feature(ft, outline)
        if ev is not None:
            events.append(ev)
    print(f"  EMSC SeismicPortal FDSN: {len(events)} events parsed")
    return events, "EMSC SeismicPortal FDSN"


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
    start_iso = f"{datetime.fromtimestamp(start, timezone.utc):%Y-%m-%d}"

    usgs_events, enriched, usgs_source = fetch_usgs(
        start_iso, outline, cells, district_names)
    emsc_events, emsc_source = fetch_emsc(start_iso, outline)

    sources_active = [s for s in (usgs_source, emsc_source) if s]
    path = os.path.join(DATA, "live.json")

    if not sources_active:
        print("all live earthquake feeds failed — keeping previous live.json intact",
              file=sys.stderr)
        return 1

    events = merge_events(usgs_events, emsc_events)
    if not events and os.path.exists(path):
        print("zero events returned across active feeds — keeping previous live.json intact",
              file=sys.stderr)
        return 1

    emsc_unique = sum(1 for e in events if e.get("source") == "EMSC")
    matched = sum(1 for e in events
                  if "USGS" in (e.get("sources") or [])
                  and "EMSC" in (e.get("sources") or []))

    out = {
        "generated": now.isoformat(timespec="seconds"),
        "source": " + ".join(sources_active),
        "query": {"bbox": BBOX, "days": LOOKBACK_DAYS, "min_magnitude": MIN_MAG},
        "counts": {
            "events": len(events),
            "with_exposure_estimate": enriched,
            "usgs": len(usgs_events),
            "emsc": len(emsc_events),
            "emsc_unique": emsc_unique,
            "multi_agency": matched,
            "sources_active": sources_active,
        },
        "events": events,
    }
    atomic_write_json(path, out)
    print(f"wrote live.json: {len(events)} events "
          f"({len(usgs_events)} USGS, {emsc_unique} unique EMSC, "
          f"{matched} matched, {enriched} with exposure estimates, "
          f"{os.path.getsize(path)/1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
validate.py — refuses to publish data that fails a consistency check.

This runs in CI between fetching and committing. The point is that a broken
upstream page or a silent parser failure should stop the pipeline, not quietly
ship an empty or wrong dataset to a page people may consult after an
earthquake.

Exit code 1 fails the workflow.
"""

import json
import os
import sys
from datetime import datetime, timezone

DATA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "data"))

BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)

failures, warnings = [], []


def fail(msg):
    failures.append(msg)


def warn(msg):
    warnings.append(msg)


def load(name, required=True):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        (fail if required else warn)(f"{name} is missing")
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:                            # noqa: BLE001
        fail(f"{name} is not valid JSON: {e}")
        return None


def check_live():
    d = load("live.json")
    if not d:
        return
    evs = d.get("events", [])
    if not evs:
        fail("live.json has no events at all")
        return

    for e in evs:
        if not (BBOX["minlat"] <= e["lat"] <= BBOX["maxlat"]
                and BBOX["minlon"] <= e["lon"] <= BBOX["maxlon"]):
            fail(f"event {e['id']} is outside the query bounding box")
            break
        if e.get("mag") is not None and not (-2 <= e["mag"] <= 10):
            fail(f"event {e['id']} has an implausible magnitude {e['mag']}")
            break
        if e.get("depth") is not None and not (-5 <= e["depth"] <= 800):
            fail(f"event {e['id']} has an implausible depth {e['depth']}")
            break

    ts = [e["t"] for e in evs]
    if ts != sorted(ts, reverse=True):
        warn("live.json events are not in reverse time order")

    future = [e for e in evs
              if e["t"] > (datetime.now(timezone.utc).timestamp() + 3600) * 1000]
    if future:
        fail(f"{len(future)} events are dated in the future")

    # Exposure sanity: no band may exceed Nepal's population, and the bands
    # must decrease as intensity rises (MMI 5+ cannot exceed MMI 4+).
    meta = load("reference_meta.json", required=False) or {}
    census = meta.get("census_total") or 40_000_000
    for e in evs:
        ex = e.get("exposure")
        if not ex:
            continue
        bands = sorted(((int(k), v) for k, v in ex["bands"].items()))
        for k, v in bands:
            if v > census:
                fail(f"event {e['id']} exposure at MMI {k} ({v:,}) exceeds "
                     f"Nepal's population ({census:,})")
        vals = [v for _, v in bands]
        if vals != sorted(vals, reverse=True):
            fail(f"event {e['id']} exposure bands are not monotonic")

    print(f"  live.json: {len(evs)} events, "
          f"{sum(1 for e in evs if e.get('exposure'))} with exposure")


def check_nsc():
    d = load("nsc.json", required=False)
    if not d:
        return
    evs = d.get("events", [])
    if not evs:
        fail("nsc.json exists but has no events — the parser is probably broken")
        return
    bad = [e for e in evs if not (20 <= e["lat"] <= 35 and 75 <= e["lon"] <= 92)]
    if bad:
        fail(f"{len(bad)} NEMRC events have coordinates outside the region")
    if not any(e["mag"] >= 4 for e in evs):
        warn("no NEMRC event at M4 or above — check the magnitude column")
    print(f"  nsc.json: {len(evs)} events, latest {evs[0]['utc']}")


def check_impacts():
    d = load("impacts.json", required=False)
    if not d:
        return
    inc = d.get("incidents", [])
    if not inc:
        fail("impacts.json exists but has no incidents")
        return
    tot = d.get("verified_totals", {})
    deaths = tot.get("deaths", 0)
    # Nepal's deadliest recorded earthquake killed on the order of 10^4.
    # A total far above that means duplicated or misparsed records.
    if deaths > 100_000:
        fail(f"verified death total {deaths:,} is implausibly high")
    print(f"  impacts.json: {len(inc)} incidents, {deaths:,} recorded deaths")


def check_reference():
    d = load("reference_meta.json", required=False)
    if not d:
        return
    if d.get("polygons_unmatched"):
        fail(f"districts unmatched to population: {d['polygons_unmatched']}")
    method = d.get("popgrid_method")
    if method == "census_wards":
        print("  popgrid built from observed census ward counts")
    grid, census = d.get("popgrid_total"), d.get("census_total")
    if method == "census_wards":
        # Grid is census 2021; census_total is the COD-PS 2023 projection.
        # They are different series and are not expected to match.
        grid = census = None
    if grid and census:
        drift = abs(grid - census) / census
        if drift > 0.02:
            fail(f"population grid total is {drift*100:.1f}% off the census "
                 f"total ({grid:,} vs {census:,})")
        else:
            print(f"  popgrid within {drift*100:.2f}% of census total")
    if (d.get("hospitals_tier1") or 0) < 100:
        warn("fewer than 100 hospitals — the facility extract may be truncated")


def check_municipalities():
    d = load("municipalities.json", required=False)
    if not d:
        return
    feats = d.get("features", [])
    local = sum(1 for f in feats if f["properties"].get("is_local_level"))
    other = len(feats) - local
    untyped = sum(1 for f in feats if not f["properties"].get("unit_type"))
    named = sum(1 for f in feats if f["properties"].get("name_codab"))
    if named:
        print(f"  {named} units carry an official Geoportal spelling "
              f"differing from COD-AB")
    if untyped:
        warn(f"{untyped} boundary polygons have no unit type")
    if local != 753:
        fail(f"{local} local levels, expected 753 (460 rural + 276 "
             f"municipalities + 11 sub-metro + 6 metro). Upstream has "
             f"changed; per-unit statistics are not trustworthy.")
    else:
        print(f"  municipalities.json: 753 local levels + {other} "
              f"protected areas")


def check_ward_census():
    d = load("ward_census.json", required=False)
    if not d:
        return
    rows = d.get("wards", [])
    if not rows:
        fail("ward_census.json has no ward rows")
        return
    rec = d.get("reconciliation", {})
    if rec.get("wards") != rec.get("official_wards"):
        fail(f"{rec.get('wards')} census ward rows vs official "
             f"{rec.get('official_wards')}")
    total, published = rec.get("population"), rec.get("published_total")
    if total and published:
        gap = abs(published - total) / published
        if gap > 0.03:
            fail(f"ward population sum is {gap*100:.1f}% off the published "
                 f"national total ({total:,} vs {published:,})")
        else:
            print(f"  ward_census.json: {len(rows)} wards, {total:,} people "
                  f"({gap*100:.2f}% below published total)")
    unlocated = sum(1 for r in rows if r[6] is None or r[7] is None)
    if unlocated:
        warn(f"{unlocated} census wards have no coordinates")
    v = [r[5] for r in rows if r[5] is not None]
    if v:
        bad = [x for x in v if x < 0 or x > 100]
        if bad:
            fail(f"{len(bad)} wards have an out-of-range vulnerability share")


def check_wards():
    d = load("wards.json", required=False)
    if not d:
        return
    rows = d.get("wards", [])
    if not rows:
        fail("wards.json has no ward records")
        return
    bad = [r for r in rows
           if not (BBOX["minlat"] <= r[1] <= BBOX["maxlat"]
                   and BBOX["minlon"] <= r[0] <= BBOX["maxlon"])]
    if bad:
        fail(f"{len(bad)} ward centroids fall outside the Nepal bounding box")
    c = d.get("counts", {})
    got = c.get("distinct_wards_in_local_levels")
    want = c.get("official_ward_count")
    if got and want and got != want:
        warn(f"{got} ward polygons vs official {want} (+{got-want}); "
             f"polygon artefact, census confirms {want} — use census counts "
             f"for totals")
    print(f"  wards.json: {len(rows)} ward centroids, "
          f"{c.get('municipalities')} municipalities")


def check_contacts():
    d = load("contacts.json", required=False)
    if not d:
        return
    if "UNVERIFIED" in (d.get("verification_status") or ""):
        warn("emergency contacts are still flagged UNVERIFIED — a person "
             "must confirm them against official directories before this "
             "page is promoted for public use")
    for n in d.get("national", []):
        if not str(n.get("number", "")).strip():
            fail("a national contact entry has no number")
        if not n.get("source"):
            fail(f"contact {n.get('number')} has no source recorded")


def main():
    print("validating data/ ...")
    check_reference()
    check_live()
    check_nsc()
    check_impacts()
    check_municipalities()
    check_wards()
    check_ward_census()
    check_contacts()

    for w in warnings:
        print(f"  WARN  {w}")
    for f in failures:
        print(f"  FAIL  {f}")

    if failures:
        print(f"\n{len(failures)} check(s) failed — not publishing.")
        return 1
    print(f"\nall checks passed ({len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

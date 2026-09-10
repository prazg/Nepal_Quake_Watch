#!/usr/bin/env python3
"""
build_reference.py — builds the static reference layers for Nepal Quake Watch.

Run this occasionally (not every update cycle). It writes byte-deterministic
JSON into ../data/.

Inputs are downloaded from public sources; see DATA_SOURCES.md for provenance
and licensing. Nothing here is invented: every population number traces to the
Nepal COD-PS (2021 census projections, 2023 release) and every point traces to
OpenStreetMap via HOT's HDX exports.

Outputs
-------
data/districts.json        ADM2 polygons + 2023 population by district
data/popgrid.json          0.05-deg dasymetric population grid (for exposure)
data/hospitals.json        health facilities, tiered
data/places.json           populated places (city/town/village)
data/reference_meta.json   build provenance and counts
"""

import csv
import io
import json
import os
import sys
import zipfile
import urllib.request
from collections import defaultdict

from shapely.geometry import shape, Point
from shapely.prepared import prep
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
CACHE = os.path.abspath(os.path.join(HERE, "..", ".cache"))
os.makedirs(DATA, exist_ok=True)
os.makedirs(CACHE, exist_ok=True)

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}

# Nepal bounding box, buffered slightly so cross-border events that shake
# Nepal are still captured.
BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)
CELL = 0.05  # degrees, ~5.5 km N-S

GEOB = "https://www.geoboundaries.org/api/current/gbOpen/NPL/{lvl}/"
HDX_PKG = "https://data.humdata.org/api/3/action/package_show?id={ds}"


def fetch(url, dest, binary=True):
    """Download with a local cache so reruns are cheap and reproducible."""
    path = os.path.join(CACHE, dest)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print(f"  downloading {dest} ...", flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        f.write(r.read())
    return path


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json(name, obj):
    """Deterministic write: sorted keys, no float drift, LF endings."""
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print(f"  wrote {name}  ({os.path.getsize(path)/1024:.0f} KB)")


def hdx_resource(dataset, resource_name):
    meta = json.load(urllib.request.urlopen(
        urllib.request.Request(HDX_PKG.format(ds=dataset), headers=UA), timeout=120))
    for r in meta["result"]["resources"]:
        if r["name"] == resource_name:
            return r["url"]
    raise SystemExit(f"resource {resource_name} not found in {dataset}")


# ---------------------------------------------------------------- boundaries

def get_boundaries(layer):
    """
    Official OCHA Common Operational Dataset boundaries, which carry pcodes.

    An earlier version of this script used geoBoundaries gbOpen and joined on
    district name. That was abandoned: the gbOpen NPL ADM2 layer ships 75
    polygons for Nepal's 77 districts, duplicates the names "Saptari" and
    "Bara", omits Siraha, Parsa, Rupandehi and Dailekh, and labels the two
    halves of the former Nawalparasi district the opposite way round from
    what the names imply. Joining on pcode removes that whole class of error.
    """
    url = hdx_resource("cod-ab-npl", "npl_admin_boundaries.geojson.zip")
    path = fetch(url, "cod_ab.zip")
    with zipfile.ZipFile(path) as z:
        with z.open(layer) as f:
            return json.load(io.TextIOWrapper(f, encoding="utf-8"))


def simplify_feature(geom, tol):
    """Douglas-Peucker in degrees. Keeps files small enough for a web map."""
    g = shape(geom).simplify(tol, preserve_topology=True)
    return json.loads(json.dumps(g.__geo_interface__), parse_float=lambda s: round(float(s), 4))


# --------------------------------------------------------------- population

def district_population():
    """
    Returns {ADM2_PCODE: {...}} from the Nepal COD-PS.

    Fields kept: total, plus the two age groups that matter most for triage
    (under-5 and 65+), because response planning treats them differently.
    """
    url = hdx_resource("cod-ps-npl", "npl_admpop_adm2_2023.csv")
    path = fetch(url, "npl_admpop_adm2_2023.csv")
    out = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pcode = row["ADM2_PCODE"].strip()
            if not pcode:
                continue

            def n(*keys):
                return sum(int(float(row[k])) for k in keys if row.get(k))

            out[pcode] = {
                "name": row["ADM2_EN"].strip(),
                "province": row["ADM1_EN"].strip(),
                "pop": n("T_TL"),
                "u5": n("T_00_04"),
                "o65": n("T_65_69", "T_70_74", "T_75_79", "T_80Plus"),
            }
    return out


# ------------------------------------------------------------- OSM extracts

def osm_geojson(dataset, resource):
    url = hdx_resource(dataset, resource)
    path = fetch(url, f"{dataset}.zip")
    with zipfile.ZipFile(path) as z:
        name = [n for n in z.namelist() if n.endswith(".geojson")][0]
        with z.open(name) as f:
            return json.load(io.TextIOWrapper(f, encoding="utf-8"))


def centroid(geom):
    g = shape(geom)
    c = g.centroid
    return round(c.x, 5), round(c.y, 5)


# -------------------------------------------------------------------- build

def main():
    print("1/6  admin boundaries (OCHA COD-AB)")
    adm2 = get_boundaries("npl_admin2.geojson")
    adm0 = get_boundaries("npl_admin0.geojson")
    write_json("nepal_outline.json", {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {},
                      "geometry": simplify_feature(
                          adm0["features"][0]["geometry"], 0.008)}]})

    print("2/6  district population (COD-PS 2023)")
    pop = district_population()

    unmatched, matched_pcodes = [], set()
    feats, polys, pcodes = [], [], []
    for f in adm2["features"]:
        pc = (f["properties"].get("adm2_pcode") or "").strip()
        v = pop.get(pc)
        if v is None:
            unmatched.append(f["properties"].get("adm2_name") or pc)
            props = {"name": f["properties"].get("adm2_name"), "pop": None,
                     "u5": None, "o65": None, "province": None, "pcode": pc}
        else:
            matched_pcodes.add(pc)
            props = {"name": v["name"], "pop": v["pop"], "u5": v["u5"],
                     "o65": v["o65"], "province": v["province"], "pcode": pc}
        polys.append(shape(f["geometry"]))
        pcodes.append(pc if v is not None else None)
        feats.append({"type": "Feature", "properties": props,
                      "geometry": simplify_feature(f["geometry"], 0.004)})

    missing = sorted(set(pop) - matched_pcodes)
    no_poly_pop = sum(pop[p]["pop"] for p in missing)
    census_total = sum(v["pop"] for v in pop.values())
    print(f"     {len(feats)} polygons, {len(matched_pcodes)} joined to "
          f"population by pcode")
    if unmatched:
        print(f"     WARNING: polygons with no population: {unmatched}")
    if missing:
        print(f"     WARNING: population with no polygon: "
              f"{[pop[p]['name'] for p in missing]} "
              f"({no_poly_pop:,} people, {100*no_poly_pop/census_total:.2f}%)")
    if not unmatched and not missing:
        print(f"     all {census_total:,} people are represented by a polygon")

    write_json("districts.json", {"type": "FeatureCollection", "features": feats})

    print("3/6  populated places (OSM via HOT/HDX)")
    pp = osm_geojson("hotosm_npl_populated_places",
                     "hotosm_npl_populated_places_osm_geojson.zip")
    KEEP = {"city", "town", "village", "suburb", "hamlet"}
    places = []
    for f in pp["features"]:
        p = f["properties"]
        kind = (p.get("place") or "").lower()
        if kind not in KEEP:
            continue
        name = p.get("name_en") or p.get("name") or p.get("name_latin")
        if not name:
            continue
        try:
            lon, lat = centroid(f["geometry"])
        except Exception:
            continue
        places.append({
            "n": name.strip(), "k": kind, "x": lon, "y": lat,
            "d": (p.get("adm2_name") or "").strip(),
            "m": (p.get("adm3_name") or "").strip(),
        })
    places.sort(key=lambda r: (r["k"], r["n"], r["x"], r["y"]))
    print(f"     kept {len(places)} of {len(pp['features'])} place features")

    # Display layer: named settlements above hamlet level. Hamlets are used
    # for the population grid weighting but are too numerous to ship as points.
    display = [p for p in places if p["k"] in ("city", "town", "village")]
    write_json("places.json", {"places": display})

    print("4/6  health facilities (OSM via HOT/HDX)")
    hf = osm_geojson("hotosm_npl_health_facilities",
                     "hotosm_npl_health_facilities_osm_geojson.zip")
    TIER1 = {"hospital"}
    TIER2 = {"clinic", "doctors", "health_post", "health_centre"}
    hosp = []
    for f in hf["features"]:
        p = f["properties"]
        am = (p.get("amenity") or "").lower()
        hc = (p.get("healthcare") or "").lower()
        if am in TIER1 or hc in TIER1:
            tier = 1
        elif am in TIER2 or hc in TIER2:
            tier = 2
        else:
            continue
        name = p.get("name_en") or p.get("name") or p.get("name_latin")
        if not name:
            continue
        try:
            lon, lat = centroid(f["geometry"])
        except Exception:
            continue
        hosp.append({
            "n": name.strip(), "t": tier, "x": lon, "y": lat,
            "d": (p.get("adm2_name") or "").strip(),
            "s": (p.get("healthcare_speciality") or "").strip() or None,
            "o": (p.get("operator_type") or "").strip() or None,
        })
    hosp.sort(key=lambda r: (r["t"], r["n"], r["x"], r["y"]))
    n1 = sum(1 for h in hosp if h["t"] == 1)
    print(f"     {n1} hospitals (tier 1), {len(hosp)-n1} clinics/posts (tier 2)")
    write_json("hospitals.json", {"facilities": hosp})

    print("5/6  population grid")
    # Preferred path: real ward counts from the 2021 census. Each ward's
    # observed population is assigned to the grid cell containing its
    # centroid. Wards average about 4,300 people, so at a 5 km cell this is
    # close to a direct measurement rather than an estimate.
    #
    # Fallback, when ward_census.json is absent: the original dasymetric
    # estimate, spreading district totals by settlement and ward density.
    wc_path = os.path.join(DATA, "ward_census.json")
    if os.path.exists(wc_path):
        with open(wc_path, encoding="utf-8") as f:
            wc = json.load(f)
        cells_by_key = defaultdict(int)
        placed = dropped = 0
        for row in wc["wards"]:
            _, _, _, wpop, _, _, x, y = row
            if x is None or y is None or not wpop:
                dropped += 1
                continue
            i = int((x - BBOX["minlon"]) / CELL)
            j = int((y - BBOX["minlat"]) / CELL)
            cells_by_key[(i, j)] += wpop
            placed += 1

        # District attribution, for the per-district exposure breakdown.
        tree = STRtree(polys)
        prepared = [prep(g) for g in polys]
        cells = []
        nearest_assigned = [0]
        for (i, j), val in sorted(cells_by_key.items()):
            lon = round(BBOX["minlon"] + (i + 0.5) * CELL, 4)
            lat = round(BBOX["minlat"] + (j + 0.5) * CELL, 4)
            pt = Point(lon, lat)
            pc = None
            for idx in tree.query(pt):
                if prepared[idx].contains(pt):
                    pc = pcodes[idx]
                    break
            if pc is None:
                # Ward centroids come from the Geoportal, district polygons
                # from COD-AB; the two disagree slightly along borders, so a
                # few cells land just outside every district. Assigning them
                # to the nearest district keeps their people in the
                # per-district breakdown instead of silently dropping them.
                best, bestd = None, float("inf")
                for idx, g_ in enumerate(polys):
                    dd = g_.distance(pt)
                    if dd < bestd:
                        bestd, best = dd, idx
                if best is not None and bestd < 0.15:      # ~15 km
                    pc = pcodes[best]
                    nearest_assigned[0] += 1
            cells.append([lon, lat, int(val), pc])

        gridtotal = sum(c[2] for c in cells)
        print(f"     built from CENSUS WARD COUNTS: {placed} wards placed, "
              f"{dropped} without coordinates")
        print(f"     {len(cells)} cells, total {gridtotal:,} "
              f"(census ward sum {sum(r[3] for r in wc['wards']):,})")
        if nearest_assigned[0]:
            print(f"     {nearest_assigned[0]} cells fell outside every "
                  f"district polygon (Geoportal/COD-AB border disagreement) "
                  f"and were assigned to the nearest district within 15 km")
        nodist = sum(1 for c in cells if c[3] is None)
        if nodist:
            npop = sum(c[2] for c in cells if c[3] is None)
            print(f"     NOTE: {nodist} cells ({npop:,} people) still have no "
                  f"district and are excluded from per-district breakdowns")
        write_json("popgrid.json", {
            "cell_deg": CELL,
            "bbox": BBOX,
            "method": "census_wards",
            "note": ("Observed population. Each ward's 2021 census count is "
                     "assigned to the grid cell containing its centroid. "
                     "Wards average ~4,300 people, so allocation error at a "
                     "5 km cell is small. This is not a dasymetric estimate."),
            "source": "National Population and Housing Census 2021, NSO Nepal",
            "cells": cells,
        })
        print("6/6  provenance")
        write_json("reference_meta.json", {
            "districts": len(feats),
            "polygons_unmatched": sorted(unmatched),
            "boundary_source": "OCHA COD-AB (pcode join)",
            "districts_without_polygon": [pop[p_]["name"] for p_ in missing],
            "population_without_polygon": no_poly_pop,
            "places_display": len(display),
            "places_used_for_weighting": len(places),
            "hospitals_tier1": n1,
            "facilities_total": len(hosp),
            "popgrid_cells": len(cells),
            "popgrid_total": gridtotal,
            "popgrid_method": "census_wards",
            "census_total": census_total,
            "sources": {
                "boundaries": "OCHA COD-AB",
                "population": "Census 2021 ward counts (NSO Nepal)",
                "places_and_health": "OpenStreetMap via HOT on HDX (ODbL)",
            },
        })
        print("done.")
        return

    print("     ward_census.json absent — falling back to dasymetric estimate")
    # Each district's census population is spread over the grid cells inside
    # it, in proportion to how many OSM settlements fall in each cell. A small
    # uniform floor keeps unmapped but inhabited cells from going to zero.
    # This is an approximation, and is labelled as such in the interface.
    tree = STRtree(polys)
    prepared = [prep(g) for g in polys]

    def district_of(lon, lat):
        pt = Point(lon, lat)
        for idx in tree.query(pt):
            if prepared[idx].contains(pt):
                return idx
        return None

    nx = int(round((BBOX["maxlon"] - BBOX["minlon"]) / CELL))
    ny = int(round((BBOX["maxlat"] - BBOX["minlat"]) / CELL))

    cell_district = {}
    for j in range(ny):
        for i in range(nx):
            lon = BBOX["minlon"] + (i + 0.5) * CELL
            lat = BBOX["minlat"] + (j + 0.5) * CELL
            d = district_of(lon, lat)
            if d is not None:
                cell_district[(i, j)] = d

    # Ward-derived weights, if build_wards.py has produced them. Ward
    # boundaries are drawn administratively with population in mind, so ward
    # density carries a population signal independent of OpenStreetMap
    # coverage. Blending it with settlement density reduces the OSM mapping
    # bias that this grid would otherwise inherit wholesale.
    ward_w = {}
    wpath = os.path.join(DATA, "ward_weights.json")
    if os.path.exists(wpath):
        with open(wpath, encoding="utf-8") as f:
            wj = json.load(f)
        if abs(wj["cell_deg"] - CELL) > 1e-9:
            print("     ward_weights.json uses a different cell size; "
                  "ignoring it")
        else:
            for lon, lat, n, inv in wj["cells"]:
                i = int((lon - BBOX["minlon"]) / CELL)
                j = int((lat - BBOX["minlat"]) / CELL)
                ward_w[(i, j)] = (n, inv)
            print(f"     blending ward weights from {len(ward_w)} cells")
    else:
        print("     no ward_weights.json; using settlement density alone")

    weight = defaultdict(float)
    for p in places:
        i = int((p["x"] - BBOX["minlon"]) / CELL)
        j = int((p["y"] - BBOX["minlat"]) / CELL)
        if (i, j) in cell_district:
            # A city seeds far more population than a hamlet.
            weight[(i, j)] += {"city": 40, "town": 12, "suburb": 6,
                               "village": 3, "hamlet": 1}[p["k"]]

    by_district = defaultdict(list)
    for cell, d in cell_district.items():
        by_district[d].append(cell)

    cells = []
    FLOOR = 0.35      # every inhabited-district cell gets some weight
    WARD_W = 2.5      # relative pull of the ward signal vs settlement density
    for d, cs in by_district.items():
        pc = pcodes[d]
        if pc is None or pop.get(pc) is None:
            continue
        total = pop[pc]["pop"]
        w = {}
        for c in cs:
            v = weight.get(c, 0.0) + FLOOR
            if c in ward_w:
                n, inv = ward_w[c]
                # Count of ward centres, plus a bounded inverse-area term so
                # that dense urban wards outweigh large rural ones without a
                # single tiny ward dominating its district.
                v += WARD_W * (n + min(inv, 20.0) * 0.5)
            w[c] = v
        s = sum(w.values())
        if s <= 0:
            continue
        for c, wv in w.items():
            share = total * wv / s
            if share < 1:
                continue
            i, j = c
            cells.append([
                round(BBOX["minlon"] + (i + 0.5) * CELL, 4),
                round(BBOX["minlat"] + (j + 0.5) * CELL, 4),
                int(round(share)),
                pc,
            ])
    cells.sort(key=lambda r: (r[0], r[1]))
    gridtotal = sum(c[2] for c in cells)
    poptotal = census_total
    print(f"     {len(cells)} cells, grid total {gridtotal:,} vs "
          f"census total {poptotal:,} "
          f"({100*gridtotal/poptotal:.2f}% retained)")

    write_json("popgrid.json", {
        "cell_deg": CELL,
        "bbox": BBOX,
        "note": ("Dasymetric estimate. District census totals (COD-PS 2023) "
                 "redistributed within each district by OSM settlement "
                 "density. Cell values are estimates, not measurements."),
        "cells": cells,
    })

    print("6/6  provenance")
    write_json("reference_meta.json", {
        "districts": len(feats),
        "polygons_unmatched": sorted(unmatched),
        "boundary_source": "OCHA COD-AB (pcode join)",
        "districts_without_polygon": [pop[p]["name"] for p in missing],
        "population_without_polygon": no_poly_pop,
        "places_display": len(display),
        "places_used_for_weighting": len(places),
        "hospitals_tier1": n1,
        "facilities_total": len(hosp),
        "popgrid_cells": len(cells),
        "popgrid_total": gridtotal,
        "census_total": poptotal,
        "sources": {
            "boundaries": "geoBoundaries gbOpen NPL ADM2 (CC BY 3.0 IGO; "
                          "source Survey Department of Nepal / OCHA FISS)",
            "population": "Nepal Subnational Population Statistics (COD-PS), "
                          "HDX dataset cod-ps-npl, 2023 release",
            "places_and_health": "OpenStreetMap via Humanitarian OpenStreetMap "
                                 "Team exports on HDX (ODbL)",
        },
    })
    print("done.")


if __name__ == "__main__":
    sys.exit(main())

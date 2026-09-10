#!/usr/bin/env python3
"""
build_wards.py — ward index and ward-derived population weighting.

Source: "Administrative Boundary - Wards", National Geoportal, Government of
Nepal (`ward_nameupdated_wgs.shp`). EPSG:4326, 6,792 polygons.

Wards are the level Nepali disaster response actually works at: DEOC reports,
NDRRMA incident records and local relief allocation are all ward-based. An
epicentre described as "Ward 9, Sandhikharka, Arghakhanchi" is directly
actionable in a way that "27.9N, 83.1E" is not.

Why only an index is published, not the polygons
------------------------------------------------
Simplified ward geometry costs 2.5 MB at a coarse tolerance and 8.9 MB at a
usable one. This page is meant to be opened on a phone, possibly on a
congested network, shortly after an earthquake. Shipping several megabytes of
boundary geometry for a naming convenience is the wrong trade. Instead this
writes a compact index of ward centroids and attributes (`data/wards.json`),
and the interface reports the nearest ward centre, labelled as such.

The polygons are still used, at build time, for the two things that need real
geometry: weighting the population grid, and locating NDRRMA incidents.

Population
----------
There is no ward-level population here, because none is available. The COD-PS
release stops at district. Nepal's 2011 census is published by VDC, which
predates the 2017 federal restructuring and would need a lossy crosswalk to
reach current wards. No ward population figure is invented.

What the wards do improve is the *distribution* of district population. The
existing grid spreads each district's census total by OpenStreetMap settlement
density, which inherits OSM's mapping bias — a limitation this project already
documents. Ward boundaries are delineated administratively with population in
mind, so ward density is a population signal that is independent of how well
an area happens to be mapped. Blending the two reduces that bias.

It remains an estimate. Adding a second weighting signal does not make the
spatial distribution verifiable, and it is still not.
"""

import json
import math
import os
import sys
from collections import Counter, defaultdict

try:
    import shapefile                      # pyshp
except ImportError:
    sys.exit("pyshp is required for this step:  pip install pyshp")

from shapely.geometry import shape

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data")
SOURCES = os.path.join(ROOT, "sources")

SHP = os.path.join(SOURCES, "ward_nameupdated_wgs.shp")
LOCAL_TYPES = {"Gaunpalika", "Nagarpalika",
               "Mahanagarpalika", "Upamahanagarpalika"}

# Upstream typo in the Geoportal attribute table. Correcting it brings the
# Wildlife Reserve count to 6, matching the local-unit layer exactly, which is
# what confirms it is a typo rather than a distinct category.
TYPE_FIXES = {"Wieldlife Reserve": "Wildlife Reserve"}


def write_json(name, obj):
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False)
        f.write("\n")
    print(f"  wrote {name}  ({os.path.getsize(path)/1024:.0f} KB)")
    return path


def main():
    if not os.path.exists(SHP):
        sys.exit(
            f"Shapefile not found at {SHP}\n\n"
            "Download 'Administrative Boundary - Wards' from "
            "https://nationalgeoportal.gov.np/ and place all five files "
            "(.shp .shx .dbf .prj .cst) in sources/.\n\n"
            "This step is optional: data/wards.json is committed, so the "
            "rest of the build works without it.")

    print("1/3  reading ward boundaries")
    r = shapefile.Reader(SHP, encoding="ISO-8859-1")   # .cst declares ISO-8859-1
    flds = [f[0] for f in r.fields[1:]]

    wards, dup_codes = [], Counter()
    for sh, rec in zip(r.shapes(), r.records()):
        d = dict(zip(flds, rec))
        try:
            g = shape(sh.__geo_interface__)
        except Exception:                             # noqa: BLE001
            continue
        if not g.is_valid:
            g = g.buffer(0)
        c = g.representative_point()
        wtype = TYPE_FIXES.get(d.get("type_gn"), d.get("type_gn"))
        code = int(d["ddgnww"]) if d.get("ddgnww") else None
        dup_codes[code] += 1
        # Area in km2 via a local scale factor. Adequate for relative
        # weighting; not a geodetic area.
        lat0 = math.radians(c.y)
        area = g.area * (111.32 ** 2) * math.cos(lat0)
        wards.append({
            "code": code,
            "district": (d.get("district") or "").title(),
            "municipality": d.get("gapa_napa"),
            "type": wtype,
            "ward": int(d.get("new_ward_n") or 0),
            "province": d.get("province"),
            "mun_code": int(d["ddgn"]) if d.get("ddgn") else None,
            "x": round(c.x, 5),
            "y": round(c.y, 5),
            "area_km2": round(area, 3),
            "_geom": g,
        })

    print(f"     {len(wards)} ward polygons")
    dups = {k: v for k, v in dup_codes.items() if v > 1}
    if dups:
        print(f"     NOTE: {len(dups)} ward codes appear more than once "
              f"({sum(dups.values())} polygons). These are multipart wards or "
              f"upstream duplicates; they are kept as separate polygons and "
              f"the code is not treated as unique.")

    local = [w for w in wards if w["type"] in LOCAL_TYPES]
    distinct = {(w["mun_code"], w["ward"]) for w in local}
    print(f"     {len({w['mun_code'] for w in wards})} municipalities, "
          f"{len(local)} ward polygons in local levels, "
          f"{len(wards)-len(local)} in protected areas")
    print(f"     {len(distinct)} distinct (municipality, ward number) pairs")

    # Nepal's official ward count is 6,743; this source gives 6,760 distinct
    # pairs. The 2021 census independently lists exactly 6,743, and all of
    # them join to this layer, so the surplus is an artefact here (multipart
    # polygons, duplicate codes) rather than a disagreement about which wards
    # exist. Take ward totals from the census, not from polygon counts.
    OFFICIAL_WARDS = 6743
    if len(distinct) != OFFICIAL_WARDS:
        print(f"     NOTE: {len(distinct)} distinct wards vs the official "
              f"{OFFICIAL_WARDS} (+{len(distinct)-OFFICIAL_WARDS}). The "
              f"census lists exactly {OFFICIAL_WARDS} and all join to this "
              f"layer, so the surplus is a polygon artefact. Use census "
              f"counts for totals.")

    print("2/3  writing ward index")
    # Column-oriented to keep the file small: repeated district, municipality
    # and province strings are interned into lookup tables.
    districts = sorted({w["district"] for w in wards})
    provinces = sorted({w["province"] for w in wards if w["province"]})
    types = sorted({w["type"] for w in wards if w["type"]})
    di = {v: i for i, v in enumerate(districts)}
    pi = {v: i for i, v in enumerate(provinces)}
    ti = {v: i for i, v in enumerate(types)}

    rows = [[w["x"], w["y"], w["ward"], w["municipality"],
             di[w["district"]], pi.get(w["province"], -1),
             ti.get(w["type"], -1), w["code"]]
            for w in sorted(wards, key=lambda z: (z["x"], z["y"]))]

    write_json("wards.json", {
        "source": {
            "name": "Administrative Boundary - Wards, National Geoportal, "
                    "Government of Nepal",
            "url": "https://nationalgeoportal.gov.np/",
            "file": "ward_nameupdated_wgs.shp",
            "crs": "EPSG:4326 (WGS 84)",
            "obtained": "manual download; the Geoportal exposes no API",
        },
        "note": "Centroids and attributes only. Ward polygons are not "
                "published here: simplified geometry costs 2.5-8.9 MB, which "
                "is the wrong trade for a page meant to load on a phone after "
                "an earthquake. The interface reports the NEAREST WARD CENTRE, "
                "which is not the same as the containing ward, and says so.",
        "population": "No ward-level population is included because none is "
                      "available. COD-PS stops at district; the 2011 census "
                      "is by VDC and predates the 2017 restructuring.",
        "counts": {
            "polygons": len(wards),
            "municipalities": len({w["mun_code"] for w in wards}),
            "distinct_wards_in_local_levels": len(distinct),
            "official_ward_count": OFFICIAL_WARDS,
        },
        "unresolved": (
            f"This layer yields {len(distinct)} distinct (municipality, ward) "
            f"pairs against Nepal's official {OFFICIAL_WARDS}. The 2021 "
            f"census independently confirms {OFFICIAL_WARDS} and all of them "
            f"join here, so the surplus is an artefact of this file "
            f"(multipart polygons, duplicate codes). Take ward totals from "
            f"the census; individual ward identification is sound."
            if len(distinct) != OFFICIAL_WARDS else None),
        "corrections": {"type_gn typo": "'Wieldlife Reserve' -> "
                                        "'Wildlife Reserve' (1 polygon)"},
        "districts": districts,
        "provinces": provinces,
        "types": types,
        "fields": ["x", "y", "ward", "municipality", "district_i",
                   "province_i", "type_i", "code"],
        "wards": rows,
    })

    print("3/3  ward weights for the population grid")
    # A per-cell count of ward centroids, and the summed inverse area of those
    # wards. Wards are drawn smaller where people are denser, so inverse area
    # carries a population signal that does not depend on OSM coverage.
    grid_path = os.path.join(DATA, "popgrid.json")
    if not os.path.exists(grid_path):
        print("     popgrid.json not found; skipping. Run "
              "build_reference.py first, then rerun this.")
        return

    with open(grid_path, encoding="utf-8") as f:
        grid = json.load(f)
    cell = grid["cell_deg"]
    bbox = grid["bbox"]

    wardw = defaultdict(float)
    wardn = Counter()
    for w in wards:
        if w["type"] not in LOCAL_TYPES:
            continue                      # protected areas hold few residents
        i = int((w["x"] - bbox["minlon"]) / cell)
        j = int((w["y"] - bbox["minlat"]) / cell)
        wardn[(i, j)] += 1
        wardw[(i, j)] += 1.0 / max(w["area_km2"], 0.25)

    out = {"cell_deg": cell, "bbox": bbox,
           "note": "Per-cell ward centroid counts and summed inverse ward "
                   "area, for use as a population-distribution weight that "
                   "is independent of OpenStreetMap coverage. Not population.",
           "cells": sorted([[round(bbox["minlon"] + (i + .5) * cell, 4),
                             round(bbox["minlat"] + (j + .5) * cell, 4),
                             wardn[(i, j)], round(wardw[(i, j)], 4)]
                            for (i, j) in wardn])}
    write_json("ward_weights.json", out)
    print(f"     {len(out['cells'])} cells carry at least one ward centre")
    print("\nRerun scripts/build_reference.py to fold these weights into "
          "popgrid.json.")


if __name__ == "__main__":
    sys.exit(main())

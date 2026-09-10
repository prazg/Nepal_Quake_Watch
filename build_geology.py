#!/usr/bin/env python3
"""
build_geology.py — active fault traces and local-level boundaries.

Faults
------
Source: GEM Global Active Faults Database (GEMScienceTools), the harmonised
global compilation. Clipped to the Nepal region bounding box.

These are mapped *active fault traces* with, where known, slip type and net
slip rate. They are the geological structures capable of generating
earthquakes. They are not a hazard map: a fault trace tells you where a
structure is, not the probability of ground motion at a site. Nepal's
authoritative design zonation is NBC 105:2020 and is not reproduced here.

The Main Himalayan Thrust system underlies most of Nepal, so proximity to a
mapped surface trace is a weak predictor of shaking on its own. The interface
presents faults as geological context, not as a risk score.

Local levels
------------
Boundaries come from OCHA COD-AB admin3, which carries pcodes. Unit types come
from Open Knowledge Nepal's LocalBoundaries (CC BY 4.0), which carries the type
but no pcodes, and are joined geometrically.

Both sources give 775 polygons where Nepal's local-level count is usually cited
as 753. The type field resolves this: 753 are local levels (460 rural
municipalities, 276 municipalities, 11 sub-metropolitan, 6 metropolitan) and 22
are national parks, wildlife reserves, hunting reserves, a watershed reserve
and a development area. Those 22 are not local levels and are largely
unpopulated, so they are flagged with is_local_level=false rather than counted
as municipalities.
"""

import io
import json
import math
import os
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))
CACHE = os.path.abspath(os.path.join(HERE, "..", ".cache"))
os.makedirs(CACHE, exist_ok=True)

from collections import Counter                               # noqa: E402
from shapely.geometry import shape, box                       # noqa: E402
from shapely.strtree import STRtree                           # noqa: E402

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}
BBOX = dict(minlon=79.5, maxlon=88.8, minlat=25.8, maxlat=31.0)

GEM = ("https://raw.githubusercontent.com/GEMScienceTools/"
       "gem-global-active-faults/master/geojson/"
       "gem_active_faults_harmonized.geojson")
HDX_PKG = "https://data.humdata.org/api/3/action/package_show?id={ds}"
LOCALBOUNDARIES = ("https://localboundries.oknp.org/data/local-level/"
                   "nepal.geojson")


def fetch(url, dest):
    path = os.path.join(CACHE, dest)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    print(f"  downloading {dest} ...", flush=True)
    with urllib.request.urlopen(
            urllib.request.Request(url, headers=UA), timeout=600) as r, \
            open(path, "wb") as f:
        f.write(r.read())
    return path


def write_json(name, obj):
    path = os.path.join(DATA, name)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False)
        f.write("\n")
    print(f"  wrote {name}  ({os.path.getsize(path)/1024:.0f} KB)")


def main():
    clip = box(BBOX["minlon"], BBOX["minlat"], BBOX["maxlon"], BBOX["maxlat"])

    print("1/3  active faults (GEM Global Active Faults Database)")
    path = fetch(GEM, "gem_active_faults.geojson")
    with open(path, encoding="utf-8") as f:
        gem = json.load(f)

    feats = []
    total_km = 0.0
    for ft in gem["features"]:
        try:
            g = shape(ft["geometry"])
        except Exception:                             # noqa: BLE001
            continue
        if not g.intersects(clip):
            continue
        g = g.intersection(clip)
        if g.is_empty:
            continue
        p = ft["properties"]
        # Rough length in km for context only, using a mid-latitude scale
        # factor. Not a geodesic measurement.
        lat0 = math.radians((BBOX["minlat"] + BBOX["maxlat"]) / 2)
        total_km += g.length * 111.32 * math.cos(lat0) if g.length else 0
        feats.append({
            "type": "Feature",
            "properties": {
                "name": p.get("name") or None,
                "slip_type": p.get("slip_type") or None,
                "slip_rate": p.get("net_slip_rate") or None,
                "dip": p.get("average_dip") or None,
                "catalog": p.get("catalog_name") or None,
            },
            "geometry": json.loads(json.dumps(
                g.simplify(0.002, preserve_topology=True).__geo_interface__),
                parse_float=lambda s: round(float(s), 4)),
        })

    print(f"     {len(feats)} fault traces intersect the Nepal region "
          f"(of {len(gem['features'])} global)")
    write_json("faults.json", {
        "type": "FeatureCollection",
        "meta": {
            "source": "GEM Global Active Faults Database, harmonised "
                      "compilation (GEMScienceTools)",
            "url": "https://github.com/GEMScienceTools/gem-global-active-faults",
            "caveat": "Mapped active fault traces. This is geological "
                      "context, not a seismic hazard map, and not the "
                      "national design zonation (NBC 105:2020).",
        },
        "features": feats,
    })

    print("2/3  local levels (municipalities)")
    meta = json.load(urllib.request.urlopen(
        urllib.request.Request(HDX_PKG.format(ds="cod-ab-npl"), headers=UA),
        timeout=120))
    url = next(r["url"] for r in meta["result"]["resources"]
               if r["name"] == "npl_admin_boundaries.geojson.zip")
    zpath = fetch(url, "cod_ab.zip")
    with zipfile.ZipFile(zpath) as f_z:
        with f_z.open("npl_admin3.geojson") as f:
            adm3 = json.load(io.TextIOWrapper(f, encoding="utf-8"))

    print("3/3  unit names and types")
    # COD-AB carries pcodes and geometry but no unit type, and its
    # transliterations are out of date for 174 units.
    #
    # Preferred source is data/local_units.json, derived by
    # scripts/build_local_units.py from the National Geoportal's official
    # "name updated" layer. That file is committed, so a clone works without
    # the 18 MB shapefile.
    #
    # Fallback is Open Knowledge Nepal's LocalBoundaries, fetched live. It
    # gives the same 753/22 split but the older spellings.
    LOCAL_TYPES = {"Gaunpalika", "Nagarpalika",
                   "Mahanagarpalika", "Upamahanagarpalika"}

    lut_path = os.path.join(DATA, "local_units.json")
    lut, source_label = {}, None
    if os.path.exists(lut_path):
        with open(lut_path, encoding="utf-8") as f:
            lut = json.load(f)["units"]
        source_label = "National Geoportal (official, name-updated)"
        print(f"     using data/local_units.json — {len(lut)} units")
    else:
        print("     data/local_units.json absent, falling back to "
              "LocalBoundaries")
        source_label = "Open Knowledge Nepal, LocalBoundaries"
        lb_path = fetch(LOCALBOUNDARIES, "oknp_local_level.geojson")
        with open(lb_path, encoding="utf-8") as f:
            lb = json.load(f)
        lb_shapes = []
        for f_ in lb["features"]:
            try:
                lb_shapes.append((shape(f_["geometry"]),
                                  f_["properties"].get("Type_GN")))
            except Exception:                         # noqa: BLE001
                continue
        lb_tree = STRtree([g for g, _ in lb_shapes])

    out, typed = [], 0
    for ft in adm3["features"]:
        p = ft["properties"]
        pcode = p.get("adm3_pcode")
        g_full = shape(ft["geometry"])
        g = g_full.simplify(0.003, preserve_topology=True)

        name = p.get("adm3_name")
        alias = None
        unit_type = None
        province = p.get("adm1_name")

        rec = lut.get(pcode)
        if rec:
            name = rec["name"]
            alias = rec.get("name_codab")
            unit_type = rec["type"]
            province = rec.get("province") or province
        elif lut:
            pass                                      # pcode missing from LUT
        else:
            pt = g_full.representative_point()
            for idx in lb_tree.query(pt):
                if lb_shapes[idx][0].contains(pt):
                    unit_type = lb_shapes[idx][1]
                    break
        if unit_type:
            typed += 1

        out.append({
            "type": "Feature",
            "properties": {
                "name": name,
                "name_codab": alias,
                "pcode": pcode,
                "district": p.get("adm2_name"),
                "district_pcode": p.get("adm2_pcode"),
                "province": province,
                "unit_type": unit_type,
                "is_local_level": unit_type in LOCAL_TYPES if unit_type else None,
            },
            "geometry": json.loads(json.dumps(g.__geo_interface__),
                                   parse_float=lambda s: round(float(s), 4)),
        })

    pcodes = {f["properties"]["pcode"] for f in out}
    counts = Counter(f["properties"]["unit_type"] for f in out)
    n_local = sum(v for k, v in counts.items() if k in LOCAL_TYPES)
    n_other = sum(v for k, v in counts.items()
                  if k and k not in LOCAL_TYPES)

    renamed = sum(1 for f in out if f["properties"]["name_codab"])
    print(f"     {len(out)} polygons, {len(pcodes)} distinct pcodes, "
          f"{typed} typed ({100*typed/len(out):.1f}%)")
    if renamed:
        print(f"     {renamed} use the official Geoportal spelling; the "
              f"COD-AB spelling is retained in name_codab")
    for k, v in sorted(counts.items(), key=lambda r: -r[1]):
        print(f"       {str(k):<32} {v:>4}")
    print(f"     -> {n_local} actual local levels, "
          f"{n_other} protected areas and other units")

    # This is the check that explains the 775-vs-753 discrepancy. If it ever
    # stops holding, something upstream has changed and the counts in the
    # interface can no longer be trusted.
    if n_local != 753:
        print(f"     WARNING: {n_local} local levels, expected 753 "
              f"(460 rural + 276 municipalities + 11 sub-metro + 6 metro). "
              f"Verify before publishing any per-unit statistic.")
    else:
        print("     matches Nepal's official count of 753 local levels")

    write_json("municipalities.json", {
        "type": "FeatureCollection",
        "meta": {
            "boundaries": "OCHA COD-AB admin3 (carries pcodes)",
            "names_and_types": source_label,
            "note": f"{len(out)} polygons = {n_local} local levels "
                    f"+ {n_other} protected areas and other units. The "
                    f"commonly cited figure of 753 counts local levels only.",
        },
        "features": out,
    })
    print("done.")


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
build_local_units.py — official local-unit names and types.

Source: "Administrative Boundary - Local Level with Province Names",
National Geoportal, Government of Nepal (https://nationalgeoportal.gov.np/).
WGS84, 777 polygons, 775 distinct units.

Why this is a manual step
-------------------------
The National Geoportal is a JavaScript viewer. No ArcGIS REST or OGC service
root was discoverable — /arcgis/rest/services, /server/rest/services and /api/
all return the application's HTML rather than a service document. The
shapefile therefore has to be downloaded by hand and placed in sources/.

Because that cannot be automated, this script writes a small derived lookup
(data/local_units.json, keyed by COD-AB pcode) which IS committed. Everything
downstream reads the lookup, so a clone builds correctly without the 18 MB
shapefile present. Rerun this only when the Geoportal publishes an update.

What it contributes over the alternatives
-----------------------------------------
  - Official current spellings. The file is explicitly "name updated" and
    differs from OCHA COD-AB on 174 of 775 unit names (Phaktanglung not
    Phaktanlung, Janakpurdham not Janakpur, Shivasataxi not Shivasatakshi).
    Both spellings are kept: the Geoportal one as the display name, the
    COD-AB one as an alias so existing joins keep working.
  - Unit type, which COD-AB does not carry.
  - Named provinces.

It does NOT replace COD-AB for geometry, because the Geoportal file carries no
pcodes and pcodes are what the population join depends on. Geometry and pcodes
come from COD-AB; names and types come from here; the two are joined by
centroid containment, which matches 775 of 775.

Independent confirmation of the 753/775 split
---------------------------------------------
This authoritative source gives the same breakdown already derived from Open
Knowledge Nepal's LocalBoundaries: 753 local levels (460 Gaunpalika, 276
Nagarpalika, 11 Upamahanagarpalika, 6 Mahanagarpalika) plus 22 protected
areas and other units. Three independent datasets now agree.
"""

import io
import json
import os
import sys
import unicodedata
import re
import zipfile
import urllib.request
from collections import Counter

try:
    import shapefile                      # pyshp
except ImportError:
    sys.exit("pyshp is required for this step:  pip install pyshp")

from shapely.geometry import shape
from shapely.strtree import STRtree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, ".cache")
SOURCES = os.path.join(ROOT, "sources")

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}
HDX_PKG = "https://data.humdata.org/api/3/action/package_show?id={ds}"

SHP = os.path.join(SOURCES, "local_unit_nameupdated_wgs.shp")
LOCAL_TYPES = {"Gaunpalika", "Nagarpalika",
               "Mahanagarpalika", "Upamahanagarpalika"}


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", s.lower())


def cod_admin3():
    """COD-AB admin3 supplies geometry and pcodes."""
    cached = os.path.join(CACHE, "cod_ab.zip")
    if not os.path.exists(cached):
        os.makedirs(CACHE, exist_ok=True)
        meta = json.load(urllib.request.urlopen(
            urllib.request.Request(HDX_PKG.format(ds="cod-ab-npl"), headers=UA),
            timeout=120))
        url = next(r["url"] for r in meta["result"]["resources"]
                   if r["name"] == "npl_admin_boundaries.geojson.zip")
        print("  downloading COD-AB ...", flush=True)
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=600) as r, \
                open(cached, "wb") as f:
            f.write(r.read())
    with zipfile.ZipFile(cached) as z:
        with z.open("npl_admin3.geojson") as f:
            return json.load(io.TextIOWrapper(f, encoding="utf-8"))


def main():
    if not os.path.exists(SHP):
        sys.exit(
            f"Shapefile not found at {SHP}\n\n"
            "Download 'Administrative Boundary - Local Level with Province "
            "Names' from https://nationalgeoportal.gov.np/ and place all five "
            "files (.shp .shx .dbf .prj .cst) in sources/.\n\n"
            "This step is optional: data/local_units.json is committed, so "
            "the rest of the build works without it.")

    print("1/3  reading National Geoportal shapefile")
    # The .cst sidecar declares ISO-8859-1. Passing it explicitly avoids
    # mojibake in names carrying diacritics.
    r = shapefile.Reader(SHP, encoding="ISO-8859-1")
    flds = [f[0] for f in r.fields[1:]]
    polys, attrs = [], []
    for sh, rec in zip(r.shapes(), r.records()):
        d = dict(zip(flds, rec))
        try:
            g = shape(sh.__geo_interface__)
        except Exception:                             # noqa: BLE001
            continue
        if not g.is_valid:
            g = g.buffer(0)                           # fix self-intersections
        polys.append(g)
        attrs.append(d)
    print(f"     {len(polys)} polygons")

    distinct = {(a["district"], a["gapa_napa"]): a["type_gn"] for a in attrs}
    counts = Counter(distinct.values())
    n_local = sum(v for k, v in counts.items() if k in LOCAL_TYPES)
    print(f"     {len(distinct)} distinct units: {n_local} local levels, "
          f"{len(distinct)-n_local} protected areas and other")
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"       {k:<34} {v:>4}")
    if n_local != 753:
        print(f"     WARNING: {n_local} local levels, expected 753. The "
              f"Geoportal may have republished; verify before using.")

    print("2/3  joining to COD-AB pcodes by centroid containment")
    adm3 = cod_admin3()
    tree = STRtree(polys)

    lut, renamed, unmatched = {}, 0, []
    for ft in adm3["features"]:
        p = ft["properties"]
        pcode = p.get("adm3_pcode")
        pt = shape(ft["geometry"]).representative_point()
        found = None
        for idx in tree.query(pt):
            if polys[idx].contains(pt):
                found = attrs[idx]
                break
        if not found:
            unmatched.append(p.get("adm3_name"))
            continue
        cod_name = p.get("adm3_name")
        official = found["gapa_napa"]
        if norm(official) != norm(cod_name):
            renamed += 1
        lut[pcode] = {
            "name": official,
            "name_codab": cod_name if norm(official) != norm(cod_name) else None,
            "type": found["type_gn"],
            "is_local_level": found["type_gn"] in LOCAL_TYPES,
            "district": found["district"].title(),
            "province": found["province"],
        }

    print(f"     {len(lut)}/{len(adm3['features'])} matched "
          f"({100*len(lut)/len(adm3['features']):.1f}%)")
    print(f"     {renamed} units have an updated official spelling; the "
          f"COD-AB spelling is kept as an alias")
    if unmatched:
        print(f"     WARNING: unmatched: {unmatched}")

    print("3/3  writing lookup")
    out = {
        "source": {
            "name": "Administrative Boundary - Local Level with Province "
                    "Names, National Geoportal, Government of Nepal",
            "url": "https://nationalgeoportal.gov.np/",
            "file": "local_unit_nameupdated_wgs.shp",
            "crs": "EPSG:4326 (WGS 84)",
            "obtained": "manual download; the Geoportal exposes no API",
        },
        "join": "centroid containment against OCHA COD-AB admin3 pcodes",
        "counts": {
            "units": len(lut),
            "local_levels": sum(1 for v in lut.values() if v["is_local_level"]),
            "other": sum(1 for v in lut.values() if not v["is_local_level"]),
            "renamed_vs_codab": renamed,
        },
        "units": lut,
    }
    path = os.path.join(DATA, "local_units.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False)
        f.write("\n")
    print(f"  wrote local_units.json ({os.path.getsize(path)/1024:.0f} KB)")
    print("\nNow rerun scripts/build_geology.py to fold these into "
          "municipalities.json.")


if __name__ == "__main__":
    sys.exit(main())

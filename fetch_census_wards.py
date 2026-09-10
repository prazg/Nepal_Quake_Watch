#!/usr/bin/env python3
"""
fetch_census_wards.py — ward-level population and building vulnerability.

Source: National Population and Housing Census 2021, published by Nepal's
National Statistics Office at https://censusresults.nsonepal.gov.np/

This replaces the single weakest number in the project. Until now, population
exposure was a dasymetric *estimate*: district census totals smeared across a
5 km grid by settlement density. This script supplies the real thing —
observed population for all 6,743 wards, at roughly 4,300 people per ward
instead of 400,000 per district.

It also adds something the project had no substitute for: building
vulnerability. Census table 03 records outer wall material for every ward.
Mud-bonded brick or stone, unbaked brick and bamboo construction is what
kills people in Himalayan earthquakes, and the share of it varies enormously
between a Kathmandu ward and a hill ward in Achham.

How the files are reached
-------------------------
The census portal is a Next.js application with no documented API. The file
list is embedded in the page bundle and the download path is

    /files/ward/P{1..7}/{filename}.xlsx

with P1..P7 being Koshi, Madhesh, Bagmati, Gandaki, Lumbini, Karnali and
Sudurpaschim. This was established by reading the client bundle. It is an
undocumented path and may change without notice, which is why this script
verifies its own output and fails loudly rather than silently shipping less.

The join, which is the hard part
--------------------------------
Census spreadsheets carry names, not pcodes, and its transliterations differ
from the boundary file's (Panchdebalbinayak/Panchadewalbinayak,
Bhumikasthan/Bhumekasthan, Turmakhand/Turmakhad). Names also carry the unit
type, and the census file misspells it as "Metropolitian" in places.

The join therefore runs in three stages, most reliable first:
  1. district, exact then fuzzy — 77 of 77, 7 needing fuzzy
  2. municipality within the matched district, exact then fuzzy — 751 of 753
  3. the last 2 by elimination: where one census municipality and one boundary
     municipality remain unmatched in the same district, they must correspond.
     This resolves Gangadev/Sukidaha in Rolpa and Mahakali/Dodhara Chandani
     in Kanchanpur, which are genuine renames rather than spelling variants.
     Elimination is a structural argument, not a claim about the names; both
     pairs are reported so they can be verified.
"""

import difflib
import json
import os
import re
import sys
import unicodedata
import urllib.request
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, ".cache", "census")
os.makedirs(CACHE, exist_ok=True)

BASE = "https://censusresults.nsonepal.gov.np/files/ward"
UA = {"User-Agent": "Mozilla/5.0 (compatible; nepal-quake-watch/0.1)"}

PROVINCES = {1: "Koshi", 2: "Madhesh", 3: "Bagmati", 4: "Gandaki",
             5: "Lumbini", 6: "Karnali", 7: "Sudurpaschim"}

TABLES = {
    "population": "WRD_Indv01-HouseholdAndPopulation.xlsx",
    "wall": "WRD_Hhld03-OuterWallOfHouse.xlsx",
}

# Census 2021 published national total, used as a reconciliation target.
PUBLISHED_TOTAL = 29_164_578
OFFICIAL_WARDS = 6743

# Outer wall materials, by seismic performance. Column order follows the
# census table header: mud-bonded brick/stone, cement-bonded brick/stone,
# wood/planks, bamboo, unbaked brick, galvanized sheet, prefabricated, other.
WALL_COLS = ["mud_bonded", "cement_bonded", "wood", "bamboo",
             "unbaked_brick", "galvanized", "prefab", "other"]
# Materials that perform badly in earthquakes. Mud-bonded masonry and unbaked
# brick are the dominant killers in Himalayan events; bamboo is light and
# fails less lethally but is still non-engineered.
VULNERABLE = {"mud_bonded", "unbaked_brick", "bamboo"}

TYPE_SUFFIXES = ["gaunpalika", "nagarpalika", "mahanagarpalika",
                 "upamahanagarpalika", "ruralmunicipality",
                 "submetropolitancity", "metropolitancity",
                 "submetropolitiancity", "metropolitiancity",  # census typos
                 "municipality"]


def norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def strip_type(m):
    n = norm(m)
    for t in sorted(TYPE_SUFFIXES, key=len, reverse=True):
        if n.endswith(t):
            return n[:-len(t)]
    return n


def fetch(province, filename):
    dest = os.path.join(CACHE, f"P{province}_{filename}")
    if os.path.exists(dest) and os.path.getsize(dest) > 10_000:
        return dest
    url = f"{BASE}/P{province}/{filename}"
    print(f"     downloading P{province} {filename} ...", flush=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
        f.write(r.read())
    return dest


def parse_sheet(path, ncols):
    """
    Census sheets nest area in columns A-C (province, district, local level)
    with the ward number in column D and values from column E. "All wards"
    subtotal rows are skipped so totals are not double counted.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    district = mun = None
    rows = []
    for row in ws.iter_rows(values_only=True):
        a, b, c, d = row[0], row[1], row[2], row[3]
        if isinstance(a, str) and a.strip().isupper() and len(a.strip()) > 2:
            district = mun = None
            continue
        if isinstance(b, str) and b.strip():
            district, mun = b.strip(), None
            continue
        if isinstance(c, str) and c.strip():
            mun = c.strip()
            continue
        if d is None or not district or not mun:
            continue
        w = str(d).strip()
        if w.lower().startswith("all"):
            continue
        try:
            ward = int(float(w))
        except ValueError:
            continue
        vals = []
        for v in row[4:4 + ncols]:
            try:
                vals.append(int(float(v)) if v not in (None, "") else 0)
            except (TypeError, ValueError):
                vals.append(0)
        rows.append((district, mun, ward, vals))
    wb.close()
    return rows


def build_join(census_keys, ward_index):
    """Three-stage join. Returns {(district_n, mun_n): (b_district, b_mun)}."""
    bnd = defaultdict(set)
    for w in ward_index:
        bnd[norm(w["district"])].add(strip_type(w["municipality"]))
    bdists = list(bnd)

    cdists = {d for d, _ in census_keys}
    dmap, dfuzzy = {}, 0
    for cd in cdists:
        if cd in bnd:
            dmap[cd] = cd
        else:
            g = difflib.get_close_matches(cd, bdists, n=1, cutoff=0.7)
            if g:
                dmap[cd] = g[0]
                dfuzzy += 1
    print(f"     districts: {len(dmap)}/{len(cdists)} matched "
          f"({dfuzzy} by fuzzy match)")

    cmuns = defaultdict(set)
    for d, m in census_keys:
        cmuns[d].add(m)

    matched, leftover_c, exact, fuzzy = {}, defaultdict(list), 0, 0
    used = defaultdict(set)
    for cd, muns in cmuns.items():
        bd = dmap.get(cd)
        cand = list(bnd.get(bd, []))
        for m in sorted(muns):
            if m in cand:
                matched[(cd, m)] = (bd, m)
                used[bd].add(m)
                exact += 1
            else:
                g = difflib.get_close_matches(m, cand, n=1, cutoff=0.6)
                if g and g[0] not in used[bd]:
                    matched[(cd, m)] = (bd, g[0])
                    used[bd].add(g[0])
                    fuzzy += 1
                else:
                    leftover_c[cd].append(m)

    # Stage 3: elimination within a district.
    elim = []
    for cd, muns in leftover_c.items():
        bd = dmap.get(cd)
        remaining = [x for x in bnd.get(bd, []) if x not in used[bd]]
        if len(muns) == 1 and len(remaining) == 1:
            matched[(cd, muns[0])] = (bd, remaining[0])
            used[bd].add(remaining[0])
            elim.append((cd, muns[0], remaining[0]))

    total = sum(len(v) for v in cmuns.values())
    print(f"     local levels: {exact} exact + {fuzzy} fuzzy + "
          f"{len(elim)} by elimination = {exact+fuzzy+len(elim)}/{total}")
    for cd, cm, bm in elim:
        print(f"       elimination: census '{cm}' -> boundary '{bm}' "
              f"in {cd} (verify: likely a rename)")
    still = [(cd, m) for cd, ms in leftover_c.items() for m in ms
             if (cd, m) not in matched]
    if still:
        print(f"     WARNING: {len(still)} local levels unmatched: {still}")
    return matched


def main():
    print("1/4  downloading census ward tables")
    raw = {}
    for key, fname in TABLES.items():
        raw[key] = []
        for p in PROVINCES:
            path = fetch(p, fname)
            ncols = 9 if key == "population" else len(WALL_COLS)
            raw[key] += parse_sheet(path, ncols)
        print(f"     {key}: {len(raw[key])} ward rows")

    pop_rows = raw["population"]
    if len(pop_rows) != OFFICIAL_WARDS:
        print(f"     NOTE: {len(pop_rows)} ward rows, expected "
              f"{OFFICIAL_WARDS}")

    print("2/4  loading ward index")
    with open(os.path.join(DATA, "wards.json"), encoding="utf-8") as f:
        wj = json.load(f)
    LOCAL_TYPES = {"Gaunpalika", "Nagarpalika",
                   "Mahanagarpalika", "Upamahanagarpalika"}
    ward_index = [{"x": r[0], "y": r[1], "ward": r[2], "municipality": r[3],
                   "district": wj["districts"][r[4]],
                   "province": wj["provinces"][r[5]] if r[5] >= 0 else None,
                   "type": wj["types"][r[6]] if r[6] >= 0 else None}
                  for r in wj["wards"]]
    # National parks and reserves are not local levels and must not compete
    # for a census municipality during the elimination stage.
    ward_index = [w for w in ward_index if w["type"] in LOCAL_TYPES]
    by_key = {}
    for w in ward_index:
        by_key[(norm(w["district"]), strip_type(w["municipality"]),
                w["ward"])] = w

    print("3/4  joining census names to boundary names")
    census_keys = {(norm(d), strip_type(m)) for d, m, _, _ in pop_rows}
    munmap = build_join(census_keys, ward_index)

    print("4/4  assembling per-ward records")
    wall_lookup = {}
    for d, m, ward, vals in raw["wall"]:
        wall_lookup[(norm(d), strip_type(m), ward)] = vals

    out, unlocated, total_pop = [], 0, 0
    for d, m, ward, vals in pop_rows:
        ck = (norm(d), strip_type(m))
        bkey = munmap.get(ck)
        geo = by_key.get((bkey[0], bkey[1], ward)) if bkey else None

        households, pop, male, female = vals[0], vals[1], vals[2], vals[3]
        total_pop += pop

        rec = {
            "district": d, "municipality": m, "ward": ward,
            "households": households, "pop": pop,
            "male": male, "female": female,
        }
        if geo:
            rec["x"], rec["y"] = geo["x"], geo["y"]
            rec["province"] = geo["province"]
        else:
            unlocated += 1

        wv = wall_lookup.get((ck[0], ck[1], ward))
        if wv:
            tot = wv[0] if wv[0] else sum(wv[1:])
            mats = dict(zip(WALL_COLS, wv[1:1 + len(WALL_COLS)]))
            vuln = sum(v for k, v in mats.items() if k in VULNERABLE)
            if tot > 0:
                rec["wall_total"] = tot
                rec["wall_vulnerable"] = vuln
                rec["vuln_pct"] = round(100 * vuln / tot, 1)
        out.append(rec)

    located = len(out) - unlocated
    print(f"     {len(out)} wards, {located} located "
          f"({100*located/len(out):.1f}%)")
    print(f"     population {total_pop:,} vs published "
          f"{PUBLISHED_TOTAL:,} "
          f"({100*total_pop/PUBLISHED_TOTAL:.2f}%)")
    withwall = sum(1 for r in out if "vuln_pct" in r)
    print(f"     {withwall} wards have wall-material data")
    if withwall:
        vp = sorted(r["vuln_pct"] for r in out if "vuln_pct" in r)
        print(f"     vulnerable-wall share: median "
              f"{vp[len(vp)//2]:.1f}%, "
              f"10th pct {vp[len(vp)//10]:.1f}%, "
              f"90th pct {vp[9*len(vp)//10]:.1f}%")

    out.sort(key=lambda r: (r["district"], r["municipality"], r["ward"]))

    # Column-oriented with interned district and municipality strings. The
    # row form is ~1.5 MB, which is too much to send to a phone; this is
    # about a third of that and carries the same information.
    dlist = sorted({r["district"] for r in out})
    mlist = sorted({r["municipality"] for r in out})
    di = {v: i for i, v in enumerate(dlist)}
    mi = {v: i for i, v in enumerate(mlist)}
    rows = [[di[r["district"]], mi[r["municipality"]], r["ward"],
             r["pop"], r["households"],
             r.get("vuln_pct"), r.get("x"), r.get("y")] for r in out]

    payload = {
        "source": {
            "name": "National Population and Housing Census 2021, National "
                    "Statistics Office, Government of Nepal",
            "url": "https://censusresults.nsonepal.gov.np/",
            "tables": list(TABLES.values()),
            "note": "Downloaded from an undocumented /files/ward/P{n}/ path "
                    "discovered in the site's client bundle. May change "
                    "without notice.",
        },
        "reconciliation": {
            "wards": len(out),
            "official_wards": OFFICIAL_WARDS,
            "population": total_pop,
            "published_total": PUBLISHED_TOTAL,
            "shortfall": PUBLISHED_TOTAL - total_pop,
            "note": "Ward rows sum slightly below the published national "
                    "total. The gap is not explained here and is reported "
                    "rather than distributed.",
        },
        "vulnerability": {
            "definition": "Share of households whose outer wall is "
                          "mud-bonded brick/stone, unbaked brick or bamboo.",
            "caveat": "Wall material is a proxy for seismic vulnerability, "
                      "not a structural assessment. It says nothing about "
                      "storeys, age, retrofit or site conditions, and it "
                      "does not predict collapse or casualties.",
        },
        "fields": ["district_i", "municipality_i", "ward", "pop",
                   "households", "vuln_pct", "x", "y"],
        "districts": dlist,
        "municipalities": mlist,
        "wards": rows,
    }
    path = os.path.join(DATA, "ward_census.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"),
                  ensure_ascii=False)
        f.write("\n")
    print(f"  wrote ward_census.json ({os.path.getsize(path)/1024:.0f} KB)")
    print("\nRerun scripts/build_reference.py to rebuild the population grid "
          "from these real ward counts.")


if __name__ == "__main__":
    sys.exit(main())

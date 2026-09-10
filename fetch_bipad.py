#!/usr/bin/env python3
"""
fetch_bipad.py — recorded earthquake impacts in Nepal.

Source: BIPAD Portal, the disaster information platform of the National
Disaster Risk Reduction and Management Authority (NDRRMA).
https://bipadportal.gov.np/

This is the only feed in this project that carries *observed* impact —
deaths, injuries, damaged houses, roads and bridges — recorded by Nepali
authorities rather than modelled. Where BIPAD has a record, it should be
preferred over any estimate this tool produces.

Important limits, stated because they change how the numbers should be read:

  - BIPAD records are entered and verified administratively. Records carry
    an `approved` and `verified` flag, and unverified records are kept but
    labelled, never silently mixed with verified ones.
  - Reporting is not instantaneous. Counts for a recent event will rise for
    days afterwards. A low number can mean low impact or slow reporting, and
    this tool cannot distinguish the two.
  - Incidents are located to a point and a ward, not to a fault or epicentre.
    A BIPAD earthquake incident is a place where damage was recorded, not the
    earthquake's origin.
  - BIPAD incidents are not linked to USGS or NEMRC event IDs. This script
    does not invent that link. Matching in the interface is by date proximity
    only and is presented as a suggestion, not a fact.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))

BASE = "https://bipadportal.gov.np/api/v1/incident/"
HAZARD_EARTHQUAKE = 8          # confirmed against /api/v1/hazard/
PAGE = 200
MAX_PAGES = 40

UA = {"User-Agent": "nepal-quake-watch/0.1 (research; contact via repository)"}

LOSS_FIELDS = {
    "peopleDeathCount": "deaths",
    "peopleMissingCount": "missing",
    "peopleInjuredCount": "injured",
    "peopleAffectedCount": "affected",
    "familyAffectedCount": "families_affected",
    "familyRelocatedCount": "families_relocated",
    "familyEvacuatedCount": "families_evacuated",
    "livestockDestroyedCount": "livestock_lost",
    "infrastructureDestroyedHouseCount": "houses_destroyed",
    "infrastructureAffectedHouseCount": "houses_damaged",
    "infrastructureDestroyedRoadCount": "roads_destroyed",
    "infrastructureAffectedRoadCount": "roads_damaged",
    "infrastructureDestroyedBridgeCount": "bridges_destroyed",
    "infrastructureAffectedBridgeCount": "bridges_damaged",
    "infrastructureDestroyedElectricityCount": "electricity_destroyed",
    "infrastructureEconomicLoss": "infrastructure_loss_npr",
    "agricultureEconomicLoss": "agriculture_loss_npr",
}


def get(url, retries=3, timeout=120):
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
    print("fetching BIPAD earthquake incidents ...", flush=True)
    records, offset = [], 0
    for page in range(MAX_PAGES):
        url = (f"{BASE}?hazard={HAZARD_EARTHQUAKE}&limit={PAGE}"
               f"&offset={offset}&expand=loss&ordering=-incident_on")
        d = get(url)
        results = d.get("results", [])
        if not results:
            break
        records.extend(results)
        offset += PAGE
        time.sleep(0.5)
        if len(results) < PAGE:
            break
        if page and page % 5 == 0:
            print(f"  ... {len(records)} incidents", flush=True)

    print(f"  {len(records)} incidents retrieved")

    incidents, totals = [], {v: 0 for v in LOSS_FIELDS.values()}
    n_verified = 0
    for r in records:
        pt = r.get("point") or {}
        coords = pt.get("coordinates") or [None, None]
        loss = r.get("loss")
        loss = loss if isinstance(loss, dict) else {}

        vals = {}
        for src, dst in LOSS_FIELDS.items():
            v = loss.get(src)
            if isinstance(v, (int, float)) and v:
                vals[dst] = v

        verified = bool(r.get("verified")) and bool(r.get("approved"))
        if verified:
            n_verified += 1
            for k, v in vals.items():
                totals[k] += v

        incidents.append({
            "id": r.get("id"),
            "title": r.get("title"),
            "on": r.get("incidentOn"),
            "lon": coords[0],
            "lat": coords[1],
            "address": r.get("streetAddress") or None,
            "verified": verified,
            "source": r.get("source"),
            "loss": vals or None,
        })

    incidents.sort(key=lambda r: (r["on"] or ""), reverse=True)
    totals = {k: v for k, v in totals.items() if v}

    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "name": "BIPAD Portal, National Disaster Risk Reduction and "
                    "Management Authority (NDRRMA), Government of Nepal",
            "url": "https://bipadportal.gov.np/",
            "hazard_id": HAZARD_EARTHQUAKE,
        },
        "caveats": [
            "Totals below count VERIFIED and APPROVED records only. "
            f"{n_verified} of {len(incidents)} records met that bar.",
            "Reporting lags the event. Recent incidents will be revised "
            "upward. A low count may mean low impact or slow reporting.",
            "Incident points are places where damage was recorded, not "
            "earthquake epicentres.",
            "BIPAD records carry no USGS or NEMRC event identifier. Any "
            "association with a catalogued earthquake is by date only and "
            "is a suggestion, not an established link.",
        ],
        "counts": {"incidents": len(incidents), "verified": n_verified},
        "verified_totals": totals,
        "incidents": incidents,
    }
    path = os.path.join(DATA, "impacts.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print(f"wrote impacts.json ({len(incidents)} incidents, "
          f"{n_verified} verified, {os.path.getsize(path)/1024:.0f} KB)")
    if totals:
        print("  verified totals:", {k: totals[k] for k in list(totals)[:6]})


if __name__ == "__main__":
    sys.exit(main())

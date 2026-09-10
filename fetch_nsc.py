#!/usr/bin/env python3
"""
fetch_nsc.py — Nepal's national earthquake catalogue.

Source: National Earthquake Monitoring and Research Centre (NEMRC, also known
as the National Seismological Centre), Department of Mines and Geology.
https://seismonepal.gov.np/en/earthquakes

Why scrape
----------
NEMRC publishes its records as an HTML table. No public JSON or FDSN endpoint
for this catalogue was found. If one is later published, replace this module —
scraping a government page is a liability, not a design choice.

Two operational notes, both verified rather than assumed:

1. The site sits behind ModSecurity and returns a 406 block page to default
   HTTP client user agents. A browser user agent is required. This is why the
   fetch cannot run in the browser and must run server-side in CI.
2. The table publishes both Bikram Sambat and Gregorian dates, and both local
   (UTC+05:45) and UTC times. The Gregorian date and UTC time are used, so no
   calendar conversion is performed and none is invented.

NEMRC magnitudes are local magnitude (ML) and are computed independently of
USGS. They will not match USGS values for the same event, and this tool does
not reconcile them. Both are shown, attributed to their source.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.path.join(HERE, "..", "data"))

URL = "https://seismonepal.gov.np/en/earthquakes"
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def fetch(url, retries=3):
    last = None
    for a in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml",
            })
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:                        # noqa: BLE001
            last = e
            time.sleep(3 * (a + 1))
    raise last


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()


def parse(html):
    rows = re.findall(r"<tr.*?</tr>", html, re.S)
    out = []
    for r in rows:
        cells = re.findall(r"<t[hd].*?</t[hd]>", r, re.S)
        if len(cells) < 7:
            continue
        date_cell, time_cell = cells[1], cells[2]

        ad = re.search(r"AD:\s*</strong>\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
                       date_cell)
        bs = re.search(r"BS:\s*</strong>\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
                       date_cell)
        utc = re.search(r"UTC:\s*</strong>\s*([0-9]{2}:[0-9]{2}:[0-9]{2})",
                        time_cell)
        local = re.search(r"Local:\s*</strong>\s*([0-9]{2}:[0-9]{2}:[0-9]{2})",
                          time_cell)
        if not (ad and utc):
            # Without an unambiguous Gregorian date and UTC time the row is
            # skipped rather than guessed at.
            continue

        try:
            lat = float(strip_tags(cells[3]))
            lon = float(strip_tags(cells[4]))
        except ValueError:
            continue

        magtxt = strip_tags(cells[5])
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", magtxt)
        if not m:
            continue

        try:
            t = datetime.strptime(f"{ad.group(1)} {utc.group(1)}",
                                  "%Y-%m-%d %H:%M:%S").replace(
                                      tzinfo=timezone.utc)
        except ValueError:
            continue

        out.append({
            "t": int(t.timestamp() * 1000),
            "utc": t.isoformat(timespec="seconds"),
            "bs_date": bs.group(1) if bs else None,
            "local_time": local.group(1) if local else None,
            "lat": lat,
            "lon": lon,
            "mag": float(m.group(1)),
            "magType": "ML",
            "epicentre": strip_tags(cells[6]),
            "source": "NEMRC",
        })
    return out


def main():
    # The listing is paginated at 10 events per page. Page count is read from
    # the pagination links rather than assumed, and capped so a layout change
    # cannot turn this into an unbounded crawl of a government server.
    max_pages = int(os.environ.get("NSC_MAX_PAGES", "60"))

    print("fetching NEMRC catalogue ...", flush=True)
    first = fetch(URL)
    if "Mod_Security" in first or "Not Acceptable" in first:
        raise SystemExit("blocked by ModSecurity; user agent may need updating")

    pages = [int(m) for m in re.findall(r"earthquakes\?page=(\d+)", first)]
    last_page = min(max(pages) if pages else 1, max_pages)
    print(f"  {last_page} pages to fetch (site reports "
          f"{max(pages) if pages else 1})")

    events = parse(first)
    for p in range(2, last_page + 1):
        try:
            events.extend(parse(fetch(f"{URL}?page={p}")))
        except Exception as e:                        # noqa: BLE001
            print(f"  page {p} failed, stopping early: {e}")
            break
        time.sleep(0.7)   # be polite to a government server
        if p % 20 == 0:
            print(f"  ... {p} pages, {len(events)} events", flush=True)

    # The listing can repeat an event across pages if it is edited mid-crawl.
    seen, deduped = set(), []
    for e in events:
        k = (e["t"], round(e["lat"], 2), round(e["lon"], 2))
        if k in seen:
            continue
        seen.add(k)
        deduped.append(e)
    if len(deduped) != len(events):
        print(f"  removed {len(events)-len(deduped)} duplicate rows")
    events = deduped

    print(f"  parsed {len(events)} events")
    if not events:
        # Fail loudly. A silent empty list would look like a quiet period.
        raise SystemExit("parsed zero events — the page layout has probably "
                         "changed. Fix the parser rather than shipping an "
                         "empty feed.")

    events.sort(key=lambda r: -r["t"])
    out = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": {
            "name": "National Earthquake Monitoring and Research Centre "
                    "(National Seismological Centre), Department of Mines "
                    "and Geology, Nepal",
            "url": URL,
            "method": "HTML table scrape; no public API was available",
            "magnitude_scale": "ML (local magnitude), computed by NEMRC "
                               "independently of USGS",
            "pages_fetched": last_page,
        },
        "note": "NEMRC values are not reconciled with USGS values for the "
                "same event. Where both catalogues list an event, the "
                "magnitudes will differ and both are shown as reported.",
        "count": len(events),
        "range": {"earliest": events[-1]["utc"], "latest": events[0]["utc"]},
        "events": events,
    }
    path = os.path.join(DATA, "nsc.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, separators=(",", ":"), ensure_ascii=False)
        f.write("\n")
    print(f"wrote nsc.json ({len(events)} events, "
          f"latest {events[0]['utc']} M{events[0]['mag']} "
          f"{events[0]['epicentre']})")


if __name__ == "__main__":
    sys.exit(main())

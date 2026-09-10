# Nepal Quake Watch

A static, browser-based earthquake monitor for Nepal. It brings together
earthquake catalogues, modelled shaking, census population, health facilities,
government impact records and official contacts, and it is explicit about which
numbers are measured and which are estimated.

Runs entirely on GitHub Pages. No server, no build step, no API keys.

> **This is not an alerting system.** It refreshes on a schedule and may be
> minutes to hours behind. It must not be relied on for warning.

---

## What it shows

| Layer | Source | Measured or estimated |
|---|---|---|
| Earthquake locations and magnitudes | USGS FDSN; NEMRC (Nepal) | Measured |
| Shaking intensity | USGS ShakeMap | Modelled by USGS |
| Population exposure by intensity | 2021 census ward counts × ShakeMap | Observed, grid-allocated |
| Building vulnerability by ward | 2021 census wall material | Observed proxy |
| Recorded deaths, injuries, damage | NDRRMA BIPAD portal | Recorded by government |
| Hospitals and health posts | OpenStreetMap via HOT/HDX | Measured, incomplete |
| Settlements and local levels | OSM; OCHA COD-AB; Nepal National Geoportal | Measured |
| Ward of an epicentre | National Geoportal ward boundaries | Nearest centre |
| Active faults | GEM Global Active Faults | Measured |
| Recorded seismicity since 1900 | USGS catalogue | Measured, biased by coverage |
| Emergency contacts | Official Nepali sources | **Unverified — see below** |

Selecting an event gives origin details, estimated population by shaking band,
most-exposed districts, nearest hospitals, settlements within 25 km, any
matching NDRRMA impact records, and emergency contacts.

---

## Deploy

```bash
git clone https://github.com/<you>/nepal-quake-watch
cd nepal-quake-watch

pip install shapely            # only needed for the reference build

python scripts/build_reference.py   # districts, population grid, hospitals, places
python scripts/build_local_units.py  # optional: official names from Geoportal
python scripts/build_wards.py       # optional: ward index + grid weighting
python scripts/fetch_census_wards.py # ward population + building vulnerability
python scripts/build_geology.py     # faults, local levels
python scripts/build_history.py     # historical seismicity
python scripts/update_events.py     # live events + exposure
python scripts/fetch_nsc.py         # NEMRC catalogue
python scripts/fetch_bipad.py       # NDRRMA impacts
python scripts/validate.py          # refuses to publish bad data

git add . && git commit -m "initial build" && git push
```

Then in the repository: **Settings → Pages → Source: GitHub Actions**.

The three `build_*` scripts are run occasionally by hand. The three fetchers
run every 20 minutes in `.github/workflows/update.yml`, gated on
`validate.py` — if validation fails, nothing is committed.

`build_local_units.py` is optional and needs `pyshp` plus the National
Geoportal shapefile placed in `sources/` (it has no API, so the download is
manual). Its output `data/local_units.json` is committed, so a clone builds
correctly without it.

Only the reference build needs `shapely`. The scripts that run in CI use the
standard library alone, so the workflow has no install step and no lockfile to
go stale.

---

## How exposure is computed

This is the one number the project produces itself, so it is worth being
precise about.

1. Each district's 2023 census population is spread across a 0.05° (~5 km)
   grid in proportion to mapped OSM settlement density.
2. For an event with a USGS ShakeMap, the gridded MMI coverage is sampled at
   each populated cell.
3. Population is summed into intensity bands.

Population comes from the **2021 census at ward level** — each of the 6,743
wards carries its own observed count, assigned to the grid cell containing its
centre. Wards average about 4,300 people, so allocation error at a 5 km cell
is small. This is a measurement allocated to a grid, not an estimate.

An earlier version spread district totals by settlement density. Recomputing
Gorkha 2015 showed that method overstated exposure by roughly 4% at MMI 7 and
6% at MMI 8, once the difference in population base year is allowed for.

Figures remain Nepal-only, so a cross-border event will read low.

Events **without** a ShakeMap get no intensity and no exposure figure at all.
Applying an intensity-prediction equation calibrated in another tectonic
setting and presenting the output alongside measured values would produce a
number that looks authoritative and is not. That trade — a blank field over a
confident guess — is deliberate throughout.

**Validated against the 2015 Gorkha M7.8:** roughly 8.9M people at MMI ≥7 and
16.0M at MMI ≥6 within Nepal, peak MMI 8.6 over populated ground, against
USGS PAGER's grid maximum of 9.0 and a red alert. Right order of magnitude.
Our figures are Nepal-only; PAGER covers India and China too.

---

## Known limitations

Read these before quoting anything from this tool.

- **Emergency numbers are unverified.** They were compiled from public web
  sources, not confirmed against official directories by a person. There is
  also an unresolved conflict over the disaster hotline: NEOC publishes 1149,
  while a July 2026 report citing MoHA says it became 1234. Both are shown.
  **Resolve this before promoting the page.**
- **Hospital distances are straight lines.** Not travel distance, not travel
  time. In Nepal's terrain road distance is frequently several times the
  straight line, and roads are exactly what an earthquake blocks.
- **OSM health coverage is uneven.** Absence from the map is not absence on the
  ground, and presence is no evidence a facility is open or undamaged.
- **The historical layer is not a hazard map.** Catalogue completeness is
  strongly time-varying — 6 recorded events M≥4 in the 1910s against 538 in the
  2010s. That is instrumentation as much as seismicity. Nepal's authoritative
  zonation is NBC 105:2020 and is not reproduced here.
- **USGS and NEMRC magnitudes differ** for the same event and are not
  reconciled. Both are shown, attributed.
- **NDRRMA records are matched to events by date and distance only.** BIPAD
  carries no earthquake identifier. The association is a suggestion.
- **NDRRMA reporting lags.** A low count may mean low impact or slow
  reporting; this tool cannot tell the difference.
- **The boundary layer holds 775 polygons, not 753.** 753 are local levels;
  the other 22 are national parks, wildlife reserves, hunting reserves, a
  watershed reserve and a development area. They are tagged
  `is_local_level: false` and drawn differently, and the build fails if that
  split ever changes. This is confirmed by three independent sources.
- **Ward location is the nearest ward centre, not the containing ward.** Ward
  polygons are not shipped because the geometry would add megabytes to a page
  meant to load on a phone. Confirm with the DEOC for anything official.
- **Two population series are in play.** Exposure uses census 2021 (28.9M);
  district popups use the COD-PS 2023 projection (30.9M). They will not agree.
- **Ward census rows sum 0.82% below the published national total** (28,925,480
  vs 29,164,578). Reported, not distributed.
- **Building vulnerability is a proxy**, based on outer wall material. It says
  nothing about storeys, age, retrofit or site conditions.
- **174 of 775 unit names differ between the National Geoportal and OCHA.**
  The Geoportal's updated official spelling is shown; the OCHA spelling is
  kept in `name_codab` so either can be matched.
- **The population grid inherits OSM's mapping bias**, concentrating people
  where mapping is good.
- **No casualty estimation.** Deliberately.

---

## Repository layout

```
index.html                 the page
assets/app.js              map, list, detail panel
assets/style.css
data/                      all generated JSON, committed
scripts/
  build_reference.py       districts, population grid, hospitals, places
  build_local_units.py     official unit names/types from Geoportal (manual)
  build_wards.py           ward centroid index + population weighting (manual)
  fetch_census_wards.py    2021 census ward population + wall material
  build_geology.py         active faults, local levels
  build_history.py         historical seismicity
  update_events.py         USGS events + exposure          (CI, every 20 min)
  fetch_nsc.py             NEMRC catalogue                 (CI)
  fetch_bipad.py           NDRRMA impacts                  (CI)
  validate.py              publish gate                    (CI)
.github/workflows/
  update.yml               live earthquake refresh, every 20 min
  quarterly.yml            reference refresh, opens a PR (Jan/Apr/Jul/Oct)
  pages.yml                deployment
DATA_SOURCES.md            provenance, licences, what is wrong with each source
```

---

## Attribution

USGS · National Statistics Office of Nepal (2021 census) ·
National Earthquake Monitoring and Research Centre, Department of Mines
and Geology, Nepal · NDRRMA BIPAD Portal · OCHA Common Operational Datasets ·
OpenStreetMap contributors, ODbL, via the Humanitarian OpenStreetMap Team ·
GEM Global Active Faults Database · National Geoportal, Government of Nepal ·
Open Knowledge Nepal LocalBoundaries (CC BY 4.0) ·
Esri, OpenTopoMap, CARTO for basemaps.

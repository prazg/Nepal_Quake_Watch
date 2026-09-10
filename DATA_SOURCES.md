# Data sources

Every layer, where it comes from, what licence it carries, and what is wrong
with it. If you add a source, add it here in the same shape.

---

## Earthquake catalogues

### USGS FDSN event service — primary
- **Endpoint:** `https://earthquake.usgs.gov/fdsnws/event/1/query`
- **Used by:** `scripts/update_events.py`, `scripts/build_history.py`, and the
  browser for a live top-up
- **Verified:** returns `access-control-allow-origin: *`, so the browser can
  call it directly. This was checked, not assumed.
- **Licence:** US Government work, public domain
- **Limits:** USGS locates events globally. For small Nepali events its
  locations and magnitudes can differ noticeably from NEMRC's.

### NEMRC / National Seismological Centre — Nepal's own catalogue
- **Page:** `https://seismonepal.gov.np/en/earthquakes`
- **Used by:** `scripts/fetch_nsc.py`
- **Method:** HTML table scrape. No public API or FDSN endpoint was found.
- **Two verified gotchas:**
  1. The site runs ModSecurity and returns a 406 block page to default HTTP
     client user agents. A browser user agent is required, which is why this
     must run server-side in CI and cannot run in the browser.
  2. The `www.` host returns 503. Use the bare host.
- **Calendar:** the table publishes both Bikram Sambat and Gregorian dates and
  both local and UTC times. The parser uses the Gregorian date and UTC time,
  so no BS→AD conversion is performed or invented.
- **Magnitudes:** local magnitude (ML), computed independently of USGS. They
  will not match. This project does not reconcile them.
- **Fragility:** a layout change breaks the parser. `fetch_nsc.py` exits
  non-zero on zero parsed rows rather than shipping an empty feed, and the
  workflow marks this step `continue-on-error` so it cannot take down the rest.

### India — National Centre for Seismology
- **Status: not integrated.** `riseq.seismo.gov.in` and `seismo.gov.in` are
  both reachable, but no documented public JSON or FDSN endpoint was found.
  Rather than scrape an undocumented internal endpoint that could change
  without notice, this is left out.
- **If you want Indian and regional coverage now:** the EMSC seismic portal
  exposes a working FDSN service at
  `https://www.seismicportal.eu/fdsnws/event/1/query?format=json` and was
  confirmed reachable. It is a straightforward addition modelled on
  `update_events.py`.
- **Note:** the USGS query box already extends beyond Nepal's border
  (79.5–88.8 E, 25.8–31.0 N), so cross-border events that shake Nepal are
  already captured.

---

## Shaking

### USGS ShakeMap
- **Products used:** `download/cont_mmi.json` (contours, drawn on the map) and
  `download/coverage_mmi_*.covjson` (gridded MMI, used for exposure)
- **Discovered via:** the `products.shakemap` block of the event detail GeoJSON
- **Licence:** public domain
- **Limit:** only produced for larger events. Most events have none, and this
  tool then shows no intensity at all rather than modelling one.

### USGS PAGER
- Only `alertlevel` and `maxmmi` are used, read from the event properties.
  PAGER's own exposure and loss estimates are published as XML and PDF; they
  are not parsed here. PAGER covers all affected countries, so its figures
  will exceed this tool's Nepal-only estimates.

---

## Population

### Nepal Subnational Population Statistics (COD-PS)
- **HDX dataset:** `cod-ps-npl`, 2023 release
- **Used:** district (ADM2) totals plus under-5 and 65+ age bands
- **Total:** 30,899,443 across 77 districts
- **Limit:** these are census-based projections, not a live count.

### National Statistics Office — 2021 census, ward level — PRIMARY POPULATION

- **Portal:** `https://censusresults.nsonepal.gov.np/`
- **Fetched by:** `scripts/fetch_census_wards.py` → `data/ward_census.json`
- **Tables used:** `WRD_Indv01-HouseholdAndPopulation.xlsx` (population and
  households by ward) and `WRD_Hhld03-OuterWallOfHouse.xlsx` (outer wall
  material by ward)
- **Result:** all **6,743 wards**, **28,925,480 people**, every ward located

**This replaced the weakest number in the project.** Exposure was previously a
dasymetric estimate spreading district totals across a grid. It is now
observed ward counts — roughly 4,300 people per unit instead of 400,000.
Recomputing the 2015 Gorkha event showed the old estimate overstated exposure
by about 4% at MMI 7 and 6% at MMI 8, after allowing for the difference in
population base year.

**Download path is undocumented.** The portal is a Next.js app with no API.
The file list is embedded in the client bundle and files sit at
`/files/ward/P{1..7}/{name}.xlsx`, where P1..P7 are Koshi, Madhesh, Bagmati,
Gandaki, Lumbini, Karnali, Sudurpaschim. Discovered by reading the bundle. It
may change without notice, so the script verifies its own output and fails
loudly.

**48 ward-level tables are available**, not just the two used here — literacy,
disability, migration, wealth quintile, cooking fuel, drinking water, female
property ownership, house quality. Adding one is a matter of listing it in
`TABLES`.

#### The join

Census sheets carry names, not pcodes, with different transliterations from
the boundary file (Panchdebalbinayak/Panchadewalbinayak,
Bhumikasthan/Bhumekasthan, Turmakhand/Turmakhad) and a `Metropolitian` typo.
Three stages, most reliable first:

| Stage | Result |
|---|---|
| District, exact then fuzzy | 77 / 77 (7 fuzzy) |
| Local level within district, exact then fuzzy | 751 / 753 |
| Elimination — one left on each side in a district | 753 / 753 |

Elimination resolved two genuine renames: census `Gangadev` ↔ boundary
`Sukidaha` in Rolpa, and census `Mahakali` ↔ boundary `Dodhara Chandani` in
Kanchanpur. That is a structural argument, not a claim about the names, and
both are printed by the script so they can be checked.

#### Two things to know about the numbers

- **Ward rows sum to 28,925,480 against the published 29,164,578**, a
  shortfall of 239,098 (0.82%). Not explained here; reported rather than
  distributed across wards.
- **Two population series are now in play.** Exposure uses census 2021
  (28.9M). District popups use the COD-PS 2023 projection (30.9M). They are
  different series and will not agree. Each is labelled where it appears.

#### Building vulnerability

Outer wall material is the strongest seismic-vulnerability proxy available at
ward level. Households with mud-bonded brick or stone, unbaked brick, or
bamboo walls are counted as vulnerable.

National picture: median ward **61.7%** vulnerable, 10th percentile 12.5%,
90th percentile **97.3%**. That spread is the point — it separates a
Kathmandu ward from a hill ward in Achham.

**It is a proxy, not a structural assessment.** It says nothing about storeys,
age, retrofit or site conditions, and does not predict collapse or casualties.

### Superseded: dasymetric population grid
- **Built by:** `scripts/build_reference.py` → `data/popgrid.json`
- **Method:** each district's census total is distributed across 0.05°
  (~5 km) cells in proportion to mapped OpenStreetMap settlement density,
  weighted city 40 / town 12 / suburb 6 / village 3 / hamlet 1, with a 0.35
  floor so that inhabited but unmapped cells are not zeroed.
- **This is an estimate.** It is validated only in that the grid total
  reconciles to the census total within 0.01%. That check confirms nothing was
  lost; it does not confirm the spatial distribution is right.
- **Superseded.** This method is now a fallback, used only if
  `ward_census.json` is absent. Real ward counts are used instead.
- **Ward blending.** Since ward boundaries became available, the distribution
  blends two signals: OSM settlement density, and ward density (count of ward
  centres per cell plus a bounded inverse-area term). Wards are delineated
  administratively with population in mind, so they carry a signal that does
  not depend on OSM coverage. This reduces, but does not remove, the bias
  below.
- **Known bias:** it still partly inherits OSM's mapping bias. Well-mapped
  areas attract more of a district's population than they should.
- **How much the method moves the answer.** Adding the ward signal changed the
  2015 Gorkha exposure by under 1% at MMI 4-6 and by about 5-7% at MMI 7-8.
  Treat that as the honest precision of these figures: the choice of weighting
  scheme alone moves the high-intensity bands by several percent.

---

## Boundaries

### OCHA Common Operational Datasets (COD-AB) — used
- **HDX dataset:** `cod-ab-npl`
- **Why this one:** it carries pcodes, so the population join is on a stable
  identifier rather than on a district name.
- **Licence:** see the HDX dataset page; source is the Survey Department of
  Nepal via OCHA.

### geoBoundaries gbOpen — evaluated and rejected
Used in an early version and abandoned. The `NPL/ADM2` layer:
- ships **75 polygons for Nepal's 77 districts**;
- **duplicates** the names `Saptari` and `Bara`;
- **omits** Siraha, Parsa, Rupandehi and Dailekh entirely;
- labels the two halves of the former Nawalparasi district the **opposite way
  round** from what the names imply — its `Nawalparasi` borders Chitwan and
  Tanahun (so it is Nawalparasi **East**) while its `Nawalapur` borders
  Kapilbastu and Arghakhanchi (so it is **West**). Confirmed by polygon
  adjacency.

Joining these on name silently lost 9.6% of Nepal's population. It is recorded
here because the failure was quiet, and anyone reaching for geoBoundaries for
Nepal should know.

### Open Knowledge Nepal — LocalBoundaries — fallback
- `https://localboundries.oknp.org/download/` · repo `openknowledgenp/localboundaries`
- **Licence:** CC BY 4.0
- **Fetched by:** `scripts/build_geology.py` from
  `/data/local-level/nepal.geojson`, **only when `data/local_units.json` is
  absent**. The National Geoportal above is preferred: same 753/22 split, but
  current official spellings. LocalBoundaries remains the automated fallback
  because it can be fetched without a manual download.
- **Levels offered:** province, district, local level. **No ward level.**
  Its district layer is derived by dissolving local levels, since no
  separately-attributed district source exists.
- **What it contributes:** the `Type_GN` field — the unit type. COD-AB carries
  pcodes but not type; LocalBoundaries carries type but not pcodes. Joining
  them gives both.
- **Join method: geometric, not by name.** Matching on district plus name
  reaches only 69% because the two sources transliterate differently
  (Phaktanlung / Phaktanglung, and so on). Centroid containment gives 100%.

#### This resolves the 775-vs-753 discrepancy

Both sources give 775 polygons where Nepal's local-level count is usually
cited as 753. The type field explains it exactly:

| Type | Count |
|---|---|
| Gaunpalika (rural municipality) | 460 |
| Nagarpalika (municipality) | 276 |
| Upamahanagarpalika (sub-metropolitan) | 11 |
| Mahanagarpalika (metropolitan) | 6 |
| **Local levels** | **753** |
| National Park | 11 |
| Wildlife Reserve | 6 |
| Hunting Reserve | 3 |
| Watershed and Wildlife Reserve | 1 |
| Development Area | 1 |
| **Protected areas and other** | **22** |
| **Total polygons** | **775** |

The 22 are not local levels and carry little or no resident population.
They are tagged `is_local_level: false`, drawn differently on the map, and
`scripts/validate.py` fails the build if the 753 count ever changes.

### SaugatPdl/nepal-administrative-boundary-shapefiles
- Reachable. Advertises ward-level shapefiles, which **neither** COD-AB nor
  LocalBoundaries provides — LocalBoundaries stops at local level.
- **Not integrated.** Nothing in this tool currently works at ward level, and
  no licence was confirmed from the repository. This is the source to reach
  for if ward-level exposure is ever needed; check the licence first.

### Nepal National Geoportal — used, manual download
- `https://nationalgeoportal.gov.np/`
- **Layer:** "Administrative Boundary - Local Level with Province Names"
  (`local_unit_nameupdated_wgs.shp`), EPSG:4326, 777 polygons / 775 units
- **Processed by:** `scripts/build_local_units.py` → `data/local_units.json`
- **Encoding:** the `.cst` sidecar declares ISO-8859-1. Pass it explicitly or
  names with diacritics come back as mojibake.

**Not automatable.** The Geoportal is a JavaScript viewer; no ArcGIS REST or
OGC service root is discoverable (`/arcgis/rest/services`,
`/server/rest/services`, `/api/` all return the application's HTML). The
shapefile must be downloaded by hand into `sources/`. Because of that, the
derived lookup `data/local_units.json` (106 KB) **is committed**, so a clone
builds correctly without the 18 MB shapefile. `sources/` is gitignored.

**What it contributes:**

1. **Official current spellings.** The layer is explicitly "name updated" and
   differs from COD-AB on **174 of 775** unit names — Phaktanglung not
   Phaktanlung, Janakpurdham not Janakpur, Shivasataxi not Shivasatakshi,
   Barahkshetra not Barahachhetra. The Geoportal spelling is used for display;
   the COD-AB spelling is retained as `name_codab` so existing joins and
   searches still work.
2. **Unit type**, which COD-AB does not carry.
3. **Named provinces** (Koshi, Madhesh, Bagmati, Gandaki, Lumbini, Karnali,
   Sudurpashchim).

**What it does not replace.** It has no pcodes, and pcodes are what the
population join depends on. Geometry and pcodes stay with COD-AB; names and
types come from here. The two are joined by centroid containment: **775 of 775
matched**. Name matching was not used — it reaches only about 69% because of
exactly the transliteration differences above.

### National Geoportal — ward boundaries

- **Layer:** "Administrative Boundary - Wards" (`ward_nameupdated_wgs.shp`),
  EPSG:4326, ISO-8859-1, **6,792 polygons**
- **Processed by:** `scripts/build_wards.py` → `data/wards.json` (320 KB) and
  `data/ward_weights.json`
- **Manual download**, same reason as the local-level layer.

**Polygons are deliberately not published.** Simplified ward geometry costs
2.5 MB at a coarse tolerance and 8.9 MB at a usable one. This page is meant to
open on a phone on a congested network after an earthquake; several megabytes
of boundary geometry for a naming convenience is the wrong trade. What ships
is a centroid index, and the interface reports the **nearest ward centre**,
which is not the same as the containing ward and is labelled as such.

**Used for:**
1. Naming an event's administrative location — ward, local level, district,
   province. This is the level DEOCs and NDRRMA actually work at.
2. Weighting the population grid (see below).

**Ward population** was initially believed unavailable — COD-PS stops at
district, the 2011 census is by VDC, and HDX's `population-by-ward-adm3` is
Tanzania, not Nepal. It was subsequently found on the NSO census portal; see
that entry above. This layer supplies ward geometry and the census supplies
ward population, joined by name.

**Two upstream data issues, both handled explicitly:**
- `type_gn` contains the typo `Wieldlife Reserve` on one polygon. Correcting it
  brings the Wildlife Reserve count to 6, exactly matching the local-level
  layer — which is what confirms it is a typo and not a separate category.
- **Resolved:** the layer yields **6,760** distinct (municipality, ward) pairs
  against the official **6,743**. The 2021 census independently lists exactly
  6,743 and every one joined to this layer, so the surplus is an artefact of
  this file — multipart polygons and duplicate codes — not a disagreement
  about which wards exist. Take ward totals from the census, not from polygon
  counts. Individual ward identification is sound.

**Third independent confirmation of the 753/22 split.** This authoritative
government source gives precisely the same breakdown already obtained from
LocalBoundaries: 460 Gaunpalika + 276 Nagarpalika + 11 Upamahanagarpalika +
6 Mahanagarpalika = 753 local levels, plus 22 protected areas and other units.

---

## Health facilities and settlements

### OpenStreetMap via HOT exports on HDX
- `hotosm_npl_health_facilities` — 7,283 features, of which 1,679 are tagged
  hospital (tier 1) and 2,814 clinic / health post (tier 2)
- `hotosm_npl_populated_places` — 103,621 features, of which 16,253 are
  city / town / village / suburb / hamlet
- **Licence:** ODbL. Attribution is required and is in the map credits.
- **Limits that matter operationally:** coverage is uneven and better in urban
  areas; a facility's presence says nothing about whether it is open, staffed,
  supplied or undamaged; capacity tags are largely absent.

---

## Recorded impacts

### BIPAD Portal — NDRRMA
- **Endpoint:** `https://bipadportal.gov.np/api/v1/incident/?hazard=8&expand=loss`
- Hazard id 8 = Earthquake, confirmed against `/api/v1/hazard/`
- **Retrieved:** 333 earthquake incidents, all flagged verified and approved,
  totalling 9,131 recorded deaths (dominated by the 2015 Gorkha earthquake)
- **Fields used:** deaths, missing, injured, affected people and families,
  houses / roads / bridges / electricity destroyed and damaged, livestock,
  and economic loss
- **Limits:**
  - Reporting lags. Recent counts will rise.
  - Incident points are **places where damage was recorded**, not epicentres.
  - Records carry **no USGS or NEMRC event id**. The interface matches by
    date (±3 days) and distance (<150 km) and labels that as a suggestion,
    not an established link.

---

## Geology

### GEM Global Active Faults Database
- `https://github.com/GEMScienceTools/gem-global-active-faults`
- 58 of 13,696 global fault traces intersect the Nepal region box
- Attributes kept: name, slip type, net slip rate, average dip
- **What it is not:** a hazard map. A fault trace tells you where a structure
  is, not the probability of ground motion at a site. The Main Himalayan
  Thrust underlies most of Nepal, so distance to a mapped surface trace is a
  weak predictor of shaking on its own.

### Seismic hazard zonation — deliberately absent
Nepal's authoritative design zonation is the seismic zoning factor in the
national building code **NBC 105:2020**. No verified machine-readable copy was
located, so it is **not included and not approximated**. The historical layer
in this tool shows *recorded seismicity*, which is a different thing and is
labelled as such in the interface.

---

## Basemaps

| Layer | Source | Note |
|---|---|---|
| Satellite imagery | Esri World Imagery | Check Esri's terms for your use |
| Terrain | OpenTopoMap | CC BY-SA, OSM data |
| Plain dark | CARTO dark_all | OSM data, CARTO styling |

---

## Emergency contacts

See `data/contacts.json`. Every entry carries a `source` and a `confidence`.

**Currently flagged UNVERIFIED.** The numbers were compiled from public web
sources and have not been confirmed against official directories by a person.
`scripts/validate.py` emits a warning until `verification_status` is changed.

**One unresolved conflict.** NEOC's own site publishes **1149** as the
toll-free disaster hotline. A July 2026 news report citing a Ministry of Home
Affairs press release states 1149 was replaced by **1234**, routing Kathmandu
Valley calls to NEOC and calls from the other 74 districts to the respective
DEOC. Both cannot be current. The interface shows both and tells the user to
fall back to 100 or 112. **Resolve this with MoHA or NEOC directly before
promoting this page.**

District-level DEOC numbers are deliberately not reproduced. No machine-readable
national directory was found, and printing digits that may be wrong when
someone needs them is worse than linking to the official directory.

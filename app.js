/* Nepal Quake Watch
 *
 * Loading strategy: every dataset is loaded from a same-origin file in
 * data/, written by the CI pipeline. The page then tries USGS directly for
 * anything newer than the cached file. If that live call fails, the cached
 * data still renders and the header says so rather than showing an empty map.
 *
 * Nothing in this file estimates shaking. Intensity values come from USGS
 * ShakeMap; exposure is computed in the pipeline by sampling that ShakeMap
 * against a population grid. Events with no ShakeMap show location and
 * magnitude only, and say so.
 */

const NEPAL = [28.3, 84.1];
const D = {};                      // loaded datasets
let map, layers = {}, evLayer, selected = null;
let catalogue = 'usgs', magMin = 2.5;

/* ------------------------------------------------------------- utilities */

const MMI_COLOR = {
  1:'#FFFFFF', 2:'#BFCCFF', 3:'#A0E5FF', 4:'#7DF9F9', 5:'#7EFA8D',
  6:'#FFF400', 7:'#FFC900', 8:'#FF9100', 9:'#FF0000', 10:'#C80000'
};
const MMI_WORD = {
  1:'Not felt', 2:'Weak', 3:'Weak', 4:'Light', 5:'Moderate',
  6:'Strong', 7:'Very strong', 8:'Severe', 9:'Violent', 10:'Extreme'
};

function mmiColor(v){ return MMI_COLOR[Math.min(10, Math.max(1, Math.round(v)))]; }

/** Great-circle distance in km. */
function haversine(lat1, lon1, lat2, lon2){
  const R = 6371, r = Math.PI/180;
  const dLat = (lat2-lat1)*r, dLon = (lon2-lon1)*r;
  const a = Math.sin(dLat/2)**2 +
            Math.cos(lat1*r)*Math.cos(lat2*r)*Math.sin(dLon/2)**2;
  return 2*R*Math.asin(Math.sqrt(a));
}

const fmt = n => n == null ? '—' : n.toLocaleString('en-GB');
const km  = d => d < 10 ? d.toFixed(1)+' km' : Math.round(d)+' km';

function ago(ms){
  const s = (Date.now()-ms)/1000;
  if (s < 3600)   return Math.round(s/60)+' min ago';
  if (s < 86400)  return Math.round(s/3600)+' h ago';
  if (s < 2592000)return Math.round(s/86400)+' d ago';
  return new Date(ms).toISOString().slice(0,10);
}

function whenText(ms){
  const d = new Date(ms);
  return d.toISOString().replace('T',' ').slice(0,16)+' UTC';
}

function el(tag, cls, html){
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}
const esc = s => String(s ?? '').replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function loadJSON(path){
  const r = await fetch(path, {cache:'no-cache'});
  if (!r.ok) throw new Error(path+' → '+r.status);
  return r.json();
}

/* ------------------------------------------------------------------ boot */

async function boot(){
  map = L.map('map', {zoomControl:true, minZoom:5, maxZoom:14})
        .setView(NEPAL, 7);

  const imagery = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:17, attribution:'Imagery: Esri, Maxar, Earthstar Geographics'});
  const terrain = L.tileLayer(
    'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    {maxZoom:16, attribution:'© OpenTopoMap, © OpenStreetMap contributors'});
  const plain = L.tileLayer(
    'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    {maxZoom:18, attribution:'© OpenStreetMap contributors, © CARTO'});
  plain.addTo(map);

  // Core data first so the page is usable quickly; context layers after.
  const [live, refMeta] = await Promise.all([
    loadJSON('live.json').catch(()=>null),
    loadJSON('reference_meta.json').catch(()=>null)
  ]);
  D.live = live; D.refMeta = refMeta;

  evLayer = L.layerGroup().addTo(map);
  // One control, created up front so basemaps are switchable immediately.
  // Context overlays are added to it as they finish loading.
  layers.control = L.control.layers(
    {'Satellite imagery':imagery, 'Terrain':terrain, 'Plain dark':plain},
    {}, {position:'topright', collapsed:true}
  ).addTo(map);

  if (!live){
    document.getElementById('railnote').textContent =
      'Could not load data/live.json. The site may not have been built yet.';
  } else {
    render();
    refreshFromUSGS();                       // try for something newer
  }

  // Context layers load in the background and are added to the control.
  loadContext();
  wireUI();
  addLegend();
}

async function loadContext(){
  const jobs = {
    districts:      loadJSON('districts.json').catch(()=>null),
    faults:         loadJSON('faults.json').catch(()=>null),
    hospitals:      loadJSON('hospitals.json').catch(()=>null),
    places:         loadJSON('places.json').catch(()=>null),
    history:        loadJSON('history.json').catch(()=>null),
    impacts:        loadJSON('impacts.json').catch(()=>null),
    contacts:       loadJSON('contacts.json').catch(()=>null),
    wards:          loadJSON('wards.json').catch(()=>null),
    census:         loadJSON('ward_census.json').catch(()=>null),
    nsc:            loadJSON('nsc.json').catch(()=>null),
    municipalities: loadJSON('municipalities.json').catch(()=>null)
  };
  for (const [k,p] of Object.entries(jobs)) D[k] = await p;

  const add = (label, layer) => layers.control.addOverlay(layer, label);

  if (D.districts){
    layers.districts = L.geoJSON(D.districts, {
      style:{color:'#3E5666', weight:.8, fill:true, fillColor:'#1B2A34', fillOpacity:.18},
      onEachFeature:(f,l)=>{
        const p = f.properties;
        l.bindPopup(`<b>${esc(p.name)}</b><br>${esc(p.province||'')} province`+
          `<br>Population ${fmt(p.pop)} (2023 projection)`+
          `<br>Under 5: ${fmt(p.u5)} · 65+: ${fmt(p.o65)}`);
      }
    });
    add('Districts', layers.districts);
    layers.districts.addTo(map);
  }

  if (D.faults){
    layers.faults = L.geoJSON(D.faults, {
      style:{color:'#C97A4A', weight:1.4, opacity:.85, dashArray:'5,3'},
      onEachFeature:(f,l)=>{
        const p = f.properties;
        l.bindPopup(`<b>${esc(p.name||'Unnamed fault')}</b>`+
          `<br>Slip type: ${esc(p.slip_type||'not recorded')}`+
          `<br>Net slip rate: ${esc(p.slip_rate||'not recorded')}`+
          `<br><span style="color:#93A4B2">Mapped active fault trace. `+
          `Geological context, not a hazard rating.</span>`);
      }
    });
    add('Active faults', layers.faults);
  }

  if (D.history){
    const cells = D.history.cells.map(([x,y,n,maxm,e]) =>
      L.circleMarker([y,x], {
        radius: 2 + Math.min(9, n*0.35),
        color:'#7A6FA8', weight:0, fillColor:'#7A6FA8',
        fillOpacity: Math.min(.55, .12 + n*0.02)
      }).bindPopup(`<b>Recorded seismicity</b><br>${n} events M≥4 since 1900`+
        `<br>Largest recorded: M${maxm}`+
        `<br><span style="color:#93A4B2">Observed catalogue only. Coverage `+
        `improved greatly after 2015, so this maps instrumentation as well `+
        `as seismicity.</span>`));
    layers.history = L.layerGroup(cells);
    add('Recorded seismicity since 1900', layers.history);
  }

  if (D.hospitals){
    const m = D.hospitals.facilities.filter(h=>h.t===1).map(h =>
      L.circleMarker([h.y,h.x], {
        radius:3.2, color:'#E8ECEF', weight:1, fillColor:'#4FD1A5', fillOpacity:.9
      }).bindPopup(`<b>${esc(h.n)}</b><br>Hospital`+
        (h.d?`<br>${esc(h.d)} district`:'')+
        (h.s?`<br>${esc(h.s)}`:'')+
        `<br><span style="color:#93A4B2">OpenStreetMap. Presence is not a `+
        `guarantee of current capability.</span>`));
    layers.hospitals = L.layerGroup(m);
    add('Hospitals', layers.hospitals);
  }

  if (D.impacts){
    const m = D.impacts.incidents
      .filter(i => i.lat && i.lon && i.loss)
      .map(i => {
        const l = i.loss;
        const sev = (l.deaths||0)*3 + (l.injured||0);
        return L.circleMarker([i.lat,i.lon], {
          radius: 3 + Math.min(10, Math.sqrt(sev)),
          color:'#E8654B', weight:1, fillColor:'#E8654B', fillOpacity:.35
        }).bindPopup(`<b>${esc(i.title||'Recorded impact')}</b>`+
          `<br>${esc((i.on||'').slice(0,10))}`+
          Object.entries(l).map(([k,v]) =>
            `<br>${esc(k.replace(/_/g,' '))}: ${fmt(v)}`).join('')+
          `<br><span style="color:#93A4B2">NDRRMA BIPAD record. Reporting `+
          `lags the event.</span>`);
      });
    layers.impacts = L.layerGroup(m);
    add('Recorded impacts (NDRRMA)', layers.impacts);
  }

  if (D.municipalities){
    layers.municipalities = L.geoJSON(D.municipalities, {
      // Protected areas are drawn differently: they are not local levels and
      // carry little or no resident population.
      style: f => f.properties.is_local_level
        ? {color:'#2E4250', weight:.5, fill:false}
        : {color:'#3B5E3B', weight:.5, fill:true, fillColor:'#1B2A1B', fillOpacity:.25},
      onEachFeature:(f,l)=>{
        const p = f.properties;
        l.bindPopup(`<b>${esc(p.name)}</b>`+
          (p.name_codab ? `<br><span style="color:#6B7C8A">also written `+
            `${esc(p.name_codab)}</span>` : '')+
          `<br>${esc(p.unit_type||'type not recorded')}`+
          `<br>${esc(p.district)} district, ${esc(p.province||'')}`+
          (p.is_local_level === false
            ? `<br><span style="color:#93A4B2">Protected area, not a local `+
              `level. Little or no resident population.</span>` : ''));
      }
    });
    add('Local levels and protected areas', layers.municipalities);
  }

  if (D.nsc) document.querySelector('[data-cat="nemrc"]').disabled = false;
  buildAbout();
}

/* ------------------------------------------------------ live USGS refresh */

async function refreshFromUSGS(){
  const q = D.live.query;
  const url = 'https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson'
    + '&orderby=time&minmagnitude=' + q.min_magnitude
    + '&minlatitude=' + q.bbox.minlat + '&maxlatitude=' + q.bbox.maxlat
    + '&minlongitude=' + q.bbox.minlon + '&maxlongitude=' + q.bbox.maxlon
    + '&starttime=' + new Date(Date.now()-30*864e5).toISOString().slice(0,10);
  try{
    const fc = await (await fetch(url)).json();
    const known = new Set(D.live.events.map(e=>e.id));
    let added = 0;
    for (const ft of fc.features){
      if (known.has(ft.id)) continue;
      const p = ft.properties, g = ft.geometry;
      D.live.events.push({
        id:ft.id, t:p.time, mag:p.mag, magType:p.magType,
        depth:g.coordinates[2], lon:g.coordinates[0], lat:g.coordinates[1],
        place:p.place, url:p.url, felt:p.felt, cdi:p.cdi, mmi:p.mmi,
        alert:p.alert, sig:p.sig, status:p.status,
        in_nepal:null, shakemap:null, exposure:null, _fresh:true
      });
      added++;
    }
    D.live.events.sort((a,b)=>b.t-a.t);
    setFreshness(true, added);
    render();
  }catch(e){
    setFreshness(false, 0);
  }
}

function setFreshness(liveOk, added){
  const n = document.getElementById('freshness');
  const built = D.live?.generated ? new Date(D.live.generated).getTime() : null;
  if (liveOk){
    n.className = 'live';
    n.textContent = added
      ? `Live · ${added} event${added>1?'s':''} newer than last build`
      : `Live · checked just now`;
  } else {
    n.className = 'stale';
    n.textContent = built
      ? `Cached · built ${ago(built)} (live check failed)`
      : 'Cached data';
  }
}

/* ----------------------------------------------------------- list & map */

function currentEvents(){
  if (catalogue === 'nemrc'){
    return (D.nsc?.events || [])
      .filter(e => e.mag >= magMin)
      .map(e => ({...e, id:'nemrc-'+e.t, place:e.epicentre, _nemrc:true}));
  }
  return (D.live?.events || []).filter(e => (e.mag ?? 0) >= magMin);
}

function render(){
  const evs = currentEvents();
  const list = document.getElementById('events');
  list.innerHTML = '';
  evLayer.clearLayers();

  const note = document.getElementById('railnote');
  if (catalogue === 'nemrc'){
    note.textContent = `${evs.length} events from Nepal's NEMRC catalogue `
      + `(ML). Magnitudes are NEMRC's own and differ from USGS.`;
  } else {
    const withEx = evs.filter(e=>e.exposure).length;
    note.textContent = `${evs.length} events from USGS. `
      + `${withEx} have a ShakeMap and an exposure estimate.`;
  }

  for (const e of evs.slice(0, 400)){
    // list row
    const li = el('li','ev');
    li.tabIndex = 0;
    li.setAttribute('role','button');
    const tags = [];
    if (e.shakemap) tags.push('<span class="tag sm">ShakeMap</span>');
    if (e.alert)   tags.push(`<span class="tag al-${esc(e.alert)}">PAGER ${esc(e.alert)}</span>`);
    if (e._nemrc)  tags.push('<span class="tag">NEMRC ML</span>');
    li.innerHTML =
      `<div><span class="ev-m">${(e.mag??0).toFixed(1)}</span>`+
      `<span class="ev-t">${ago(e.t)}</span></div>`+
      `<div><div class="ev-p">${esc(e.place||'Unnamed location')}</div>`+
      `<div class="ev-s">${Math.round(e.depth??0)} km deep · ${whenText(e.t)}</div>`+
      (tags.length?`<div class="tagrow">${tags.join('')}</div>`:'')+
      `</div>`;
    li.addEventListener('click', ()=>select(e));
    li.addEventListener('keydown', ev=>{
      if (ev.key==='Enter'||ev.key===' '){ ev.preventDefault(); select(e); }
    });
    li.dataset.id = e.id;
    list.appendChild(li);

    // map marker: radius by magnitude, colour by observed intensity if known
    const shade = e.mmi ? mmiColor(e.mmi) : (e.mag>=6?'#FF9100':e.mag>=5?'#FFC900':'#7EFA8D');
    const mk = L.circleMarker([e.lat,e.lon], {
      radius: 3 + Math.pow(Math.max(0,(e.mag??0)-2), 1.7),
      color:'#0F1519', weight:1, fillColor:shade, fillOpacity:.82
    }).on('click', ()=>select(e));
    mk.bindTooltip(`M${(e.mag??0).toFixed(1)} · ${esc(e.place||'')}`, {direction:'top'});
    evLayer.addLayer(mk);
  }
}

function select(e){
  selected = e;
  document.querySelectorAll('.ev').forEach(n =>
    n.setAttribute('aria-current', n.dataset.id===e.id ? 'true':'false'));
  map.setView([e.lat, e.lon], Math.max(map.getZoom(), 8));
  drawDetail(e);
  drawShakemap(e);
}

/* --------------------------------------------------------- shakemap draw */

async function drawShakemap(e){
  if (layers.contours){ map.removeLayer(layers.contours); layers.contours = null; }
  if (!e.shakemap?.contour_url) return;
  try{
    const gj = await (await fetch(e.shakemap.contour_url)).json();
    layers.contours = L.geoJSON(gj, {
      style: f => ({
        color: f.properties.color || mmiColor(f.properties.value),
        weight: 2, opacity:.9
      }),
      onEachFeature:(f,l)=> l.bindTooltip(
        `MMI ${f.properties.value} — ${MMI_WORD[Math.round(f.properties.value)]||''}`,
        {sticky:true})
    }).addTo(map);
  }catch(err){ /* contour overlay is optional; the panel still works */ }
}

/* ------------------------------------------------------------ detail panel */

function drawDetail(e){
  const p = document.getElementById('panel');
  p.hidden = false;
  p.innerHTML = '';

  // --- header
  const h = el('div','p-head');
  h.innerHTML =
    `<button class="p-close" type="button">Close</button>`+
    `<div class="p-mag">M ${(e.mag??0).toFixed(1)}<small>${esc(e.magType||'')}</small></div>`+
    `<p class="p-place">${esc(e.place||'Unnamed location')}</p>`+
    `<p class="p-time">${whenText(e.t)} · ${ago(e.t)}</p>`;
  h.querySelector('.p-close').addEventListener('click', ()=>{
    p.hidden = true;
    if (layers.contours){ map.removeLayer(layers.contours); layers.contours=null; }
  });
  p.appendChild(h);

  // --- basics
  const b = el('div','sec');
  b.innerHTML = `<h3>Origin</h3>
    <dl class="kv">
      <dt>Depth</dt><dd>${e.depth!=null?Math.round(e.depth)+' km':'—'}</dd>
      <dt>Coordinates</dt><dd>${e.lat.toFixed(3)}, ${e.lon.toFixed(3)}</dd>
      <dt>Catalogue</dt><dd>${e._nemrc?'NEMRC (Nepal)':'USGS'}</dd>
      ${e.status?`<dt>Review status</dt><dd>${esc(e.status)}</dd>`:''}
      ${e.felt?`<dt>Felt reports</dt><dd>${fmt(e.felt)}</dd>`:''}
    </dl>`+
    (e.url?`<p><a href="${esc(e.url)}" target="_blank" rel="noopener">Open the USGS event page</a></p>`:'');
  p.appendChild(b);

  // --- where this is, administratively
  p.appendChild(wardSection(e));

  // --- shaking and exposure
  p.appendChild(exposureSection(e));

  // --- nearest hospitals
  p.appendChild(nearestSection(e));

  // --- settlements
  p.appendChild(placesSection(e));

  // --- recorded impacts nearby
  p.appendChild(impactSection(e));

  // --- contacts
  p.appendChild(contactSection());
}

function exposureSection(e){
  const s = el('div','sec');
  if (!e.exposure){
    s.innerHTML = `<h3>Shaking and exposure</h3>
      <p>No USGS ShakeMap has been produced for this event, so there is no
      modelled shaking to report and no exposure estimate.</p>
      <p class="note"><strong>Why nothing is shown.</strong> Estimating
      intensity here would mean applying a ground-motion relation calibrated
      in another region and presenting the output as if it were measured.
      That number would look authoritative and would not be. Location, depth
      and magnitude above are what is actually known.</p>`;
    return s;
  }
  const ex = e.exposure;
  const bands = Object.entries(ex.bands).map(([k,v])=>[+k,v]).sort((a,b)=>a[0]-b[0]);
  const max = Math.max(...bands.map(b=>b[1]), 1);

  s.innerHTML = `<h3>Estimated population by shaking intensity</h3>`;
  const wrap = el('div','bars');
  for (const [mmi, pop] of bands){
    const row = el('div','brow');
    row.innerHTML =
      `<span class="blab">MMI ${mmi}+</span>`+
      `<span class="btrack"><span class="bfill" style="width:${(pop/max*100).toFixed(1)}%;background:${mmiColor(mmi)}"></span></span>`+
      `<span class="bval">${fmt(pop)}</span>`;
    wrap.appendChild(row);
  }
  s.appendChild(wrap);

  s.appendChild(el('p','', `Peak intensity over populated ground: MMI
    ${ex.max_mmi_over_population} (${MMI_WORD[Math.round(ex.max_mmi_over_population)]||''}).`));

  if (ex.districts?.length){
    s.appendChild(el('h3','', 'Most exposed districts'));
    const ul = el('ul','lst');
    for (const d of ex.districts.slice(0,8)){
      ul.appendChild(el('li','',
        `<span class="nm">${esc(d.name)}</span>`+
        `<span class="d">${fmt(d.pop)} · MMI ${d.mmi}</span>`));
    }
    s.appendChild(ul);
  }

  s.appendChild(el('p','note',
    `<strong>How to read this.</strong> Intensity is USGS ShakeMap output.
     Population is an estimate: 2023 district census projections spread across
     a 5 km grid by settlement density, then sampled against that ShakeMap.
     Treat these as order-of-magnitude figures for planning, not as counts.
     They cover Nepal only, so a cross-border event will read low.`));

  if (e.pager?.alert){
    s.appendChild(el('p','note',
      `USGS PAGER alert level for this event: <strong>${esc(e.pager.alert)}</strong>
       (PAGER covers all affected countries, not just Nepal).`));
  }
  return s;
}

/** Nearest ward centre. Not the containing ward — the index holds centroids
 *  only, because shipping ward polygons would cost megabytes. Labelled
 *  accordingly wherever it is shown. */
function nearestWard(lat, lon){
  const w = D.wards;
  if (!w) return null;
  const F = w.fields;
  let best = null, bestD = Infinity;
  for (const row of w.wards){
    const d = haversine(lat, lon, row[1], row[0]);
    if (d < bestD){ bestD = d; best = row; }
  }
  if (!best) return null;
  return {
    ward: best[2],
    municipality: best[3],
    district: w.districts[best[4]],
    province: best[5] >= 0 ? w.provinces[best[5]] : null,
    type: best[6] >= 0 ? w.types[best[6]] : null,
    dist: bestD
  };
}

/** Census record for a ward, matched on district + municipality + ward. */
function censusFor(w){
  const c = D.census;
  if (!c || !w) return null;
  const dn = s => String(s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
  const strip = s => {
    let n = dn(s);
    for (const t of ['gaunpalika','nagarpalika','mahanagarpalika',
                     'upamahanagarpalika','ruralmunicipality',
                     'submetropolitancity','metropolitancity','municipality']){
      if (n.endsWith(t)) return n.slice(0, -t.length);
    }
    return n;
  };
  if (!c._idx){
    c._idx = new Map();
    for (const r of c.wards){
      const key = strip(c.municipalities[r[1]]) + '|' + r[2];
      if (!c._idx.has(key)) c._idx.set(key, r);
    }
  }
  return c._idx.get(strip(w.municipality) + '|' + w.ward) || null;
}

/** Vulnerable-wall share, coloured by how far above the national median. */
function vulnBand(p){
  if (p >= 85) return ['#FF0000','far above the national median'];
  if (p >= 70) return ['#FF9100','above the national median'];
  if (p >= 50) return ['#FFC900','near the national median'];
  if (p >= 25) return ['#7EFA8D','below the national median'];
  return ['#7DF9F9','far below the national median'];
}

function wardSection(e){
  const s = el('div','sec');
  s.innerHTML = '<h3>Administrative location</h3>';
  const w = nearestWard(e.lat, e.lon);
  if (!w){
    s.appendChild(el('p','','Ward index not loaded.'));
    return s;
  }
  s.appendChild(el('dl','kv',
    `<dt>Ward</dt><dd>${esc(String(w.ward))}</dd>`+
    `<dt>Local level</dt><dd>${esc(w.municipality||'—')}${w.type?` <span style="color:#6B7C8A">(${esc(w.type)})</span>`:''}</dd>`+
    `<dt>District</dt><dd>${esc(w.district||'—')}</dd>`+
    `<dt>Province</dt><dd>${esc(w.province||'—')}</dd>`));
  const cr = censusFor(w);
  if (cr){
    const pop = cr[3], hh = cr[4], vp = cr[5];
    s.appendChild(el('h3','','Ward population, 2021 census'));
    s.appendChild(el('dl','kv',
      `<dt>Population</dt><dd>${fmt(pop)}</dd>`+
      `<dt>Households</dt><dd>${fmt(hh)}</dd>`));
    if (vp != null){
      const [col, word] = vulnBand(vp);
      s.appendChild(el('h3','','Building vulnerability'));
      const row = el('div','brow');
      row.innerHTML =
        `<span class="blab">Walls</span>`+
        `<span class="btrack"><span class="bfill" style="width:${vp}%;background:${col}"></span></span>`+
        `<span class="bval">${vp}%</span>`;
      s.appendChild(row);
      s.appendChild(el('p','',
        `${vp}% of households here have outer walls of mud-bonded brick or
         stone, unbaked brick, or bamboo — ${word} of 61.7%.`));
      s.appendChild(el('p','note',
        `<strong>A proxy, not a structural assessment.</strong> Wall material
         is the strongest single predictor of earthquake damage available at
         ward level, but it says nothing about storeys, age, retrofit or
         ground conditions, and it does not predict collapse or casualties.`));
    }
  }

  s.appendChild(el('p','note',
    `<strong>Nearest ward centre, ${km(w.dist)} away.</strong> This is the
     closest ward centroid, not necessarily the ward containing the
     epicentre — ward polygons are not shipped, because the geometry would
     add megabytes to a page meant to open on a phone after an earthquake.
     For anything official, confirm against the DEOC.`));
  return s;
}

function nearestSection(e){
  const s = el('div','sec');
  s.innerHTML = '<h3>Nearest hospitals to the epicentre</h3>';
  if (!D.hospitals){ s.appendChild(el('p','','Facility data not loaded.')); return s; }

  const near = D.hospitals.facilities
    .filter(h=>h.t===1)
    .map(h => ({...h, dist: haversine(e.lat, e.lon, h.y, h.x)}))
    .sort((a,b)=>a.dist-b.dist)
    .slice(0,6);

  const ul = el('ul','lst');
  for (const h of near){
    ul.appendChild(el('li','',
      `<span class="nm">${esc(h.n)}${h.d?` <span style="color:#6B7C8A">· ${esc(h.d)}</span>`:''}</span>`+
      `<span class="d">${km(h.dist)}</span>`));
  }
  s.appendChild(ul);
  s.appendChild(el('p','note',
    `<strong>Straight-line distance.</strong> Not travel distance and not
     travel time. In Nepal's terrain the road distance is frequently several
     times the straight line, and roads are exactly what an earthquake blocks.
     Facility locations come from OpenStreetMap; presence on the map is not
     evidence that a facility is open, staffed or undamaged.`));
  return s;
}

function placesSection(e){
  const s = el('div','sec');
  s.innerHTML = '<h3>Settlements within 25 km</h3>';
  if (!D.places){ s.appendChild(el('p','','Settlement data not loaded.')); return s; }
  const near = D.places.places
    .map(q => ({...q, dist: haversine(e.lat, e.lon, q.y, q.x)}))
    .filter(q => q.dist <= 25)
    .sort((a,b)=>a.dist-b.dist);

  if (!near.length){
    s.appendChild(el('p','','No mapped settlement within 25 km of the epicentre.'));
    return s;
  }
  const rank = {city:0, town:1, village:2};
  const show = near.sort((a,b)=> (rank[a.k]-rank[b.k]) || (a.dist-b.dist)).slice(0,10);
  const ul = el('ul','lst');
  for (const q of show){
    ul.appendChild(el('li','',
      `<span class="nm">${esc(q.n)} <span style="color:#6B7C8A">${esc(q.k)}${q.m?` · ${esc(q.m)}`:''}</span></span>`+
      `<span class="d">${km(q.dist)}</span>`));
  }
  s.appendChild(ul);
  s.appendChild(el('p','',`${near.length} mapped settlements fall within 25 km.`));
  return s;
}

function impactSection(e){
  const s = el('div','sec');
  s.innerHTML = '<h3>Recorded impacts near this event</h3>';
  if (!D.impacts){ s.appendChild(el('p','','Impact data not loaded.')); return s; }

  const day = 86400000;
  const near = D.impacts.incidents.filter(i => {
    if (!i.lat || !i.lon || !i.on) return false;
    const t = Date.parse(i.on);
    return Math.abs(t - e.t) < 3*day && haversine(e.lat, e.lon, i.lat, i.lon) < 150;
  });

  if (!near.length){
    s.appendChild(el('p','','No NDRRMA impact record matches this event by date and location.'));
    s.appendChild(el('p','note',
      `Absence of a record is not evidence of no damage. Reporting lags,
       and small events are often not recorded at all.`));
    return s;
  }
  const ul = el('ul','lst');
  for (const i of near.slice(0,8)){
    const l = i.loss || {};
    const bits = ['deaths','injured','houses_destroyed','roads_damaged','bridges_damaged']
      .filter(k=>l[k]).map(k=>`${fmt(l[k])} ${k.replace(/_/g,' ')}`);
    ul.appendChild(el('li','',
      `<span class="nm">${esc(i.title||'Incident')}</span>`+
      `<span class="d">${esc(bits.join(', ')||'no loss recorded')}</span>`));
  }
  s.appendChild(ul);
  s.appendChild(el('p','note',
    `<strong>Matched by date and distance only.</strong> NDRRMA's BIPAD records
     carry no earthquake identifier, so this association is a suggestion, not
     an established link. Verify against NDRRMA before citing.`));
  return s;
}

function contactSection(){
  const s = el('div','sec');
  s.innerHTML = '<h3>Emergency contacts</h3>';
  const c = D.contacts;
  if (!c){ s.appendChild(el('p','','Contact data not loaded.')); return s; }

  const wrap = el('div','calls');
  for (const n of c.national){
    const a = el('a','call');
    a.href = 'tel:'+n.number;
    a.innerHTML = `<b>${esc(n.number)}</b><span>${esc(n.label)}<br>${esc(n.detail||'')}</span>`;
    wrap.appendChild(a);
  }
  s.appendChild(wrap);

  const dh = c.disaster_hotline;
  if (dh){
    s.appendChild(el('h3','','Disaster hotline'));
    const w2 = el('div','calls');
    for (const n of dh.candidates){
      const a = el('a','call');
      a.href = 'tel:'+n.number;
      a.innerHTML = `<b>${esc(n.number)}</b><span>${esc(n.label)}</span>`;
      w2.appendChild(a);
    }
    s.appendChild(w2);
    s.appendChild(el('p','note',
      `<strong>Sources disagree on this number.</strong> ${esc(dh.explanation)}
       If neither connects, call 100 or 112.`));
  }

  s.appendChild(el('h3','','Institutions'));
  const ul = el('ul','lst');
  for (const i of c.institutions){
    ul.appendChild(el('li','',
      `<span class="nm"><a href="${esc(i.website)}" target="_blank" rel="noopener">${esc(i.name)}</a></span>`+
      `<span class="d">${esc((i.phone||[]).join(' / ') || '—')}</span>`));
  }
  s.appendChild(ul);
  s.appendChild(el('p','note',
    `<strong>Verify before relying on these.</strong> ${esc(c.verification_status)}
     District-level numbers change and are not reproduced here; use the
     official directories instead.`));
  return s;
}

/* ------------------------------------------------------------- legend/UI */

function addLegend(){
  const c = L.control({position:'bottomleft'});
  c.onAdd = () => {
    const d = L.DomUtil.create('div','legend');
    const ramp = [3,4,5,6,7,8,9,10].map(v=>`<i style="background:${MMI_COLOR[v]}"></i>`).join('');
    d.innerHTML = `<h4>Shaking intensity (MMI)</h4>
      <div class="ramp">${ramp}</div>
      <div class="ramp-lab"><span>Weak</span><span>Extreme</span></div>
      <div style="margin-top:.4rem">Circle size shows magnitude. Colour shows
      measured intensity where USGS has one, otherwise magnitude band.</div>`;
    return d;
  };
  c.addTo(map);
}

function wireUI(){
  document.querySelectorAll('.seg button').forEach(b => {
    b.addEventListener('click', () => {
      document.querySelectorAll('.seg button').forEach(x=>x.classList.remove('on'));
      b.classList.add('on');
      catalogue = b.dataset.cat;
      render();
    });
  });
  const slider = document.getElementById('magmin');
  slider.addEventListener('input', () => {
    magMin = +slider.value;
    document.getElementById('magout').textContent = magMin.toFixed(1);
    render();
  });
  const sheet = document.getElementById('about');
  document.getElementById('btn-about').addEventListener('click', ()=>{
    sheet.hidden = !sheet.hidden;
  });
  document.getElementById('about-close').addEventListener('click', ()=> sheet.hidden = true);
  document.addEventListener('keydown', e => { if (e.key==='Escape') sheet.hidden = true; });
}

function buildAbout(){
  const b = document.getElementById('about-body');
  const m = D.refMeta || {};
  b.innerHTML = `
    <p>This page brings together earthquake catalogues, modelled shaking,
    census population, health facilities and government impact records for
    Nepal. Every layer is attributed, and where a number is an estimate it
    says so next to the number rather than in the small print.</p>

    <h3>Where each thing comes from</h3>
    <ul>
      <li><b>Earthquake locations and magnitudes</b> — USGS FDSN event
      service, and Nepal's National Earthquake Monitoring and Research Centre
      (NEMRC). The two are independent and their magnitudes differ for the
      same event. Neither is adjusted to match the other.</li>
      <li><b>Shaking intensity</b> — USGS ShakeMap. Only events with a
      ShakeMap show intensity. Nothing here models shaking itself.</li>
      <li><b>Population</b> — the 2021 census, at ward level: 6,743 wards,
      28,925,480 people. District figures shown in district popups come from
      the COD-PS 2023 projection (${fmt(m.census_total)}) and are a different,
      later series; the two do not match and are labelled separately.</li>
      <li><b>Building vulnerability</b> — 2021 census outer wall material by
      ward.</li>
      <li><b>Health facilities and settlements</b> — OpenStreetMap via the
      Humanitarian OpenStreetMap Team's HDX exports.
      ${fmt(m.hospitals_tier1)} hospitals and ${fmt(m.places_display)} named
      settlements.</li>
      <li><b>Recorded impacts</b> — NDRRMA's BIPAD portal.</li>
      <li><b>Wards</b> — National Geoportal ward boundaries, 6,792 polygons,
      used as a centroid index and to weight the population grid.</li>
      <li><b>Boundaries</b> — OCHA Common Operational Datasets for geometry and
      pcodes; official local-unit names and types from Nepal's National
      Geoportal.</li>
      <li><b>Active faults</b> — GEM Global Active Faults Database.</li>
    </ul>

    <h3>The exposure estimate</h3>
    <p>Exposure now uses <b>observed ward populations</b> from the 2021
    census. Each of the 6,743 wards carries its own count, assigned to the
    5 km grid cell containing its centre, and USGS's ShakeMap is sampled at
    each cell. Wards average about 4,300 people, so allocation error at this
    cell size is small.</p>
    <p>This replaced an earlier dasymetric estimate that spread district
    totals by settlement density. Recomputing the 2015 Gorkha event showed
    the old method overstated exposure by roughly 4% at MMI 7 and 6% at
    MMI 8 once the difference in population base year is accounted for. The
    figures are now measurements allocated to a grid, not estimates — though
    they remain Nepal-only, so a cross-border event will read low.</p>

    <h3>What this does not do</h3>
    <ul>
      <li>It is <b>not an alerting system</b>. It updates on a schedule and
      may be minutes to hours behind.</li>
      <li>It does <b>not show a seismic hazard zonation</b>. The historical
      layer shows recorded earthquakes, which is a different thing. Nepal's
      authoritative design zonation is in the building code NBC 105:2020 and
      is not reproduced here.</li>
      <li>It does <b>not estimate casualties</b>.</li>
      <li>It does <b>not compute travel times</b> to hospitals. Distances are
      straight lines, which in Nepal's terrain understate real journeys
      substantially.</li>
    </ul>

    <h3>Known gaps</h3>
    <ul>
      <li>OpenStreetMap coverage of health facilities is uneven and better in
      urban areas. Absence from the map is not absence on the ground.</li>
      <li>The earthquake catalogue is far more complete after 2015 than
      before, so any comparison across decades reflects instrumentation as
      much as seismicity.</li>
      <li>Ward census rows sum to 28,925,480 against the published national
      total of 29,164,578 — a shortfall of 239,098 (0.8%). The cause is not
      established here, and the gap is reported rather than distributed.</li>
      <li>District popups show COD-PS 2023 projections while exposure uses
      2021 census counts. These are different series and will not agree.</li>
      <li>Emergency numbers have not been confirmed against official
      directories by a person. The disaster hotline in particular has
      conflicting published values.</li>
      <li>The boundary layer holds 775 polygons, of which 753 are local
      levels and 22 are national parks, wildlife reserves, hunting reserves
      and similar. The latter are drawn differently and carry little or no
      resident population.</li>
      <li>Local-unit names follow the National Geoportal's updated official
      spellings, which differ from OCHA's for 174 of 775 units. Where they
      differ, the OCHA spelling is shown too, so either can be matched.</li>
    </ul>`;
}

boot();

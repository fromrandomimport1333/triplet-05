// @ts-nocheck
// The city map: drawn once per city description (zones, roads, devices) as SVG, then updated in place on
// every state push, so the animations (water, smoke, traffic, units driving) never restart.
// Ported unchanged in behaviour from the previous single-page screen; React only mounts it.
const $ = (id) => document.getElementById(id);
let MAPEL = null;
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

function fmtVal(v) { return typeof v === "number" ? (Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2)) : "-"; }
const SHORT = {water_level: "water", air_quality: "air", traffic_speed: "speed", traffic_density: "density", traffic_flow: "flow", power_load: "power"};
// --- city map ----------------------------------------------------------------------------
// Drawn once per city description (zones, roads, devices), then updated in place every state
// push, so the animations (water, smoke, traffic, units driving) never restart.
const CW = 360, CH = 172, GAP = 62, MG = 18, SEA = 78, LEG = 6;
const TINT = {port: "#12202e", commercial: "#151d27", medical: "#14261f", industrial: "#251f14", residential: "#141c24"};
const UNIT_LOOK = {ambulance: ["#f8fafc", "#c4443d", "A"], fire_truck: ["#e0645c", "#fff", "F"],
  police: ["#6b95e0", "#fff", "P"], rescue: ["#d9803a", "#1a0d00", "R"]};
const GLYPH = {
  valve: '<path class="glyph" d="M-8,-6 L8,6 L8,-6 L-8,6 Z" fill="currentColor"/>',
  traffic_signal: '<g class="glyph"><rect x="-4.5" y="-10" width="9" height="20" rx="2.5" fill="#0c1218" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle cy="-5" r="2.2" fill="currentColor"/><circle r="2.2" fill="currentColor" opacity=".55"/><circle cy="5" r="2.2" fill="currentColor" opacity=".3"/></g>',
  power_grid: '<path class="glyph" d="M2,-11 L-6,2 L-1,2 L-3,11 L6,-3 L1,-3 Z" fill="currentColor"/>',
};
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
let MAPSIG = "", GEO = null;

function mapLayout(zones, layout) {
  const pos = {}, used = new Set();
  for (const z of zones) {
    const p = layout[z.zone_id];
    if (Array.isArray(p) && p.length === 2 && !used.has(p[0] + "," + p[1])) { pos[z.zone_id] = [+p[0], +p[1]]; used.add(p[0] + "," + p[1]); }
  }
  const cols = Math.max(Math.ceil(Math.sqrt(zones.length)), ...Object.values(pos).map(p => p[0] + 1), 1);
  let i = 0;
  for (const z of zones) {
    if (pos[z.zone_id]) continue;
    while (used.has((i % cols) + "," + Math.floor(i / cols))) i++;
    pos[z.zone_id] = [i % cols, Math.floor(i / cols)]; used.add((i % cols) + "," + Math.floor(i / cols));
  }
  const ncols = Math.max(...Object.values(pos).map(p => p[0])) + 1, nrows = Math.max(...Object.values(pos).map(p => p[1])) + 1;
  const free = [];
  for (let r = 0; r < nrows; r++) for (let c = 0; c < ncols; c++) if (!used.has(c + "," + r)) free.push([c, r]);
  return {pos, ncols, nrows, free};
}

function buildMap(st) {
  const m = st.map || {}, zones = st.zones || [];
  const {pos, ncols, nrows, free} = mapLayout(zones, m.layout || {});
  const sea = m.sea;
  const x0 = MG + (sea === "west" ? SEA : 0), y0 = MG + (sea === "north" ? SEA : 0);
  const gw = ncols * CW + (ncols - 1) * GAP, gh = nrows * CH + (nrows - 1) * GAP;
  const vw = x0 + gw + MG + (sea === "east" ? SEA : 0), vh = y0 + gh + MG + (sea === "south" ? SEA : 0) + LEG;
  const box = {};
  for (const z of zones) { const [c, r] = pos[z.zone_id]; box[z.zone_id] = {x: x0 + c * (CW + GAP), y: y0 + r * (CH + GAP)}; }
  const centre = (id) => [box[id].x + CW / 2, box[id].y + CH / 2];
  // the Brain sits in the free cell nearest the middle (or above the grid when the grid is full)
  let hub = [x0 + gw / 2, Math.max(16, y0 - 12)];
  if (free.length) {
    const mc = (ncols - 1) / 2, mr = (nrows - 1) / 2;
    free.sort((a, b) => Math.hypot(a[0] - mc, a[1] - mr) - Math.hypot(b[0] - mc, b[1] - mr));
    hub = [x0 + free[0][0] * (CW + GAP) + CW / 2, y0 + free[0][1] * (CH + GAP) + CH / 2];
  }
  let s = `<svg viewBox="0 0 ${vw} ${vh}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="live city map"><defs>`;
  for (const z of zones) s += `<clipPath id="mc-${z.zone_id}"><rect x="${box[z.zone_id].x}" y="${box[z.zone_id].y}" width="${CW}" height="${CH}" rx="8"/></clipPath>`;
  s += `<pattern id="mwv" width="40" height="12" patternUnits="userSpaceOnUse"><path d="M0,6 Q10,0 20,6 T40,6" fill="none" stroke="#8cc4e3" stroke-width="1.6" opacity=".55"/></pattern></defs>`;
  s += `<rect id="m-frame" x="1" y="1" width="${vw - 2}" height="${vh - 2}" rx="8" fill="#0b1016" stroke="#1f2933" stroke-width="1"/>`;
  if (sea) {
    const r = {west: [0, 0, SEA + MG / 2, vh - LEG], east: [vw - SEA - MG / 2, 0, SEA + MG / 2, vh - LEG],
      north: [0, 0, vw, SEA + MG / 2], south: [0, vh - LEG - SEA - MG / 2, vw, SEA + MG / 2]}[sea];
    if (r) s += `<g><rect x="${r[0]}" y="${r[1]}" width="${r[2]}" height="${r[3]}" fill="#13293b"/>` +
      `<rect class="m-sea" x="${r[0]}" y="${r[1]}" width="${r[2] + 80}" height="${r[3]}" fill="url(#mwv)" opacity=".5"/>` +
      `<text x="${r[0] + r[2] / 2}" y="${r[1] + r[3] / 2}" text-anchor="middle" class="m-legend" fill="#8cc4e3" transform="rotate(${sea === "west" || sea === "east" ? -90 : 0} ${r[0] + r[2] / 2} ${r[1] + r[3] / 2})" style="letter-spacing:.5em">SEA</text></g>`;
  }
  // roads between adjacent zones
  const pairs = [];
  for (const z of zones) for (const a of z.adjacent || []) if (box[a] && z.zone_id < a) pairs.push([z.zone_id, a]);
  s += "<g>";
  for (const [a, b] of pairs) {
    const [ax, ay] = centre(a), [bx, by] = centre(b);
    s += `<line x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="#2c333e" stroke-width="30" stroke-linecap="round"/>` +
      `<line id="mr-${a}-${b}" class="m-road" x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="#5fbf8a" stroke-width="3" stroke-linecap="round" opacity=".5"/>` +
      `<line class="m-flow" x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="#e2e8f0" stroke-width="1.5" stroke-linecap="round" opacity=".3"/>` +
      `<line id="mk-${a}-${b}" class="m-corr" x1="${ax}" y1="${ay}" x2="${bx}" y2="${by}" stroke="#6fcf97" stroke-width="12" opacity="0"/>`;
  }
  s += "</g>";
  // zones
  const acts = m.actuators || {}, sensors = m.sensors || {};
  const apos = {};
  for (const z of zones) {
    const {x, y} = box[z.zone_id], id = z.zone_id;
    s += `<g><rect x="${x}" y="${y}" width="${CW}" height="${CH}" rx="8" fill="${TINT[z.type] || "#141c24"}"/>`;
    s += `<g clip-path="url(#mc-${id})"><g id="mw-${id}" class="m-water" style="transform:scaleY(0)"><rect x="${x}" y="${y}" width="${CW}" height="${CH}" fill="#4f9fd0" opacity=".38"/>` +
      `<rect class="m-wave" x="${x}" y="${y}" width="${CW + 80}" height="12" fill="url(#mwv)"/></g>` +
      `<g id="ms-${id}" class="m-smoke" opacity="0">` +
      [0, 1, 2, 3].map(k => `<circle class="m-puff" cx="${x + 70 + k * 70}" cy="${y + 104}" r="24" fill="#9ca3af" style="animation-delay:${-k * .85}s"/>`).join("") + `</g>` +
      `<rect id="md-${id}" class="m-dim" x="${x}" y="${y}" width="${CW}" height="${CH}" fill="#000" opacity="0"/></g>`;
    s += `<rect id="mo-${id}" x="${x + 1}" y="${y + 1}" width="${CW - 2}" height="${CH - 2}" rx="7" fill="none" stroke="#2c333e" stroke-width="1"/>`;
    s += `<text x="${x + 12}" y="${y + 26}" class="zone-name">${esc(z.name.length > 17 ? z.name.slice(0, 16) + "…" : z.name)}</text>` +
      `<text x="${x + CW - 10}" y="${y + 24}" text-anchor="end" class="zone-id">${esc(id)}</text>` +
      `<text id="mb-${id}" x="${x + 12}" y="${y + 49}" class="zone-badge" fill="#5fbf8a">NORMAL</text>` +
      `<text id="mv-${id}" x="${x + 12}" y="${y + 70}" class="zone-val"></text>` +
      `<line id="ma-${id}" x1="${x + 12}" x2="${x + 202}" stroke="#e0645c" stroke-dasharray="4 3" opacity="0"/>` +
      `<polyline id="mp-${id}" fill="none" stroke="#67b7c4" stroke-width="2" points=""/>`;
    s += `<g id="mcall-${id}" opacity="0" transform="translate(${x + CW - 22} ${y + 70})"><g class="m-call"><circle r="13" fill="#d6a24a"/>` +
      `<path d="M-6,-4 q0,-3 3,-3 l2,4 l-2,2 q2,4 6,6 l2,-2 l4,2 q0,3 -3,3 q-12,-2 -12,-12z" fill="#1a1200"/></g>` +
      `<circle cx="11" cy="-10" r="9" fill="#c4443d"/><text id="mcalln-${id}" x="11" y="-6" text-anchor="middle" class="m-tag" fill="#fff" style="font-size:11px"></text><title id="mcallt-${id}">calls</title></g>`;
    // sensors: one dot each, coloured by Guardian's verdict
    const sl = Object.entries(sensors).filter(([, v]) => v.zone_id === id).sort((a, b) => a[0] < b[0] ? -1 : 1);
    sl.forEach(([sid, v], k) => {
      s += `<circle id="mn-${sid}" cx="${x + CW - 14 - k * 15}" cy="${y + 42}" r="5" fill="#475569"><title>${esc(sid)} (${esc(v.type)})</title></circle>`;
    });
    // actuators along the bottom edge
    const al = Object.entries(acts).filter(([, v]) => v.zone_id === id).sort((a, b) => a[0] < b[0] ? -1 : 1);
    al.forEach(([aid, v], k) => {
      const ax = x + 26 + k * 44, ay = y + CH - 22;
      apos[aid] = [ax, ay];
      s += `<g id="mx-${aid}" class="m-act" transform="translate(${ax} ${ay})" style="color:#64748b"><title>${esc(aid)}</title>` +
        `<circle class="bg" r="16" fill="#0c1218" stroke="currentColor" stroke-width="2"/>${GLYPH[v.type] || '<circle class="glyph" r="5" fill="currentColor"/>'}` +
        `<path id="mq-${aid}" d="M-11,-11 L11,11 M11,-11 L-11,11" stroke="#e0645c" stroke-width="3.5" opacity="0"/></g>` +
        `<text id="mt-${aid}" x="${ax}" y="${ay - (k % 2 ? 36 : 23)}" text-anchor="middle" class="m-tag" opacity="0"></text>`;
    });
    s += "</g>";
  }
  // command beams, the Brain, units on top
  s += `<g id="m-beams"></g>`;
  s += `<g transform="translate(${hub[0]} ${hub[1]})"><circle class="m-hub-ring" r="40"/><circle r="40" fill="#0f151c" stroke="#7fa6e8" stroke-width="2.5"/>` +
    `<text y="-3" text-anchor="middle" style="font:700 15px Inter,sans-serif;letter-spacing:.12em" fill="#e7eaee">CITY</text>` +
    `<text y="15" text-anchor="middle" style="font:700 15px Inter,sans-serif;letter-spacing:.12em" fill="#7fa6e8">BRAIN</text></g>`;
  s += `<g id="m-units">`;
  for (const [u, info] of Object.entries(st.units || {})) {
    const [bg, fg, ch] = UNIT_LOOK[info.type] || ["#94a3b8", "#0c1218", (info.type || "?")[0].toUpperCase()];
    s += `<g id="mu-${u}" class="m-unit nomove"><title>${esc(u)}</title><circle id="mur-${u}" class="m-ring" r="19"/>` +
      `<circle r="14" fill="${bg}" stroke="#0c1218" stroke-width="2"/><text y="4.5" text-anchor="middle" fill="${fg}">${esc(ch)}</text></g>`;
  }
  s += `</g>`;
  s += `</svg>`;
  MAPEL.innerHTML = s;
  GEO = {box, hub, apos, pairs};
}

// the zone's sensor of a type, or null when it is not believed (rejected by Guardian or quarantined):
// the map draws the city as the Brain believes it, so a forged reading never floods a zone (T5)
function zoneSensor(st, zid, type) {
  for (const [sid, v] of Object.entries((st.map || {}).sensors || {})) {
    if (v.zone_id !== zid || v.type !== type) continue;
    return v.verdict === "rejected" || (st.quarantined || {})[sid] !== undefined ? null : v;
  }
  return null;
}
function level(st, z, type) {           // 0 at half the alert threshold, 0.5 at the threshold, 1 at 1.5x
  const L = (z.latest || {})[type], s = zoneSensor(st, z.zone_id, type);
  if (!L || !s || !s.alert) return 0;
  return clamp((L.value - .5 * s.alert) / s.alert, 0, 1);
}
function unitSlot(zid, k) {
  const b = GEO.box[zid]; if (!b) return null;
  return [b.x + CW - 122 + (k % 3) * 36, b.y + 100 + Math.floor(k / 3) * 34];
}

function updateMap(st) {
  const m = st.map || {}, acts = m.actuators || {};
  const zstate = {};
  for (const z of st.zones || []) {
    const id = z.zone_id; zstate[id] = z;
    const inc = z.state === "incident", q = (z.quarantined || []).length > 0, sev = inc && z.incident ? z.incident.severity : "";
    const col = inc ? (sev === "high" || sev === "critical" ? "#e0645c" : "#d6a24a") : q ? "#d6a24a" : "#323945";
    const o = $("mo-" + id);
    o.setAttribute("stroke", col); o.setAttribute("stroke-width", inc ? 4 : q ? 2.5 : 1.5);
    o.setAttribute("stroke-dasharray", q && !inc ? "9 6" : ""); o.classList.toggle("pulse", inc);
    const b = $("mb-" + id);
    if (inc && z.incident) { b.textContent = z.incident.type.toUpperCase().replace("_", " ") + " · " + sev; b.setAttribute("fill", col); }
    else if (q) { b.textContent = "QUARANTINE " + z.quarantined.slice(0, 2).join(", "); b.setAttribute("fill", "#d6a24a"); }
    else { b.textContent = "NORMAL"; b.setAttribute("fill", "#5fbf8a"); }
    $("mw-" + id).style.transform = `scaleY(${(level(st, z, "water_level") * .8).toFixed(3)})`;
    $("ms-" + id).setAttribute("opacity", (level(st, z, "air_quality") * .85).toFixed(2));
    const sp = z.spark, bx = GEO.box[id];
    if (sp && sp.points && sp.points.length > 1) {
      const doubted = !zoneSensor(st, id, sp.type);
      $("mv-" + id).textContent = `${SHORT[sp.type] || sp.type} ${fmtVal(sp.points[sp.points.length - 1])} ${sp.unit || ""}` +
        (doubted ? "  (not believed)" : "");
      let lo = Math.min(...sp.points, sp.alert ?? Infinity), hi = Math.max(...sp.points, sp.alert ?? -Infinity);
      if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
      const X = (i) => bx.x + 12 + i * 190 / (sp.points.length - 1), Y = (v) => bx.y + 106 - (v - lo) / (hi - lo) * 28;
      const p = $("mp-" + id);
      p.setAttribute("points", sp.points.map((v, i) => X(i).toFixed(1) + "," + Y(v).toFixed(1)).join(" "));
      p.setAttribute("stroke", sp.points[sp.points.length - 1] >= (sp.alert ?? Infinity) ? "#e0645c" : "#67b7c4");
      const a = $("ma-" + id);
      if (sp.alert != null) { a.setAttribute("y1", Y(sp.alert)); a.setAttribute("y2", Y(sp.alert)); a.setAttribute("opacity", ".7"); }
    }
    const calls = (m.calls || []).filter(c => c.zone_id === id);
    $("mcall-" + id).setAttribute("opacity", calls.length ? 1 : 0);
    $("mcalln-" + id).textContent = calls.length > 99 ? "99+" : String(calls.length);
    $("mcallt-" + id).textContent = `${calls.length} emergency call(s) in the last ${(m.call_s || 30)} s: ` +
      [...new Set(calls.map(c => c.category || "other"))].join(", ");
    $("md-" + id).setAttribute("opacity", 0);
  }
  // sensors: Guardian's verdict, quarantine
  // Guardian offline: its last verdicts are stale, so the dots go grey (nothing is being checked)
  const gOn = ((st.layers || {}).guardian || {}).status === "online";
  for (const [sid, v] of Object.entries(m.sensors || {})) {
    const el = $("mn-" + sid); if (!el) continue;
    const quar = (st.quarantined || {})[sid] !== undefined;
    el.setAttribute("fill", quar ? "#e0645c" : !gOn ? "#475569" : {trusted: "#5fbf8a", suspicious: "#d6a24a", rejected: "#e0645c"}[v.verdict] || "#475569");
    el.setAttribute("r", quar || v.verdict === "rejected" ? 7 : 5);
    el.setAttribute("stroke", quar ? "#fff" : "none");
    el.querySelector("title").textContent = `${sid} (${v.type}): ${quar ? "QUARANTINED" : !gOn ? "not checked (Guardian offline)" : v.verdict || "no verdict yet"}`;
  }
  // actuators: state, quarantine, the last outcome (refused by a safety rule, failed)
  for (const [aid, a] of Object.entries(acts)) {
    const g = $("mx-" + aid); if (!g) continue;
    const st8 = String(a.state || "");
    const on = (a.type === "valve" && st8.startsWith("open")) || st8 === "priority" || st8 === "shed";
    const colour = st8 === "shed" ? "#d6a24a" : a.type === "valve" ? "#67b7c4" : "#6fcf97";
    g.style.color = on ? colour : "#64748b";
    g.classList.toggle("active", on); g.classList.toggle("spin", on && a.type === "valve");
    g.querySelector("title").textContent = `${aid} (${a.type}): ${st8 || "state unknown"}`;
    const quar = (st.quarantined || {})[aid] !== undefined;
    $("mq-" + aid).setAttribute("opacity", quar ? 1 : 0);
    const t = $("mt-" + aid), out = a.outcome;
    if (out && (out.status === "refused" || out.status === "failed")) {
      t.textContent = (out.status === "refused" ? "REFUSED " + (out.rule || "") : "FAILED").trim();
      t.setAttribute("fill", "#e0645c"); t.setAttribute("opacity", clamp(1.4 - out.age_s / 12, .25, 1).toFixed(2));
    } else if (on) { t.textContent = st8.startsWith("open") ? "OPEN" : st8 === "priority" ? "PRIO" : st8.toUpperCase(); t.setAttribute("fill", colour); t.setAttribute("opacity", .9); }
    else t.setAttribute("opacity", 0);
    if (st8 === "shed") for (const zid of a.controls || []) { const d = $("md-" + zid); if (d) d.setAttribute("opacity", .32); }
  }
  // roads: traffic density of the two zones; green corridor when a signal gives priority
  for (const [a, b] of GEO.pairs) {
    const r = Math.max(level2(st, zstate[a]), level2(st, zstate[b]));
    $(`mr-${a}-${b}`).setAttribute("stroke", r >= 1 ? "#e0645c" : r >= .6 ? "#d6a24a" : "#5fbf8a");
    const prio = Object.values(acts).some(x => x.type === "traffic_signal" && x.state === "priority" && (x.zone_id === a || x.zone_id === b));
    $(`mk-${a}-${b}`).setAttribute("opacity", prio ? .55 : 0);
  }
  // units: drive to the zone they are in or heading to (home when returning)
  const byZone = {};
  for (const [u, info] of Object.entries(st.units || {}).sort((x, y) => x[0] < y[0] ? -1 : 1)) {
    const g = $("mu-" + u); if (!g) continue;
    const zid = info.status === "returning" && info.home_zone ? info.home_zone : info.zone_id;
    const k = (byZone[zid] = (byZone[zid] || 0) + 1) - 1, p = unitSlot(zid, k);
    if (!p) continue;
    g.style.transform = `translate(${p[0]}px, ${p[1]}px)`;
    GEO.upos = GEO.upos || {}; GEO.upos[u] = p;
    $("mur-" + u).setAttribute("class", "m-ring " + (info.status || ""));
    g.querySelector("title").textContent = `${u} (${info.type}): ${(info.status || "?").replace("_", " ")} in ${info.zone_id}`;
    if (g.classList.contains("nomove")) requestAnimationFrame(() => requestAnimationFrame(() => g.classList.remove("nomove")));
  }
  // command beams from the Brain: blue = sent, green = acknowledged, red = rejected or failed
  let beams = "";
  for (const c of m.commands || []) {
    const to = GEO.apos[c.target_id] || (GEO.upos || {})[c.target_id];
    if (!to) continue;
    const colour = c.result === "done" ? "#5fbf8a" : c.result ? "#e0645c" : "#7fa6e8";
    const op = clamp(1 - c.age_s / 10, 0, 1);
    if (op <= 0) continue;
    const [hx, hy] = GEO.hub, mx = (hx + to[0]) / 2, my = Math.min(hy, to[1]) - 50;
    beams += `<path class="m-beam" d="M${hx},${hy} Q${mx},${my} ${to[0]},${to[1]}" stroke="${colour}" opacity="${op.toFixed(2)}"/>` +
      `<circle cx="${to[0]}" cy="${to[1]}" r="22" fill="none" stroke="${colour}" stroke-width="3" opacity="${op.toFixed(2)}"/>`;
  }
  $("m-beams").innerHTML = beams;
  $("m-frame").setAttribute("stroke", st.safe_mode ? "#d6a24a" : "#1f2933");
  $("m-frame").setAttribute("stroke-width", st.safe_mode ? 2 : 1);
}
function level2(st, z) {                // traffic: density over its alert threshold (1 = at the threshold)
  if (!z) return 0;
  const L = (z.latest || {}).traffic_density, s = zoneSensor(st, z.zone_id, "traffic_density");
  return L && s && s.alert ? L.value / s.alert : 0;
}

function renderMap(st) {
  const zones = st.zones || [];
  if (!zones.length) return;
  const m = st.map || {};
  const sig = JSON.stringify([zones.map(z => [z.zone_id, z.name, z.adjacent]), m.layout, m.sea,
    Object.keys(m.actuators || {}).sort(), Object.keys(m.sensors || {}).sort(), Object.keys(st.units || {}).sort()]);
  if (sig !== MAPSIG) { buildMap(st); MAPSIG = sig; }
  updateMap(st);
}


export function mountMap(el) { MAPEL = el; MAPSIG = ""; GEO = null; }
export function drawMap(st) { if (MAPEL) renderMap(st); }

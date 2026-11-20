// ZeppLocal dashboard. Plain JS + SVG; talks only to this local server.
"use strict";

const SECRET = document.querySelector('meta[name="zl-secret"]').content;
const view = document.getElementById("view");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 0) => (v == null ? "—" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
const DAY = 86400;

let days = 30;
try { days = Number(localStorage.getItem("zl-days")) || 30; } catch (e) { /* storage unavailable */ }

function range() {
  const to = new Date();
  const from = new Date(to.getTime() - (days - 1) * DAY * 1000);
  const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return { from: iso(from), to: iso(to) };
}
const qs = (extra = {}) => new URLSearchParams({ ...range(), ...extra }).toString();
async function api(path, extra) {
  const res = await fetch(`/api/${path}${path.includes("?") ? "&" : "?"}${qs(extra)}`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
  return res.json();
}

// ------------------------------------------------------------------ charts
const tip = document.createElement("div");
tip.className = "tip"; tip.hidden = true; document.body.appendChild(tip);

function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 1; hi += 1; }
  const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= step0);
  const ticks = [];
  const top = Math.ceil(hi / step - 1e-9) * step;  // last tick must cover the maximum
  for (let v = Math.floor(lo / step) * step; v <= top + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

const dateLabel = (x, span) => {
  const d = new Date(x * 1000);
  if (span <= 2 * DAY) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
};

/* series: [{points:[[t_sec, y]], color, label, bars?}] ; gap: seconds that break the line */
function chart(series, { height = 200, gap = Infinity, unit = "", digits = 0, yMin, bars = false, wide = false } = {}) {
  const W = wide ? 1040 : 420, H = height, L = 38, R = 8, T = 8, B = 22;
  const all = series.flatMap((s) => s.points);
  const wrap = document.createElement("div");
  if (!all.length) { wrap.innerHTML = `<div class="empty">No data in this range</div>`; return wrap; }
  let x0 = Math.min(...all.map((p) => p[0])), x1 = Math.max(...all.map((p) => p[0]));
  if (bars) { x0 -= DAY / 2; x1 += DAY / 2; }
  if (x1 === x0) x1 = x0 + 1;
  const ys = all.map((p) => p[1]);
  const ticks = niceTicks(yMin ?? Math.min(...ys), Math.max(...ys));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const sx = (x) => L + ((x - x0) / (x1 - x0)) * (W - L - R);
  const sy = (y) => T + (1 - (y - y0) / (y1 - y0 || 1)) * (H - T - B);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img"><g class="axis">`;
  for (const t of ticks) svg += `<line x1="${L}" x2="${W - R}" y1="${sy(t)}" y2="${sy(t)}"/><text x="${L - 6}" y="${sy(t) + 3}" text-anchor="end">${fmt(t, t % 1 ? 1 : 0)}</text>`;
  const span = x1 - x0;
  for (let i = 0; i <= 4; i++) { const x = x0 + (span * i) / 4; svg += `<text x="${sx(x)}" y="${H - 6}" text-anchor="${i === 0 ? "start" : i === 4 ? "end" : "middle"}">${dateLabel(x, span)}</text>`; }
  svg += `</g>`;
  for (const s of series) {
    const pts = [...s.points].sort((a, b) => a[0] - b[0]);
    if (bars || s.bars) {
      const bw = Math.max(1, ((W - L - R) * DAY) / (x1 - x0) * 0.7);
      for (const [x, y] of pts) svg += `<rect x="${sx(x) - bw / 2}" y="${sy(Math.max(y, y0))}" width="${bw}" height="${Math.max(0, sy(y0) - sy(Math.max(y, y0)))}" rx="1.5" fill="${s.color}"/>`;
      continue;
    }
    // Breaks only for real holes: never smaller than 3× the typical spacing of the (bucketed) data.
    const dxs = pts.slice(1).map((p, i) => p[0] - pts[i][0]).sort((a, b) => a - b);
    const brk = Math.max(gap, 3 * (dxs[Math.floor(dxs.length / 2)] || 0));
    let d = "", prev = null;
    for (const [x, y] of pts) { d += `${prev == null || x - prev > brk ? "M" : "L"}${sx(x).toFixed(1)},${sy(y).toFixed(1)}`; prev = x; }
    svg += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>`;
    if (pts.length < 60) for (const [x, y] of pts) svg += `<circle cx="${sx(x)}" cy="${sy(y)}" r="2.2" fill="${s.color}"/>`;
  }
  svg += `<line class="cursor" y1="${T}" y2="${H - B}" stroke="${css("--muted")}" stroke-dasharray="2 3" visibility="hidden"/></svg>`;
  wrap.innerHTML = svg;
  if (series.length > 1) wrap.innerHTML += `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${esc(s.label)}</span>`).join("")}</div>`;
  const el = wrap.querySelector("svg"), cursor = wrap.querySelector(".cursor");
  el.addEventListener("mousemove", (ev) => {
    const r = el.getBoundingClientRect(), x = x0 + ((ev.clientX - r.left) / r.width * W - L) / (W - L - R) * (x1 - x0);
    const lines = [];
    let best = null;
    for (const s of series) {
      let p = null;
      for (const q of s.points) if (!p || Math.abs(q[0] - x) < Math.abs(p[0] - x)) p = q;
      if (p && Math.abs(p[0] - x) < (bars ? DAY : Math.max(gap === Infinity ? span / 20 : gap, span / 200))) { lines.push(`${series.length > 1 ? s.label + ": " : ""}${fmt(p[1], digits)} ${unit}`); best = p; }
    }
    if (!best) { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); return; }
    cursor.setAttribute("x1", sx(best[0])); cursor.setAttribute("x2", sx(best[0])); cursor.setAttribute("visibility", "visible");
    const when = new Date(best[0] * 1000);
    tip.textContent = `${bars ? when.toLocaleDateString() : when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })} · ${lines.join(" · ")}`;
    tip.hidden = false; tip.style.left = `${ev.clientX + 12}px`; tip.style.top = `${ev.clientY - 28}px`;
  });
  el.addEventListener("mouseleave", () => { tip.hidden = true; cursor.setAttribute("visibility", "hidden"); });
  return wrap;
}

const dayTs = (d) => new Date(`${d}T12:00:00`).getTime() / 1000;
const dailyPts = (rows) => (rows || []).map(([d, v]) => [dayTs(d), v]);

function hypnogram(stages, wide = false) {
  const wrap = document.createElement("div");
  if (!stages.length) { wrap.innerHTML = `<div class="empty">No stage timeline for this night</div>`; return wrap; }
  const order = { awake: 0, rem: 1, light: 2, deep: 3, unknown: 2 };
  const colors = { awake: css("--awake"), rem: css("--rem"), light: css("--light"), deep: css("--deep"), unknown: css("--line") };
  const W = wide ? 1040 : 420, H = 150, L = 44, T = 6, row = 30;
  const t0 = stages[0].start_ts, t1 = stages[stages.length - 1].end_ts;
  const sx = (t) => L + ((t - t0) / (t1 - t0)) * (W - L - 8);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}">`;
  ["awake", "rem", "light", "deep"].forEach((s, i) => (svg += `<text x="0" y="${T + i * row + 19}">${s}</text>`));
  for (const s of stages) svg += `<rect x="${sx(s.start_ts)}" y="${T + order[s.stage] * row + 4}" width="${Math.max(1, sx(s.end_ts) - sx(s.start_ts))}" height="${row - 8}" rx="2" fill="${colors[s.stage]}"><title>${s.stage} ${new Date(s.start_ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}–${new Date(s.end_ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</title></rect>`;
  svg += `<text x="${L}" y="${H - 2}">${new Date(t0 * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text><text x="${W - 8}" y="${H - 2}" text-anchor="end">${new Date(t1 * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text></svg>`;
  wrap.innerHTML = svg;
  return wrap;
}

/* Several nights on one clock-time axis (evening → morning). Each night is a step line over the stage lanes. */
const NIGHT_COLORS = ["--sleep", "--hr", "--act", "--body", "--rem", "--accent", "--warn", "--deep"];
function sleepOverlay(nights) {
  const wrap = document.createElement("div");
  nights = nights.filter((n) => n.stages.length);
  if (!nights.length) { wrap.innerHTML = `<div class="empty">No stage timelines for the chosen nights</div>`; return wrap; }
  const lane = { awake: 0, rem: 1, light: 2, deep: 3, unknown: 2 };
  // seconds since 18:00 local on the evening the night started
  const rel = (ts, ref) => { const d = new Date(ref * 1000); const e = new Date(d); e.setHours(18, 0, 0, 0); if (d < e) e.setDate(e.getDate() - 1); return ts - e.getTime() / 1000; };
  const spans = nights.map((n) => [rel(n.stages[0].start_ts, n.stages[0].start_ts), rel(n.stages.at(-1).end_ts, n.stages[0].start_ts)]);
  const h0 = Math.floor(Math.min(...spans.map((s) => s[0])) / 3600), h1 = Math.ceil(Math.max(...spans.map((s) => s[1])) / 3600);
  const W = 1040, H = 190, L = 44, R = 8, T = 10, B = 22, laneH = (H - T - B) / 4;
  const sx = (r) => L + ((r - h0 * 3600) / ((h1 - h0) * 3600)) * (W - L - R);
  const sy = (st) => T + lane[st] * laneH + laneH / 2;
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}"><g class="axis">`;
  ["awake", "rem", "light", "deep"].forEach((s, i) => (svg += `<line x1="${L}" x2="${W - R}" y1="${T + (i + 0.5) * laneH}" y2="${T + (i + 0.5) * laneH}"/><text x="0" y="${T + (i + 0.5) * laneH + 4}">${s}</text>`));
  const stepH = h1 - h0 > 12 ? 2 : 1;
  for (let h = h0; h <= h1; h += stepH) svg += `<text x="${sx(h * 3600)}" y="${H - 6}" text-anchor="middle">${String((18 + h) % 24).padStart(2, "0")}:00</text>`;
  svg += `</g>`;
  nights.forEach((n, i) => {
    const ref = n.stages[0].start_ts, color = css(NIGHT_COLORS[i % NIGHT_COLORS.length]);
    let d = "";
    n.stages.forEach((s, j) => {
      const x0 = sx(rel(s.start_ts, ref)).toFixed(1), x1 = sx(rel(s.end_ts, ref)).toFixed(1), y = sy(s.stage).toFixed(1);
      d += `${j ? "L" : "M"}${x0},${y}L${x1},${y}`;
    });
    svg += `<path d="${d}" fill="none" stroke="${color}" stroke-width="2" stroke-opacity="${nights.length > 3 ? 0.75 : 0.95}" stroke-linejoin="round"><title>${esc(n.label)}</title></path>`;
  });
  svg += `</svg>`;
  wrap.innerHTML = svg + `<div class="legend">${nights.map((n, i) => `<span><i style="background:${css(NIGHT_COLORS[i % NIGHT_COLORS.length])}"></i>${esc(n.label)}</span>`).join("")}</div>`;
  return wrap;
}

/* Stacked bars: buckets [{label, parts:{key: value}}], colours {key: color}. */
function stackedBars(buckets, colors, { unit = "", digits = 0, height = 200, wide = true } = {}) {
  const wrap = document.createElement("div");
  if (!buckets.length) { wrap.innerHTML = `<div class="empty">No data in this range</div>`; return wrap; }
  const W = wide ? 1040 : 420, H = height, L = 38, R = 8, T = 8, B = 22;
  const totals = buckets.map((b) => Object.values(b.parts).reduce((a, v) => a + v, 0));
  const ticks = niceTicks(0, Math.max(...totals, 1)), y1 = ticks.at(-1);
  const sy = (v) => T + (1 - v / y1) * (H - T - B);
  const slot = (W - L - R) / buckets.length, bw = Math.max(2, slot * 0.72);
  let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}"><g class="axis">`;
  for (const t of ticks) svg += `<line x1="${L}" x2="${W - R}" y1="${sy(t)}" y2="${sy(t)}"/><text x="${L - 6}" y="${sy(t) + 3}" text-anchor="end">${fmt(t, t % 1 ? 1 : 0)}</text>`;
  const every = Math.ceil(buckets.length / 8);
  buckets.forEach((b, i) => { if (i % every === 0) svg += `<text x="${L + slot * (i + 0.5)}" y="${H - 6}" text-anchor="middle">${esc(b.label)}</text>`; });
  svg += `</g>`;
  buckets.forEach((b, i) => {
    let acc = 0;
    for (const [k, v] of Object.entries(b.parts)) {
      if (!v) continue;
      svg += `<rect x="${L + slot * i + (slot - bw) / 2}" y="${sy(acc + v)}" width="${bw}" height="${sy(acc) - sy(acc + v)}" fill="${colors[k]}"><title>${esc(b.label)} · ${esc(sportLabel(k))}: ${fmt(v, digits)} ${esc(unit)}</title></rect>`;
      acc += v;
    }
  });
  wrap.innerHTML = svg + `</svg><div class="legend">${Object.keys(colors).map((k) => `<span><i style="background:${colors[k]}"></i>${esc(sportLabel(k))}</span>`).join("")}</div>`;
  return wrap;
}

// ------------------------------------------------------------------ sports
const SPORT_LABELS = { run: "Running", treadmill: "Treadmill", walking: "Walking", outdoor_cycling: "Outdoor cycling", indoor_cycling: "Indoor cycling",
  pool_swimming: "Pool swimming", open_water_swimming: "Open-water swimming", activity: "Auto-detected activity", strength_training: "Strength training" };
const sportLabel = (t) => SPORT_LABELS[t] || (t.startsWith("unknown:") ? `Sport #${t.slice(8)}` : t.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
const sportColors = (types) => Object.fromEntries(types.map((t, i) => [t, css(NIGHT_COLORS[i % NIGHT_COLORS.length])]));
const ZONE_NAMES = ["Warm-up", "Fat burn", "Aerobic", "Anaerobic", "VO₂ max", "Max"];
const ZONE_COLORS = ["--line", "--body", "--accent", "--act", "--hr", "--rem"];
const realDist = (w) => (w.distance_m >= 100 ? w.distance_m : 0);
const durS = (w) => w.moving_s || w.end_ts - w.start_ts;
const isoWeek = (ts) => { const d = new Date(ts * 1000); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return d; };

function zoneBar(secs) {
  const total = secs.reduce((a, v) => a + v, 0);
  if (!total) return `<span class="note">no zone data</span>`;
  return `<div class="zones">${secs.map((v, i) => (v ? `<i style="width:${(100 * v) / total}%;background:${css(ZONE_COLORS[i])}" title="${ZONE_NAMES[i]}: ${hm(v / 60)} (${Math.round((100 * v) / total)}%)"></i>` : "")).join("")}</div>`;
}
const zoneLegend = () => `<div class="legend">${ZONE_NAMES.map((n, i) => `<span><i style="background:${css(ZONE_COLORS[i])}"></i>${n}</span>`).join("")}</div>`;
const sumZones = (ws) => ws.reduce((acc, w) => { (JSON.parse(w.hr_zones || "[]")).forEach((v, i) => (acc[i] += v)); return acc; }, [0, 0, 0, 0, 0, 0]);

/* GPS track drawn as a plain polyline. No map tiles: tile servers would learn where you run. */
function routeSvg(route) {
  const wrap = document.createElement("div");
  if (route.length < 2) { wrap.innerHTML = `<div class="empty">No GPS track recorded</div>`; return wrap; }
  const lat0 = route.reduce((a, p) => a + p[1], 0) / route.length, k = Math.cos((lat0 * Math.PI) / 180);
  const xs = route.map((p) => p[2] * k), ys = route.map((p) => -p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const W = 420, H = 300, pad = 16, s = Math.min((W - 2 * pad) / (maxX - minX || 1e-9), (H - 2 * pad) / (maxY - minY || 1e-9));
  const ox = (W - (maxX - minX) * s) / 2, oy = (H - (maxY - minY) * s) / 2;
  const pts = xs.map((x, i) => `${(ox + (x - minX) * s).toFixed(1)},${(oy + (ys[i] - minY) * s).toFixed(1)}`);
  // 1 km scale bar
  const kmPx = (1 / 111.32) * s;
  wrap.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}"><rect width="${W}" height="${H}" rx="10" fill="${css("--bg")}"/>
    <polyline points="${pts.join(" ")}" fill="none" stroke="${css("--hr")}" stroke-width="3" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${pts[0].split(",")[0]}" cy="${pts[0].split(",")[1]}" r="5" fill="${css("--accent")}"/>
    <circle cx="${pts.at(-1).split(",")[0]}" cy="${pts.at(-1).split(",")[1]}" r="5" fill="${css("--ink")}"/>
    ${kmPx > 20 && kmPx < W / 2 ? `<line x1="${pad}" x2="${pad + kmPx}" y1="${H - pad}" y2="${H - pad}" stroke="${css("--muted")}" stroke-width="2"/><text x="${pad}" y="${H - pad - 5}">1 km</text>` : ""}</svg>`;
  return wrap;
}

// ------------------------------------------------------------------ helpers
const card = (title, body, cls = "") => { const c = document.createElement("section"); c.className = `card ${cls}`; c.innerHTML = `<h2>${esc(title)}</h2>`; if (body) c.append(body); return c; };
const tile = (k, v, u = "", s = "") => `<div class="card tile"><div class="k">${esc(k)}</div><div class="v">${v}<span class="u">${esc(u)}</span></div><div class="s">${s}</div></div>`;
const avg = (rows) => (rows && rows.length ? rows.reduce((a, r) => a + r[1], 0) / rows.length : null);
const latest = (rows) => (rows && rows.length ? rows[rows.length - 1] : null);
const hm = (min) => { if (min == null) return "—"; const m = Math.round(min); return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`; };
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
// Pace only where it means something: min/km on foot, min/100 m in the pool; indoor rides report a token distance.
const pace = (w) => {
  if (!(w.distance_m > 0 && w.moving_s > 0)) return "—";
  if (/swim/.test(w.type)) return `${mmss(w.moving_s / (w.distance_m / 100))} /100m`;
  if (/run|walk|treadmill|hik|trail/.test(w.type) && w.distance_m > 200) return `${mmss(w.moving_s / (w.distance_m / 1000))} /km`;
  return "—";
};
const dt = (ts) => new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
function mount(...nodes) { view.replaceChildren(...nodes); }
function grid(...cards) { const g = document.createElement("div"); g.className = "grid"; g.append(...cards); return g; }
function html(s) { const d = document.createElement("div"); d.innerHTML = s; return d.firstElementChild || d; }

// ------------------------------------------------------------------ pages
const pages = {
  async overview() {
    const d = await api("daily", { metrics: "steps,resting_hr,readiness,stress,charge,pai_total" });
    const sl = await api("sleep", { stages: "latest" });
    const hr = await api("samples", { metric: "heart_rate", from: range().to });
    const night = sl.sessions.at(-1);
    const tiles = html(`<div class="tiles">
      ${tile("Steps today", fmt(latest(d.steps)?.[1]), "", `avg ${fmt(avg(d.steps))} / day`)}
      ${tile("Resting HR", fmt(latest(d.resting_hr)?.[1]), "bpm", `avg ${fmt(avg(d.resting_hr))}`)}
      ${tile("Last sleep", hm(night?.duration_min), "", night ? `score ${fmt(night.score)}` : "")}
      ${tile("Readiness", fmt(latest(d.readiness)?.[1]), "", "")}
      ${tile("Stress", fmt(latest(d.stress)?.[1]), "", `avg ${fmt(avg(d.stress))}`)}
      ${tile("PAI", fmt(latest(d.pai_total)?.[1]), "", "7-day total")}
    </div>`);
    mount(html(`<h1>Overview</h1>`), tiles, grid(
      card("Heart rate today", chart([{ points: hr, color: css("--hr") }], { gap: 900, unit: "bpm" })),
      card("Last night", hypnogram(sl.stages || [])),
      card("Steps", chart([{ points: dailyPts(d.steps), color: css("--act") }], { bars: true, yMin: 0, unit: "steps" })),
      card("Resting heart rate", chart([{ points: dailyPts(d.resting_hr), color: css("--hr") }], { gap: 1.5 * DAY, unit: "bpm" })),
    ));
  },

  async heart() {
    const d = await api("daily", { metrics: "resting_hr,sleep_rhr,device_max_hr" });
    const samples = await api("samples", { metric: "heart_rate" });
    const hrv = await api("samples", { metric: "hrv" });
    mount(html(`<h1>Heart</h1>`), grid(
      card("Heart rate (minute samples, downsampled)", chart([{ points: samples, color: css("--hr") }], { gap: 900, unit: "bpm", wide: true }), "wide"),
      card("Resting heart rate", chart([{ points: dailyPts(d.resting_hr), color: css("--hr"), label: "resting" }, { points: dailyPts(d.sleep_rhr), color: css("--sleep"), label: "during sleep" }], { gap: 1.5 * DAY, unit: "bpm" })),
      card("HRV", chart([{ points: hrv, color: css("--body") }], { gap: 1.5 * DAY, unit: "ms" })),
    ));
  },

  async sleep() {
    const sl = await api("sleep", { stages: "latest" });
    const s = sl.sessions;
    const pts = (k) => s.filter((x) => x[k] != null).map((x) => [dayTs(x.date), x[k] / 60]);
    const table = html(`<div class="tablewrap"><table><thead><tr><th title="Show on the comparison chart">Compare</th><th>Night of</th><th>Asleep</th><th>Deep</th><th>Light</th><th>REM</th><th>Awake</th><th>Score</th></tr></thead><tbody>
      ${[...s].reverse().map((x) => `<tr class="click" data-id="${esc(x.sleep_id)}"><td><input type="checkbox" class="cmp" value="${esc(x.sleep_id)}"></td><td>${esc(x.date)}</td><td>${hm(x.duration_min)}</td><td>${hm(x.deep_min)}</td><td>${hm(x.light_min)}</td><td>${hm(x.rem_min)}</td><td>${hm(x.awake_min)}</td><td>${fmt(x.score)}</td></tr>`).join("")}
    </tbody></table></div>`);
    const hyp = card(`Stages · ${sl.sessions.at(-1)?.date ?? ""}`, hypnogram(sl.stages || [], true), "wide");

    // ---- compare several nights on one clock-time chart
    let pick = 7;
    try { pick = Number(localStorage.getItem("zl-sleep-n")) || 7; } catch (e) { /* ignore */ }
    const cmpCard = card("Compare nights", null, "wide");
    const controls = html(`<div class="row" style="margin-bottom:8px"><span class="note">Last</span>
      ${[3, 7, 14].map((n) => `<button class="btn ghost nbtn" data-n="${n}">${n} nights</button>`).join("")}
      <span class="note">or tick nights in the table below</span></div>`);
    const cmpBody = document.createElement("div");
    cmpCard.append(controls, cmpBody);
    const boxes = () => [...table.querySelectorAll("input.cmp")];
    async function drawCompare() {
      const ids = boxes().filter((b) => b.checked).map((b) => b.value);
      controls.querySelectorAll(".nbtn").forEach((b) => b.classList.toggle("on", Number(b.dataset.n) === pick && ids.length === Math.min(pick, s.length)));
      if (!ids.length) { cmpBody.innerHTML = `<div class="empty">Tick one or more nights</div>`; return; }
      const r = await api("sleep", { compare: ids.join(",") });
      const byId = Object.fromEntries(s.map((x) => [x.sleep_id, x]));
      const nights = ids.reverse().map((id) => ({ label: `${byId[id]?.date} · ${hm(byId[id]?.duration_min)}`, stages: r.stages.filter((st) => st.sleep_id === id) }));
      cmpBody.replaceChildren(sleepOverlay(nights));
    }
    const selectLast = (n) => { pick = n; boxes().forEach((b, i) => (b.checked = i < n)); try { localStorage.setItem("zl-sleep-n", String(n)); } catch (e) { /* ignore */ } drawCompare(); };
    controls.addEventListener("click", (ev) => { const b = ev.target.closest(".nbtn"); if (b) selectLast(Number(b.dataset.n)); });
    table.addEventListener("change", (ev) => { if (ev.target.matches("input.cmp")) drawCompare(); });

    table.addEventListener("click", async (ev) => {
      if (ev.target.matches("input")) return;
      const tr = ev.target.closest("tr[data-id]"); if (!tr) return;
      const r = await api("sleep", { stages: tr.dataset.id });
      hyp.replaceChildren(html(`<h2>Stages · ${esc(tr.children[1].textContent)}</h2>`), hypnogram(r.stages || [], true));
      hyp.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    mount(html(`<h1>Sleep</h1>`), grid(
      card("Hours per night", chart([
        { points: pts("duration_min"), color: css("--sleep"), label: "asleep" },
        { points: pts("deep_min"), color: css("--deep"), label: "deep" },
        { points: pts("rem_min"), color: css("--rem"), label: "REM" }], { gap: 1.5 * DAY, unit: "h", digits: 1, yMin: 0, wide: true }), "wide"),
      cmpCard,
      hyp,
      card("Nights", table, "wide"),
    ));
    selectLast(pick);
  },

  async activity() {
    const d = await api("daily", { metrics: "steps,distance,active_calories,pai_daily,pai_total" });
    mount(html(`<h1>Activity</h1>`), grid(
      card("Steps", chart([{ points: dailyPts(d.steps), color: css("--act") }], { bars: true, yMin: 0, unit: "steps" })),
      card("Distance", chart([{ points: dailyPts(d.distance).map(([t, v]) => [t, v / 1000]), color: css("--act") }], { bars: true, yMin: 0, unit: "km", digits: 1 })),
      card("Active calories", chart([{ points: dailyPts(d.active_calories), color: css("--hr") }], { bars: true, yMin: 0, unit: "kcal" })),
      card("PAI", chart([{ points: dailyPts(d.pai_total), color: css("--accent"), label: "7-day total" }, { points: dailyPts(d.pai_daily), color: css("--act"), label: "daily" }], { gap: 1.5 * DAY, yMin: 0 })),
    ));
  },

  async body() {
    const d = await api("daily", { metrics: "readiness,charge,stress,respiratory_rate,spo2_odi,vo2max,training_load" });
    const spo2 = await api("samples", { metric: "spo2" });
    const stress = await api("samples", { metric: "stress" });
    const line = (rows, color, unit, digits = 0) => chart([{ points: dailyPts(rows), color }], { gap: 1.5 * DAY, unit, digits });
    mount(html(`<h1>Body &amp; recovery</h1>`), grid(
      card("Readiness", line(d.readiness, css("--body"), "")),
      card("Charge", line(d.charge, css("--accent"), "")),
      card("Daily stress (avg)", line(d.stress, css("--warn"), "")),
      card("Stress readings", chart([{ points: stress, color: css("--warn") }], { gap: 1800, unit: "" })),
      card("SpO₂ readings", chart([{ points: spo2, color: css("--body") }], { gap: 6 * 3600, unit: "%" })),
      card("Respiratory rate", line(d.respiratory_rate, css("--sleep"), "br/min", 1)),
      card("VO₂ max", line(d.vo2max, css("--hr"), "", 1)),
      card("Training load", line(d.training_load, css("--act"), "")),
    ));
  },

  async workouts(sel) {
    const all = await api("workouts");
    const types = [...new Set(all.map((w) => w.type))].sort((x, y) => all.filter((w) => w.type === y).length - all.filter((w) => w.type === x).length);
    const colors = sportColors(types);
    const ws = sel ? all.filter((w) => w.type === sel) : all;
    const chips = html(`<div class="row chips">
      <a class="chip ${sel ? "" : "on"}" href="#/workouts">All <b>${all.length}</b></a>
      ${types.map((t) => `<a class="chip ${t === sel ? "on" : ""}" href="#/workouts/${encodeURIComponent(t)}"><i style="background:${colors[t]}"></i>${esc(sportLabel(t))} <b>${all.filter((w) => w.type === t).length}</b></a>`).join("")}
    </div>`);
    if (!all.length) return mount(html(`<h1>Workouts</h1>`), html(`<div class="empty">No workouts in this range. Try “All” at the top.</div>`));

    const sum = (k, list = ws) => list.reduce((a, w) => a + (w[k] || 0), 0);
    const mean = (k, list = ws) => { const v = list.filter((w) => w[k] != null); return v.length ? v.reduce((a, w) => a + w[k], 0) / v.length : null; };
    const totalS = ws.reduce((a, w) => a + durS(w), 0);
    const dist = ws.reduce((a, w) => a + realDist(w), 0);
    const swim = sel === "pool_swimming";
    const tiles = html(`<div class="tiles">
      ${tile("Sessions", fmt(ws.length), "", `${fmt(ws.length / Math.max(1, days / 7), 1)} / week`)}
      ${tile("Total time", hm(totalS / 60), "", `avg ${hm(totalS / 60 / ws.length)}`)}
      ${tile("Distance", dist ? fmt(swim ? dist : dist / 1000, swim ? 0 : 1) : "—", dist ? (swim ? "m" : "km") : "")}
      ${tile("Avg heart rate", fmt(mean("avg_hr")), "bpm", `max ${fmt(Math.max(...ws.map((w) => w.max_hr || 0)) || null)}`)}
      ${tile("Training load", fmt(sum("training_load")), "", `avg ${fmt(mean("training_load"))} / session`)}
      ${tile("Calories", fmt(sum("calories")), "kcal")}
    </div>`);

    // weekly time per sport (stacked)
    const weeks = new Map();
    for (const w of [...ws].sort((x, y) => x.start_ts - y.start_ts)) {
      const k = isoWeek(w.start_ts).getTime();
      if (!weeks.has(k)) weeks.set(k, {});
      weeks.get(k)[w.type] = (weeks.get(k)[w.type] || 0) + durS(w) / 3600;
    }
    const buckets = [...weeks.entries()].map(([k, parts]) => ({ label: new Date(k).toLocaleDateString([], { month: "short", day: "numeric" }), parts }));
    const shown = sel ? { [sel]: colors[sel] } : colors;

    // per-sport statistics
    const statRows = types.map((t) => {
      const g = all.filter((w) => w.type === t), secs = g.reduce((a, w) => a + durS(w), 0), d = g.reduce((a, w) => a + realDist(w), 0);
      const longest = g.reduce((m, w) => (durS(w) > durS(m) ? w : m), g[0]);
      return `<tr class="click" data-sport="${esc(t)}"><td><i class="sw" style="background:${colors[t]}"></i>${esc(sportLabel(t))}</td><td>${g.length}</td><td>${hm(secs / 60)}</td><td>${hm(secs / 60 / g.length)}</td>
        <td>${d ? (t.includes("swim") ? fmt(d) + " m" : fmt(d / 1000, 1) + " km") : "—"}</td><td>${fmt(mean("avg_hr", g))}</td><td>${fmt(mean("training_load", g))}</td><td>${fmt(mean("training_effect", g), 1)}</td><td>${hm(durS(longest) / 60)}</td><td>${zoneBar(sumZones(g))}</td></tr>`;
    }).join("");
    const stats = html(`<div class="tablewrap"><table><thead><tr><th>Sport</th><th>Sessions</th><th>Total time</th><th>Avg time</th><th>Distance</th><th>Avg HR</th><th>Avg load</th><th>Avg TE</th><th>Longest</th><th style="min-width:160px">HR zones</th></tr></thead><tbody>${statRows}</tbody></table>${zoneLegend()}</div>`);
    stats.addEventListener("click", (ev) => { const tr = ev.target.closest("tr[data-sport]"); if (tr) location.hash = `#/workouts/${encodeURIComponent(tr.dataset.sport)}`; });

    // trends for one sport
    const trend = [];
    if (sel) {
      const by = (f) => ws.filter((w) => f(w) != null).map((w) => [w.start_ts, f(w)]);
      trend.push(card("Avg heart rate per session", chart([{ points: by((w) => w.avg_hr), color: css("--hr") }], { unit: "bpm" })));
      trend.push(card("Duration per session", chart([{ points: by((w) => durS(w) / 60), color: colors[sel] }], { unit: "min", yMin: 0 })));
      if (dist && !swim) trend.push(card("Distance per session", chart([{ points: by((w) => (w.distance_m ? w.distance_m / 1000 : null)), color: css("--act") }], { unit: "km", digits: 1, yMin: 0 })));
      if (/run|walk|treadmill|hik/.test(sel)) trend.push(card("Pace (min/km, lower is faster)", chart([{ points: by((w) => (w.distance_m > 500 ? durS(w) / 60 / (w.distance_m / 1000) : null)), color: css("--body") }], { unit: "min/km", digits: 1 })));
      if (swim) trend.push(card("SWOLF (lower is better)", chart([{ points: by((w) => w.swolf), color: css("--body") }], { unit: "" })));
      trend.push(card("Training load per session", chart([{ points: by((w) => w.training_load), color: css("--act") }], { bars: true, yMin: 0 })));
      trend.push(card("Time in heart-rate zones", html(`<div>${zoneBar(sumZones(ws))}${zoneLegend()}</div>`)));
    }

    const list = html(`<div class="tablewrap"><table><thead><tr><th>Date</th><th>Sport</th><th>Duration</th><th>Distance</th><th>Pace</th><th>Avg HR</th><th>Load</th><th>Calories</th><th>Zones</th><th>GPS</th></tr></thead><tbody>
      ${ws.map((w) => `<tr class="click" data-id="${esc(w.workout_id)}"><td>${dt(w.start_ts)}</td><td><i class="sw" style="background:${colors[w.type]}"></i>${esc(sportLabel(w.type))}</td><td>${hm(durS(w) / 60)}</td><td>${w.distance_m >= 100 ? (w.type.includes("swim") ? fmt(w.distance_m) + " m" : fmt(w.distance_m / 1000, 2) + " km") : "—"}</td><td>${pace(w)}</td><td>${fmt(w.avg_hr)}</td><td>${fmt(w.training_load)}</td><td>${fmt(w.calories)}</td><td style="min-width:110px">${zoneBar(JSON.parse(w.hr_zones || "[]"))}</td><td>${w.route_n ? "✓" : ""}</td></tr>`).join("")}
    </tbody></table></div>`);
    list.addEventListener("click", (ev) => { const tr = ev.target.closest("tr[data-id]"); if (tr) location.hash = `#/workout/${tr.dataset.id}`; });

    mount(html(`<h1>Workouts${sel ? " · " + esc(sportLabel(sel)) : ""}</h1>`), chips, tiles, grid(
      card("Weekly training time (hours)", stackedBars(buckets, shown, { unit: "h", digits: 1 }), "wide"),
      ...(sel ? trend : [card("By sport", stats, "wide")]),
      card(`${ws.length} sessions`, list, "wide"),
    ), ...(all.some((w) => w.auto_detected) && !sel ? [html(`<p class="note">“Auto-detected activity” is movement the watch recognised on its own (no workout started), usually without distance or GPS.</p>`)] : []));
  },

  async workout(id) {
    const r = await fetch(`/api/workout/${encodeURIComponent(id)}`).then((x) => x.json());
    if (r.error) return mount(html(`<div class="empty">${esc(r.error)}</div>`));
    const w = r.workout;
    const col = (i) => r.samples.filter((s) => s[i] != null).map((s) => [s[0], s[i]]);
    const tiles = html(`<div class="tiles">
      ${tile("Duration", hm((w.moving_s || w.end_ts - w.start_ts) / 60))}
      ${tile("Distance", w.distance_m >= 100 ? (/swim/.test(w.type) ? fmt(w.distance_m) : fmt(w.distance_m / 1000, 2)) : "—", w.distance_m >= 100 ? (/swim/.test(w.type) ? "m" : "km") : "")}
      ${tile("Pace", pace(w))}
      ${tile("Avg / max HR", `${fmt(w.avg_hr)} / ${fmt(w.max_hr)}`, "bpm")}
      ${tile("Calories", fmt(w.calories), "kcal")}
      ${tile("Training load", fmt(w.training_load), "", w.training_effect ? `TE ${fmt(w.training_effect, 1)} aerobic${w.anaerobic_te ? ` · ${fmt(w.anaerobic_te, 1)} anaerobic` : ""}` : "")}
      ${w.rpe ? tile("Perceived effort", fmt(w.rpe), "/ 10") : ""}
      ${w.swolf ? tile("SWOLF", fmt(w.swolf), "", `${fmt(w.pool_laps)} lengths of ${fmt(w.pool_length_m)} m · ${fmt(w.swim_strokes)} strokes`) : ""}
    </div>`);
    const gpx = r.route.length ? `<a class="btn ghost" href="/api/gpx/${encodeURIComponent(id)}">Download GPX</a>` : "";
    const speed = col(2).map(([t, v]) => [t, v * 3.6]);
    mount(
      html(`<div class="row" style="justify-content:space-between"><h1>${esc(sportLabel(w.type))} · ${dt(w.start_ts)}</h1><div class="row"><a class="btn ghost" href="#/workouts">Back</a>${gpx}</div></div>`),
      tiles,
      grid(
        card("Route", routeSvg(r.route), r.route.length ? "" : ""),
        ...(w.hr_zones ? [card("Time in heart-rate zones", html(`<div>${zoneBar(JSON.parse(w.hr_zones))}${zoneLegend()}${JSON.parse(w.hr_zones).map((v, i) => (v ? `<div class="note">${ZONE_NAMES[i]}: ${hm(v / 60)}</div>` : "")).join("")}</div>`))] : []),
        card("Heart rate", chart([{ points: col(1), color: css("--hr") }], { gap: 60, unit: "bpm" })),
        ...(speed.length ? [card("Speed", chart([{ points: speed, color: css("--act") }], { gap: 60, unit: "km/h", digits: 1 }))] : []),
        ...(col(3).length ? [card("Cadence", chart([{ points: col(3), color: css("--sleep") }], { gap: 60, unit: "spm" }))] : []),
        ...(col(4).length ? [card("Power", chart([{ points: col(4), color: css("--body") }], { gap: 60, unit: "W" }))] : []),
      ),
      html(`<p class="note">Detail: ${esc(w.detail_status || "not fetched yet")}. The route is drawn without a background map on purpose: map tile servers would learn where you exercise.</p>`),
    );
  },

  async export() {
    const r = range();
    const link = (fmtName, label, extra = "") => `<a class="btn ghost" href="/api/export?${qs({ format: fmtName })}${extra}">${label}</a>`;
    const box = html(`<div>
      <div class="card" style="margin-bottom:14px"><h2>Files · ${r.from} → ${r.to}</h2>
        <div class="row">${link("json", "JSON (summary)")} ${link("json", "JSON (full series + routes)", "&full=1")} ${link("csv", "Daily CSV")} ${link("workouts_csv", "Workouts CSV")}</div>
        <p class="note">Change the range with the buttons at the top. GPX is on each workout page.</p></div>
      <div class="card"><h2>Package for an AI assistant</h2>
        <div class="row"><select id="tpl"><option value="summary">General summary</option><option value="sleep">Sleep analysis</option><option value="training">Training &amp; recovery</option></select>
        <button class="primary" id="mk">Build</button><button class="btn ghost" id="cp" disabled>Copy</button></div>
        <p class="note">Removes account, device, workout and sleep ids and every GPS coordinate. Read it before you paste it anywhere. Nothing is sent from here.</p>
        <textarea id="ai" readonly placeholder="Build to preview the exact text"></textarea></div></div>`);
    box.querySelector("#mk").onclick = async () => {
      const t = await fetch(`/api/export?${qs({ format: "ai", template: box.querySelector("#tpl").value })}`).then((x) => x.text());
      box.querySelector("#ai").value = t; box.querySelector("#cp").disabled = false;
    };
    box.querySelector("#cp").onclick = () => navigator.clipboard.writeText(box.querySelector("#ai").value);
    mount(html(`<h1>Export</h1>`), box);
  },

  async data() {
    const s = await fetch("/api/status").then((x) => x.json());
    const counts = Object.entries(s.counts).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${fmt(v)}</td></tr>`).join("");
    const cov = s.coverage.map((c) => `<tr><td>${esc(c.month)}</td><td><span class="pill ${esc(c.status)}">${esc(c.status)}</span></td><td>${esc(c.note)}</td><td>${esc(c.updated_at)}</td></tr>`).join("");
    const logs = s.last_sync.map((l) => `<tr><td>${esc(l.started_at)}</td><td>${esc(l.outcome)}</td><td>${esc(l.detail)}</td></tr>`).join("");
    mount(html(`<h1>Data</h1>`), grid(
      card("Account", html(`<div><p>${s.connected ? `Connected · ${esc(s.region)}` : "Not connected. Run <code>zepplocal login</code>."}</p><p class="note">Days held: ${esc(s.first_day ?? "—")} → ${esc(s.last_day ?? "—")}</p></div>`)),
      card("Rows", html(`<table><tbody>${counts}</tbody></table>`)),
      card("Coverage by month", html(`<div class="tablewrap"><table><thead><tr><th>Month</th><th>Status</th><th>Range</th><th>Updated</th></tr></thead><tbody>${cov || `<tr><td colspan=4 class="note">Nothing synced yet</td></tr>`}</tbody></table><p class="note">written = data stored · empty = Zepp returned nothing · partial = month not over yet · failed = retry with sync/backfill</p></div>`), "wide"),
      card("Recent syncs", html(`<div class="tablewrap"><table><tbody>${logs}</tbody></table></div>`), "wide"),
    ));
  },
};

// ------------------------------------------------------------------ router + sync
async function route() {
  const [, page = "overview", arg] = location.hash.split("/");
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.hash.split("/")[1] === (page === "workout" ? "workouts" : page)));
  document.querySelectorAll("#ranges button").forEach((b) => b.classList.toggle("on", Number(b.dataset.days) === days));
  view.innerHTML = `<div class="empty">Loading…</div>`;
  try { await (pages[page] || pages.overview)(arg && decodeURIComponent(arg)); }
  catch (e) { view.innerHTML = `<div class="empty">Could not load: ${esc(e.message)}</div>`; }
}
document.getElementById("ranges").addEventListener("click", (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  days = Number(b.dataset.days);
  try { localStorage.setItem("zl-days", String(days)); } catch (e) { /* ignore */ }
  route();
});

const syncBtn = document.getElementById("syncBtn"), bar = document.getElementById("syncBar");
async function pollSync() {
  const s = (await fetch("/api/status").then((x) => x.json())).sync;
  bar.hidden = !s.running && !s.result;
  bar.textContent = s.running ? s.log.join("\n") || "Starting…" : s.result ? `Sync ${s.result.outcome}: ${fmt(s.result.rows)} rows${s.result.errors?.length ? " · " + s.result.errors.slice(0, 3).join(" · ") : ""}` : "";
  syncBtn.disabled = s.running;
  if (s.running) setTimeout(pollSync, 1500); else if (s.result && syncBtn.dataset.wasRunning) { delete syncBtn.dataset.wasRunning; route(); }
  if (s.running) syncBtn.dataset.wasRunning = "1";
}
syncBtn.addEventListener("click", async () => {
  await fetch("/api/sync", { method: "POST", headers: { "X-ZL-Secret": SECRET } });
  syncBtn.dataset.wasRunning = "1";
  pollSync();
});

window.addEventListener("hashchange", route);
route();
pollSync();

// Theme: auto (follow macOS) → light → dark; remembered per browser.
const themeBtn = document.getElementById("themeBtn");
const THEMES = ["auto", "light", "dark"];
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  themeBtn.textContent = t[0].toUpperCase() + t.slice(1);
}
let theme = "auto";
try { theme = localStorage.getItem("zl-theme") || "auto"; } catch (e) { /* storage unavailable */ }
applyTheme(theme);
themeBtn.addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  applyTheme(theme);
  route();  // charts read colours at draw time
  try { localStorage.setItem("zl-theme", theme); } catch (e) { /* ignore */ }
});

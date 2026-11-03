"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, opts = {}) => {
  const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r.json();
};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fx = (v, d = 2) => v == null || isNaN(v) ? "—" : Number(v).toFixed(d);
const dur = s => { if (s == null) return "—"; s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")}` : `${m} min ${String(x).padStart(2, "0")}`; };
const fdate = ts => ts ? new Date(ts * 1000).toLocaleString("fr-FR", { dateStyle: "short", timeStyle: "short" }) : "";
const STATUS = { done: "terminé", running: "en cours", aborted: "arrêté", interrupted: "interrompu", error: "erreur" };

let META = { step_types: {}, limits: {} };
let PROGRAMS = [];
let STATUS_NOW = null;

// ---------------------------------------------------------------- onglets
$$("nav button").forEach(b => b.onclick = () => showTab(b.dataset.tab));
function showTab(t) {
  $$("nav button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach(s => s.classList.toggle("on", s.id === "tab-" + t));
  if (t === "history") loadHistory();
  if (t === "compare") loadCompare();
  if (t === "programs") renderProgList();
  setTimeout(resizeCharts, 30);
}

// ---------------------------------------------------------------- étapes
const FIELDS = {
  charge: [["current", "A", 0.01], ["voltage", "V", 0.01], ["cutoff", "A fin", 0.01], ["max_min", "min max", 1], ["max_ah", "Ah max", 0.01]],
  discharge_cc: [["current", "A", 0.01], ["cutoff_v", "V coupure", 0.01], ["max_min", "min max", 1]],
  discharge_cp: [["power", "W", 1], ["cutoff_v", "V coupure", 0.01], ["max_min", "min max", 1]],
  discharge_cr: [["resistance", "Ω", 0.1], ["cutoff_v", "V coupure", 0.01], ["max_min", "min max", 1]],
  rest: [["minutes", "min", 1]],
  loop: [["goto", "→ étape", 1], ["times", "fois", 1]],
};
const DEFAULTS = {
  charge: { current: 1, voltage: 4.2, cutoff: 0.1 }, discharge_cc: { current: 1, cutoff_v: 2.8 },
  discharge_cp: { power: 5, cutoff_v: 2.8 }, discharge_cr: { resistance: 4, cutoff_v: 2.8 },
  rest: { minutes: 30 }, loop: { goto: 1, times: 2 },
};
function describe(s) {
  const m = s.max_min ? ` · max ${s.max_min} min` : "";
  switch (s.type) {
    case "charge": return `${s.current} A → ${s.voltage} V, fin ${s.cutoff} A${m}${s.max_ah ? ` · max ${s.max_ah} Ah` : ""}`;
    case "discharge_cc": return `${s.current} A → ${s.cutoff_v} V${m}`;
    case "discharge_cp": return `${s.power} W → ${s.cutoff_v} V${m}`;
    case "discharge_cr": return `${s.resistance} Ω → ${s.cutoff_v} V${m}`;
    case "rest": return `${s.minutes} min`;
    case "loop": return `retour étape ${s.goto}, ${s.times}×`;
  }
  return "";
}
const stepLabel = s => META.step_types[s.type] || s.type;

// ---------------------------------------------------------------- graphes
function axisTime(u, vals) { return vals.map(v => { const h = Math.floor(v / 3600), m = Math.floor(v % 3600 / 60), s = Math.floor(v % 60);
  return h ? `${h}h${String(m).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`; }); }
function makeVIChart(el) {
  const grid = { stroke: css("--line"), width: 1 }, ticks = { stroke: css("--line") };
  const axis = { stroke: css("--muted"), grid, ticks };
  return new uPlot({
    width: el.clientWidth || 600, height: 360,
    scales: { x: { time: false }, V: { auto: true }, A: { auto: true, range: (u, mn, mx) => [Math.min(0, mn), Math.max(mx * 1.1, 0.1)] } },
    series: [{ label: "t" },
      { label: "Tension", scale: "V", stroke: css("--volt"), width: 2, value: (u, v) => fx(v, 3) + " V" },
      { label: "Courant", scale: "A", stroke: css("--amp"), width: 1.5, value: (u, v) => fx(v, 2) + " A" }],
    axes: [{ ...axis, values: axisTime }, { ...axis, scale: "V", values: (u, v) => v.map(x => +x.toFixed(3) + " V"), size: 64 },
      { ...axis, scale: "A", side: 1, grid: { show: false }, values: (u, v) => v.map(x => +x.toFixed(2) + " A"), size: 56 }],
    cursor: { drag: { x: true, y: false } },
  }, [[], [], []], el);
}
let liveChart, tdChart, cmpChart;
const live = { t: [], v: [], i: [], testId: null, t0: performance.now() / 1000 };
function resizeCharts() {
  for (const [c, el] of [[liveChart, "#liveChart"], [tdChart, "#tdChart"], [cmpChart, "#cmpChart"]])
    if (c && $(el).clientWidth) c.setSize({ width: $(el).clientWidth, height: 360 });
}
window.addEventListener("resize", resizeCharts);

// ---------------------------------------------------------------- direct
function setReadouts(st) {
  const f = st.frame, run = st.run, step = run && run.step;
  $("#roV").textContent = f ? fx(f.v, 3) : "—";
  $("#roI").textContent = f ? fx(f.i, 2) : "—";
  $("#roP").textContent = f ? fx(f.p, 2) : "—";
  $("#roC").textContent = step ? Math.round(step.cap * 1000) : (f ? Math.round(f.cap_dev * 1000) : "—");
  $("#roE").textContent = step ? fx(step.wh, 2) : "—";
  $("#roR").textContent = step && step.ir_mohm ? fx(step.ir_mohm, 0) : "—";
  $("#modeLabel").textContent = f ? `EBC : ${f.mode_label}` : "";
  $("#elapsed").textContent = run ? `test ${dur(run.elapsed)}${step ? ` · étape ${dur(step.elapsed)}` : ""}` : "";
  const dot = $("#dot");
  dot.className = "dot " + (st.connected ? (st.age > 3 ? "warn" : "ok") : "");
  $("#connText").textContent = st.connected ? st.model : (st.error || "déconnecté");
}

function setRunPanel(st) {
  const run = st.run;
  $("#runIdle").hidden = !!run;
  $("#runActive").hidden = !run;
  $("#btnStart").disabled = !st.connected;
  if (!run) return;
  $("#runTitle").textContent = `#${run.test_id} · ${run.name}${run.battery ? " · " + run.battery : ""}`;
  const curIdx = run.step ? run.step.index : -1;
  const doneIdx = new Set(run.results.map(r => r.step));
  $("#runSteps").innerHTML = run.steps.map((s, k) => {
    const res = [...run.results].reverse().find(r => r.step === k);
    const extra = k === curIdx ? `${dur(run.step.elapsed)} · ${Math.round(run.step.cap * 1000)} mAh`
      : res ? `${Math.round(res.capacity_ah * 1000)} mAh · ${res.end_reason}` : describe(s);
    return `<li class="${k === curIdx ? "cur" : doneIdx.has(k) ? "done" : ""}">${esc(stepLabel(s))}<small>${esc(extra)}</small></li>`;
  }).join("");
}

async function onStatus(st) {
  STATUS_NOW = st;
  setReadouts(st);
  setRunPanel(st);
  const tid = st.run ? st.run.test_id : null;
  if (tid !== live.testId) {
    live.testId = tid; live.t = []; live.v = []; live.i = [];
    if (tid) {
      $("#chartTitle").textContent = `Test #${tid}`;
      live.pending = [];
      const rows = await api(`/api/tests/${tid}/samples?max=4000`);
      const last = rows.length ? rows[rows.length - 1].t : -1;
      rows.concat(live.pending.filter(s => s.t > last)).forEach(r => { live.t.push(r.t); live.v.push(r.v); live.i.push(r.i); });
      live.pending = null;
    } else {
      $("#chartTitle").textContent = "Mesures (10 dernières minutes)";
      live.t0 = performance.now() / 1000;
    }
    liveChart.setData([live.t, live.v, live.i]);
  }
  if (!tid && st.frame) {
    const t = performance.now() / 1000 - live.t0;
    live.t.push(t); live.v.push(st.frame.v); live.i.push(st.frame.i);
    while (live.t.length && t - live.t[0] > 600) { live.t.shift(); live.v.shift(); live.i.shift(); }
    liveChart.setData([live.t, live.v, live.i]);
  }
}
function onSample(ev) {
  if (ev.test_id !== live.testId) return;
  if (live.pending) { live.pending.push(ev.s); return; }
  live.t.push(ev.s.t); live.v.push(ev.s.v); live.i.push(ev.s.i);
  liveChart.setData([live.t, live.v, live.i]);
}

function connectWS() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = m => {
    const ev = JSON.parse(m.data);
    if (ev.type === "status") onStatus(ev.status);
    else if (ev.type === "sample") onSample(ev);
    else if (ev.type === "log") addLog(ev.line);
    else if (ev.type === "test_finished") { loadBatteryList(); if ($("#tab-history").classList.contains("on")) loadHistory(); }
  };
  ws.onclose = () => { $("#dot").className = "dot"; $("#connText").textContent = "serveur injoignable"; setTimeout(connectWS, 2000); };
}
function addLog(line) { const p = $("#log"); p.textContent += line + "\n"; p.scrollTop = p.scrollHeight; }

function fillRunPrograms() {
  const sel = $("#runProgram"), cur = sel.value;
  sel.innerHTML = PROGRAMS.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join("");
  if (cur && PROGRAMS.some(p => p.id == cur)) sel.value = cur;
  previewRun();
}
function previewRun() {
  const p = PROGRAMS.find(p => p.id == $("#runProgram").value);
  $("#runPreview").innerHTML = p ? p.steps.map(s => `<li>${esc(stepLabel(s))}<small>${esc(describe(s))}</small></li>`).join("") : "";
}
$("#runProgram").onchange = previewRun;
$("#btnStart").onclick = async () => {
  $("#runErr").textContent = "";
  try {
    await api("/api/run", { method: "POST", body: { program_id: +$("#runProgram").value,
      battery: $("#runBattery").value, name: $("#runName").value, notes: $("#runNotes").value } });
    $("#runName").value = ""; $("#runNotes").value = "";
  } catch (e) { $("#runErr").textContent = e.message; }
};
$("#btnStop").onclick = () => confirm("Arrêter le test en cours ?") && api("/api/stop", { method: "POST" });
$("#btnSkip").onclick = () => confirm("Passer à l'étape suivante ?") && api("/api/skip", { method: "POST" });

async function loadBatteryList() {
  const tests = await api("/api/tests");
  const labels = [...new Set(tests.map(t => t.battery).filter(Boolean))];
  $("#batteryList").innerHTML = labels.map(l => `<option value="${esc(l)}">`).join("");
}

// ---------------------------------------------------------------- programmes
let editing = null; // {id, name, steps, builtin}
async function loadPrograms() { PROGRAMS = await api("/api/programs"); fillRunPrograms(); }
function renderProgList() {
  $("#progList").innerHTML = PROGRAMS.map(p => `<li data-id="${p.id}" class="${editing && editing.id === p.id ? "on" : ""}">
    ${esc(p.name)}<span>${p.steps.length} ét.${p.builtin ? " · prédéfini" : ""}</span></li>`).join("");
  $$("#progList li").forEach(li => li.onclick = () => editProgram(PROGRAMS.find(p => p.id == li.dataset.id)));
  if (!editing && PROGRAMS.length) editProgram(PROGRAMS[0]);
}
function editProgram(p) {
  editing = p ? JSON.parse(JSON.stringify(p)) : { id: null, name: "Nouveau programme", steps: [], builtin: 0 };
  $("#progErr").textContent = "";
  renderEditor();
  $$("#progList li").forEach(li => li.classList.toggle("on", editing.id && li.dataset.id == editing.id));
}
function renderEditor() {
  const ro = !!editing.builtin;
  $("#progName").value = editing.name; $("#progName").disabled = ro;
  $("#progRO").hidden = !ro;
  $("#btnSaveProg").disabled = ro; $("#btnDelProg").disabled = ro || !editing.id;
  $("#btnAddStep").disabled = ro; $("#addType").disabled = ro;
  $("#progSteps").innerHTML = editing.steps.map((s, k) => `<li data-k="${k}">
    <span class="type">${esc(stepLabel(s))}</span>
    ${FIELDS[s.type].map(([f, u, st]) => `<label class="fld"><input type="number" step="${st}" data-f="${f}" value="${s[f] ?? ""}" ${ro ? "disabled" : ""}>${u}</label>`).join("")}
    <span class="tools">${ro ? "" : `<button data-a="up" title="Monter">↑</button><button data-a="down" title="Descendre">↓</button><button data-a="del" title="Supprimer">✕</button>`}</span>
  </li>`).join("") || `<p class="muted">Aucune étape. Ajoute une charge, une pause, une décharge…</p>`;
  $$("#progSteps input").forEach(inp => inp.oninput = () => {
    const k = +inp.closest("li").dataset.k; const v = inp.value === "" ? null : +inp.value;
    if (v == null) delete editing.steps[k][inp.dataset.f]; else editing.steps[k][inp.dataset.f] = v;
  });
  $$("#progSteps button").forEach(b => b.onclick = () => {
    const k = +b.closest("li").dataset.k, st = editing.steps;
    if (b.dataset.a === "del") st.splice(k, 1);
    if (b.dataset.a === "up" && k > 0) [st[k - 1], st[k]] = [st[k], st[k - 1]];
    if (b.dataset.a === "down" && k < st.length - 1) [st[k + 1], st[k]] = [st[k], st[k + 1]];
    renderEditor();
  });
}
$("#progName").oninput = e => editing.name = e.target.value;
$("#btnNewProg").onclick = () => { editProgram(null); };
$("#btnAddStep").onclick = () => { const t = $("#addType").value; editing.steps.push({ type: t, ...DEFAULTS[t] }); renderEditor(); };
$("#btnCopyProg").onclick = () => { editing = { ...JSON.parse(JSON.stringify(editing)), id: null, builtin: 0, name: editing.name + " (copie)" }; renderEditor(); };
$("#btnSaveProg").onclick = async () => {
  $("#progErr").textContent = "";
  try {
    const body = { name: editing.name, steps: editing.steps };
    const r = editing.id ? await api(`/api/programs/${editing.id}`, { method: "PUT", body }) : await api("/api/programs", { method: "POST", body });
    await loadPrograms();
    editProgram(PROGRAMS.find(p => p.id === r.id)); renderProgList();
  } catch (e) { $("#progErr").textContent = e.message; }
};
$("#btnDelProg").onclick = async () => {
  if (!editing.id || !confirm(`Supprimer « ${editing.name} » ?`)) return;
  await api(`/api/programs/${editing.id}`, { method: "DELETE" });
  editing = null; await loadPrograms(); renderProgList();
};

// ---------------------------------------------------------------- historique
let TESTS = [], selected = new Set(), detailId = null;
async function loadHistory() { TESTS = await api("/api/tests"); renderHistory(); }
function renderHistory() {
  const q = $("#histFilter").value.toLowerCase();
  const rows = TESTS.filter(t => !q || `${t.battery} ${t.name} ${t.notes}`.toLowerCase().includes(q));
  $("#histTable tbody").innerHTML = rows.map(t => { const s = t.summary || {};
    return `<tr data-id="${t.id}" class="${t.id === detailId ? "on" : ""}">
      <td><input type="checkbox" data-sel="${t.id}" ${selected.has(t.id) ? "checked" : ""}></td>
      <td>${fdate(t.started)}</td><td>${esc(t.battery)}</td><td>${esc(t.name)}</td>
      <td><span class="st ${t.status}">${STATUS[t.status] || t.status}</span></td>
      <td class="n">${s.capacity_ah ? Math.round(s.capacity_ah * 1000) + " mAh" : "—"}</td>
      <td class="n">${s.energy_wh ? fx(s.energy_wh, 2) + " Wh" : "—"}</td>
      <td class="n">${s.ir_mohm ? fx(s.ir_mohm, 0) + " mΩ" : "—"}</td>
      <td class="n">${t.ended ? dur(t.ended - t.started) : "—"}</td></tr>`; }).join("")
    || `<tr><td colspan="9" class="muted">Aucun test pour l'instant.</td></tr>`;
  $$("#histTable tbody tr[data-id]").forEach(tr => tr.onclick = e => { if (e.target.dataset.sel) return; showDetail(+tr.dataset.id); });
  $$("#histTable [data-sel]").forEach(cb => cb.onchange = () => { const id = +cb.dataset.sel; cb.checked ? selected.add(id) : selected.delete(id); });
}
$("#histFilter").oninput = renderHistory;
$("#btnCompareSel").onclick = () => { if (!selected.size) return alert("Coche au moins un test."); cmpSel = new Set(selected); showTab("compare"); };

async function showDetail(id) {
  detailId = id; renderHistory();
  const t = await api(`/api/tests/${id}`);
  const rows = await api(`/api/tests/${id}/samples?max=4000`);
  $("#testDetail").hidden = false;
  $("#tdTitle").textContent = `#${t.id} · ${t.battery || "(sans étiquette)"} · ${t.name} · ${fdate(t.started)}`;
  $("#tdCsv").href = `/api/tests/${id}/csv`;
  $("#tdNotes").textContent = t.notes ? "Notes : " + t.notes : "";
  if (!tdChart) tdChart = makeVIChart($("#tdChart"));
  tdChart.setData([rows.map(r => r.t), rows.map(r => r.v), rows.map(r => r.i)]);
  setTimeout(resizeCharts, 30);
  $("#tdSteps tbody").innerHTML = t.steps.map(s => `<tr>
    <td>${s.step + 1}</td><td>${esc(META.step_types[s.type] || s.type)}</td><td>${esc(describe(s.params))}</td>
    <td class="n">${dur(s.t_end - s.t_start)}</td>
    <td class="n">${s.type === "rest" ? "" : Math.round(s.capacity_ah * 1000) + " mAh"}</td>
    <td class="n">${s.type === "rest" ? "" : fx(s.energy_wh, 2) + " Wh"}</td>
    <td class="n">${fx(s.v_start, 3)} → ${fx(s.v_end, 3)}</td>
    <td class="n">${s.ir_mohm ? fx(s.ir_mohm, 0) + " mΩ" : ""}</td><td>${esc(s.end_reason)}</td></tr>`).join("");
  $("#tdEdit").onclick = async () => {
    const battery = prompt("Étiquette batterie", t.battery || ""); if (battery == null) return;
    const name = prompt("Nom du test", t.name || ""); if (name == null) return;
    const notes = prompt("Notes", t.notes || ""); if (notes == null) return;
    await api(`/api/tests/${id}`, { method: "PATCH", body: { battery, name, notes } });
    await loadHistory(); showDetail(id); loadBatteryList();
  };
  $("#tdDel").onclick = async () => {
    if (!confirm(`Supprimer le test #${id} et ses mesures ?`)) return;
    try { await api(`/api/tests/${id}`, { method: "DELETE" }); } catch (e) { return alert(e.message); }
    $("#testDetail").hidden = true; detailId = null; selected.delete(id); loadHistory();
  };
}

// ---------------------------------------------------------------- comparaison
let cmpSel = new Set();
const PALETTE = ["#f5b13d", "#39c5d6", "#e46ba6", "#7bd17b", "#a58cf0", "#ef7d4a", "#5aa8f0", "#d8d35a"];
async function loadCompare() {
  const bats = await api("/api/batteries");
  if (!cmpSel.size) bats.slice(0, 4).forEach(b => cmpSel.add(b.last.test_id));
  $("#batTable tbody").innerHTML = bats.map((b, k) => { const l = b.last;
    return `<tr class="${k === 0 ? "rank1" : ""}">
      <td><input type="checkbox" data-cmp="${l.test_id}" ${cmpSel.has(l.test_id) ? "checked" : ""}></td>
      <td>${k + 1}</td><td>${esc(b.battery)}</td>
      <td class="n">${Math.round(l.capacity_ah * 1000)} mAh</td><td class="n">${fx(l.energy_wh, 2)} Wh</td>
      <td class="n">${l.ir_mohm ? fx(l.ir_mohm, 0) + " mΩ" : "—"}</td><td class="n">${fx(l.v_avg, 3)} V</td>
      <td class="n">${Math.round(b.best_ah * 1000)} mAh</td><td class="n">${b.tests}</td><td>${fdate(l.date)}</td></tr>`; }).join("")
    || `<tr><td colspan="10" class="muted">Aucun test terminé avec une décharge.</td></tr>`;
  $$("#batTable [data-cmp]").forEach(cb => cb.onchange = () => { const id = +cb.dataset.cmp; cb.checked ? cmpSel.add(id) : cmpSel.delete(id); drawCompare(); });
  drawCompare();
}
async function drawCompare() {
  const ids = [...cmpSel];
  const data = ids.length ? await api(`/api/compare?ids=${ids.join(",")}`) : [];
  // union des abscisses (Ah), chaque série interpolée sur sa propre plage
  const xs = [...new Set(data.flatMap(d => d.curve.map(p => p[0])))].sort((a, b) => a - b);
  const ys = data.map(d => { const m = new Map(d.curve.map(p => [p[0], p[1]])); return xs.map(x => m.has(x) ? m.get(x) : null); });
  if (cmpChart) { cmpChart.destroy(); cmpChart = null; }
  const el = $("#cmpChart"); el.innerHTML = "";
  const axis = { stroke: css("--muted"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") } };
  cmpChart = new uPlot({
    width: el.clientWidth || 600, height: 360, scales: { x: { time: false } },
    series: [{ label: "Ah" }, ...data.map((d, k) => ({ label: d.battery || d.name, stroke: PALETTE[k % 8], width: 2, spanGaps: true }))],
    axes: [{ ...axis, values: (u, v) => v.map(x => Math.round(x * 1000) + " mAh") },
      { ...axis, values: (u, v) => v.map(x => x.toFixed(2) + " V"), size: 64 }],
  }, [xs, ...ys], el);
  $("#cmpLegend").innerHTML = data.map((d, k) => `<i style="background:${PALETTE[k % 8]}"></i>${esc(d.battery || d.name)} #${d.id}`).join("");
}

// ---------------------------------------------------------------- init
(async function init() {
  META = await api("/api/meta");
  $("#simBadge").hidden = !META.simulator;
  $("#addType").innerHTML = Object.entries(META.step_types).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  liveChart = makeVIChart($("#liveChart"));
  (await api("/api/log")).forEach(addLog);
  await loadPrograms();
  loadBatteryList();
  connectWS();
})();

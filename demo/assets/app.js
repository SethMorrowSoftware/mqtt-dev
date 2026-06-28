/* ===========================================================================
   Automation (Conditions → Actions) · demo behaviour
   All client-side, no backend. Three pages share this file; each block runs
   only if the elements it needs are present on the page. Mirrors the live
   webui.py UI (dashboard with variables + manual control, the rule builder,
   settings) but everything is mock data — nothing is published or saved.
   =========================================================================== */
"use strict";

/* ---- shared helpers ----------------------------------------------------- */
function toast(text, isErr) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = text;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = isErr ? "err" : ""; }, 3200);
}
function setText(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; }
function esc(s) { const d = document.createElement("div"); d.textContent = String(s); return d.innerHTML; }
const fmt = v => (v === null || v === undefined) ? "—"
  : (v === true ? "yes" : (v === false ? "no" : v));
function isoNow() { return new Date().toISOString().replace(/\.\d+Z$/, "Z"); }
function agoText(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso); if (isNaN(t)) return iso;
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (s < 5) return "just now";
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

/* =========================================================================
   DASHBOARD  ·  conditions + device states + variables + manual control
   ========================================================================= */
(function dashboard() {
  if (!document.getElementById("devicegrid")) return;

  // Base "weather" the demo nudges around; scenarios override pieces of it.
  const SCENARIOS = {
    dry:     { is_raining: false, precip_accum_in: 0.00, precipitation_probability: 10,
               temperature: 72, humidity: 45, wind_speed_mph: 6,
               short_forecast: "Sunny", active_alerts: [], connected: true },
    wet:     { is_raining: true,  precip_accum_in: 0.31, precipitation_probability: 90,
               temperature: 58, humidity: 88, wind_speed_mph: 9,
               short_forecast: "Light Rain", active_alerts: ["Flood Watch"], connected: true },
    accum:   { is_raining: false, precip_accum_in: 0.42, precipitation_probability: 30,
               temperature: 63, humidity: 70, wind_speed_mph: 7,
               short_forecast: "Mostly Cloudy", active_alerts: [], connected: true },
    freeze:  { is_raining: false, precip_accum_in: 0.00, precipitation_probability: 20,
               temperature: 29, humidity: 60, wind_speed_mph: 12,
               short_forecast: "Clear and Cold", active_alerts: ["Freeze Warning"], connected: true },
    offline: { is_raining: false, precip_accum_in: 0.05, precipitation_probability: 15,
               temperature: 66, humidity: 52, wind_speed_mph: 8,
               short_forecast: "Partly Cloudy", active_alerts: [], connected: false },
  };
  const LOOKBACK = 24;
  const MANUAL_CONTROL = true;             // demo: controls are enabled
  let current = "wet";
  // operator-set variables (toggled below) and per-device manual overrides
  let variables = [
    { name: "maintenance_mode", type: "bool",   value: false },
    { name: "temp_setpoint",    type: "number", value: 70 },
  ];
  let manual = { irrigation_rain_inhibit: "auto", maintenance_hold: "auto", any_nws_alert: "auto" };
  let lastChange = {};
  let prevActive = {};

  function round(n, d) { const p = Math.pow(10, d); return Math.round(n * p) / p; }
  function varVal(name) { const v = variables.find(x => x.name === name); return v ? v.value : undefined; }

  // Re-derive device state the way the monitor does: rules -> desired, then a
  // manual override (on/off) wins.
  function deriveRules(m) {
    const rules = [
      { name: "irrigation_rain_inhibit", description: "Hold irrigation when raining or ≥ 0.25 in / 24h",
        topic: "irrigation/rain_inhibit", on_match: "INHIBIT", on_clear: "ALLOW", enabled: true,
        desired: m.is_raining || m.precip_accum_in >= 0.25 },
      { name: "maintenance_hold", description: "Pause everything while in maintenance mode",
        topic: "facility/maintenance", on_match: "ON", on_clear: "OFF", enabled: true,
        desired: !!varVal("maintenance_mode") },
      { name: "any_nws_alert", description: "Flag whenever any NWS alert is active",
        topic: "facility/weather/nws_alert", on_match: "1", on_clear: "0", enabled: true,
        desired: m.active_alerts.length > 0 },
    ];
    for (const r of rules) {
      const man = manual[r.name] || "auto";
      r.manual = man;
      r.active = !r.enabled ? null : (man === "on" ? true : man === "off" ? false : r.desired);
      if (prevActive[r.name] !== undefined && prevActive[r.name] !== r.active) lastChange[r.name] = isoNow();
      if (lastChange[r.name] === undefined) lastChange[r.name] = isoNow();
      prevActive[r.name] = r.active;
      r.current_payload = r.active == null ? null : (r.active ? r.on_match : r.on_clear);
      r.last_change = lastChange[r.name];
    }
    return rules;
  }

  function buildState() {
    const s = SCENARIOS[current];
    const d = (base, amp, dec) => round(base + (Math.random() - 0.5) * amp, dec);
    const m = {
      is_raining: s.is_raining,
      precip_accum_in: round(s.precip_accum_in, 2),
      precipitation_probability: Math.max(0, Math.min(100, Math.round(d(s.precipitation_probability, 6, 0)))),
      temperature: d(s.temperature, 1.5, 1),
      humidity: Math.max(0, Math.min(100, Math.round(d(s.humidity, 4, 0)))),
      wind_speed_mph: Math.max(0, Math.round(d(s.wind_speed_mph, 3, 0))),
      short_forecast: s.short_forecast,
      active_alerts: s.active_alerts,
    };
    return { updated: isoNow(), lookback_hours: LOOKBACK, mqtt_connected: s.connected,
             manual_control: MANUAL_CONTROL, metrics: m, rules: deriveRules(m),
             variables: variables.map(v => ({ ...v })) };
  }

  function ctlButtons(r) {
    const cur = r.manual || "auto";
    const mk = (st, lbl) => '<button type="button" class="mini ' + (cur === st ? "" : "secondary") +
      '" data-state="' + st + '" style="margin:0;padding:5px 11px;font-size:12px">' + lbl + '</button>';
    return '<div class="ctl" data-device="' + esc(r.name) +
      '" style="display:flex;gap:5px;margin-top:8px">' + mk("auto", "Auto") + mk("on", "On") + mk("off", "Off") + '</div>';
  }

  function renderVars(vars, manualControl) {
    const card = document.getElementById("vars-card");
    const box = document.getElementById("vars-body");
    if (!card || !box) return;
    if (!vars.length) { card.style.display = "none"; return; }
    card.style.display = "";
    box.innerHTML = "";
    for (const v of vars) {
      let ctrl;
      if (!manualControl) {
        ctrl = '<span class="v">' + esc(fmt(v.value)) + "</span>";
      } else if (v.type === "bool") {
        const on = v.value === true;
        ctrl = '<button type="button" class="mini ' + (on ? "" : "secondary") + '" data-var="' + esc(v.name) +
          '" data-next="' + (on ? "false" : "true") + '" style="margin:0">' + (on ? "ON" : "OFF") + "</button>";
      } else {
        ctrl = '<input class="var-num" data-var="' + esc(v.name) + '" type="number" step="any" value="' +
          (v.value != null ? esc(v.value) : "") + '" style="width:120px;margin:0">';
      }
      const cell = document.createElement("div"); cell.className = "metric";
      cell.style.cssText = "display:flex;justify-content:space-between;align-items:center;gap:10px";
      cell.innerHTML = '<div class="k">' + esc(v.name) + "</div><div>" + ctrl + "</div>";
      box.appendChild(cell);
    }
  }

  function render(s) {
    const conn = document.getElementById("connstate");
    const up = !!s.mqtt_connected;
    conn.innerHTML = '<span class="dot ' + (up ? "up" : "down") + '"></span>MQTT ' + (up ? "connected" : "offline");

    // Headline device: prefer the irrigation rule (back-compat), else first
    // enabled rule with a known state, else the first rule.
    const rules = s.rules;
    const irr = rules.find(r => r.enabled !== false && /irrigation|rain_inhibit/.test(r.name || ""))
             || rules.find(r => r.enabled !== false && r.active !== null && r.active !== undefined)
             || rules[0];
    const dEl = document.getElementById("directive");
    const card = document.getElementById("directive-card");
    let st = "unknown";
    if (irr && irr.active !== null && irr.active !== undefined) {
      const isIrr = /irrigation|rain_inhibit/.test(irr.name || "");
      st = irr.active ? "inhibit" : "allow";
      dEl.className = "big " + st;
      const suffix = isIrr ? (irr.active ? " — do NOT water" : " — watering allowed")
                           : (irr.active ? " — active" : " — clear");
      setText("directive", irr.current_payload + suffix);
      setText("directive-sub", "topic " + irr.topic + (irr.last_change ? " · changed " + agoText(irr.last_change) : ""));
    } else {
      dEl.className = "big unknown"; setText("directive", "UNKNOWN");
      setText("directive-sub", irr ? "Waiting on data…" : "No rules configured.");
    }
    if (card) card.className = "card state-" + st;

    const m = s.metrics;
    const up2 = document.getElementById("updated");
    if (up2) { up2.textContent = "updated " + agoText(s.updated); up2.title = s.updated; }
    setText("m_rain", fmt(m.is_raining));
    setText("m_accum", fmt(m.precip_accum_in) + " in");
    setText("m_accum_k", "rain last " + s.lookback_hours + "h");
    setText("m_prob", fmt(m.precipitation_probability) + "%");
    setText("m_temp", fmt(m.temperature) + "°F");
    setText("m_hum", fmt(m.humidity) + "%");
    setText("m_wind", fmt(m.wind_speed_mph));
    const alerts = m.active_alerts.length ? m.active_alerts.join(", ") : "none";
    setText("forecast", m.short_forecast + " · alerts: " + alerts);

    renderVars(s.variables || [], !!s.manual_control);

    const grid = document.getElementById("devicegrid");
    grid.innerHTML = "";
    for (const r of rules) {
      let pill;
      if (r.enabled === false) pill = '<span class="pill na">disabled</span>';
      else if (r.active === null || r.active === undefined) pill = '<span class="pill na">n/a</span>';
      else if (r.active) pill = '<span class="pill on">active</span>';
      else pill = '<span class="pill off">clear</span>';
      if (r.manual && r.manual !== "auto") pill += ' <span class="pill na">manual ' + esc(r.manual) + "</span>";
      const cell = document.createElement("div");
      cell.className = "metric"; cell.style.cssText = "display:flex;flex-direction:column;gap:6px";
      let html = '<div class="toprow" style="align-items:center"><strong>' + esc(r.name) + "</strong><span>" + pill + "</span></div>";
      if (r.description) html += '<div class="muted" style="font-size:12px">' + esc(r.description) + "</div>";
      html += '<div class="muted" style="font-size:12px">topic <code>' + esc(r.topic) + "</code></div>";
      html += '<div class="muted" style="font-size:12px">payload ' + (r.current_payload != null ? esc(r.current_payload) : "—") +
        " · changed " + esc(agoText(r.last_change)) + "</div>";
      if (s.manual_control && r.enabled !== false) html += ctlButtons(r);
      cell.innerHTML = html;
      grid.appendChild(cell);
    }
    const dash = document.getElementById("dash");
    if (dash) dash.classList.remove("loading");
  }

  function tick() { render(buildState()); }

  // Demo control wiring: manual device buttons + variable toggles mutate the
  // mock state in place (no network) and re-render.
  document.getElementById("devicegrid").addEventListener("click", e => {
    const b = e.target.closest("button[data-state]"); if (!b) return;
    const wrap = b.closest(".ctl"); if (!wrap) return;
    manual[wrap.getAttribute("data-device")] = b.getAttribute("data-state");
    tick(); toast("Manual: " + wrap.getAttribute("data-device") + " → " + b.getAttribute("data-state"));
  });
  const vb = document.getElementById("vars-body");
  if (vb) {
    vb.addEventListener("click", e => {
      const b = e.target.closest("button[data-var]"); if (!b) return;
      const v = variables.find(x => x.name === b.getAttribute("data-var"));
      if (v) { v.value = b.getAttribute("data-next") === "true"; tick(); toast(v.name + " = " + v.value); }
    });
    vb.addEventListener("change", e => {
      const i = e.target.closest("input.var-num[data-var]"); if (!i) return;
      const v = variables.find(x => x.name === i.getAttribute("data-var"));
      if (v) { v.value = Number(i.value); toast(v.name + " = " + v.value); }
    });
  }

  document.querySelectorAll(".chip[data-scn]").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chip[data-scn]").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      current = btn.dataset.scn;
      tick();
      toast("Scenario: " + btn.textContent.trim());
    });
  });

  tick();
  setInterval(tick, 4000);
})();

/* =========================================================================
   SETTINGS  ·  client-side validation mirroring the server's range checks
   ========================================================================= */
(function settings() {
  const form = document.getElementById("settings-form");
  if (!form) return;

  function validateField(el) {
    const errBox = el.parentElement.querySelector(":scope > .field-err");
    const raw = (el.value || "").trim();
    const type = el.dataset.type;          // "num" | "int" | undefined
    let err = "";
    if (raw === "") {
      if (type || el.dataset.required) err = "Required.";
    } else if (type) {
      const n = Number(raw);
      if (isNaN(n)) err = "Must be a number.";
      else if (type === "int" && !Number.isInteger(n)) err = "Must be a whole number.";
      else if (el.dataset.min !== undefined && n < Number(el.dataset.min)) err = "Min " + el.dataset.min + ".";
      else if (el.dataset.max !== undefined && n > Number(el.dataset.max)) err = "Max " + el.dataset.max + ".";
    }
    if (errBox) errBox.textContent = err;
    el.classList.toggle("invalid", !!err);
    return !err;
  }

  form.querySelectorAll("input[data-type],input[data-required]").forEach(el => {
    el.addEventListener("input", () => validateField(el));
    el.addEventListener("blur", () => validateField(el));
  });

  // Manual control requires a login — mirror the server's fail-closed refusal.
  function manualGuard() {
    const amc = form.querySelector("[name=web_allow_manual_control]");
    if (!amc) return true;
    if (amc.value !== "true") return true;
    const u = (form.querySelector("[name=web_username]") || {}).value || "";
    const p = (form.querySelector("[name=web_password]") || {}).value || "";
    return !!(u.trim() && p.trim());
  }

  form.addEventListener("submit", e => {
    e.preventDefault();
    let ok = true;
    form.querySelectorAll("input[data-type],input[data-required]").forEach(el => {
      if (!validateField(el)) ok = false;
    });
    if (!ok) { toast("Could not save: fix the highlighted fields.", true); return; }
    if (!manualGuard()) { toast("Manual control needs a web login (set a username and password).", true); return; }
    toast("Settings saved (demo — nothing was written).");
  });
})();

/* =========================================================================
   RULES  ·  structured form builder + lightweight YAML-shape validator
   Mirrors the live webui.py Rules page; client-side only (no persistence).
   ========================================================================= */
(function rules() {
  const form = document.getElementById("rules-form");
  if (!form) return;

  const NUM = ["<", "<=", ">", ">=", "==", "!=", "between", "in", "changed"];
  const BOOLO = ["==", "!=", "changed"];
  const TXT = ["contains", "equals", "in", "changed"];
  // Built-ins + schedule metrics + a few "discovered" dynamic metrics (as if a
  // config declared variables / mqtt_in / http_poll), so the dropdowns show the
  // same dynamic discovery the live builder does.
  const METRICS = {
    is_raining:                { type: "bool",   ops: ["==", "!=", "changed"] },
    precip_accum_in:           { type: "number", ops: NUM },
    precipitation_probability: { type: "number", ops: NUM },
    temperature:               { type: "number", ops: NUM },
    wind_speed_mph:            { type: "number", ops: NUM },
    humidity:                  { type: "number", ops: NUM },
    short_forecast:            { type: "text",   ops: TXT },
    active_alert:              { type: "alert",  ops: ["any", "contains", "equals"] },
    time_hour:                 { type: "number", ops: NUM },
    time_minute:               { type: "number", ops: NUM },
    time_weekday:              { type: "text",   ops: TXT },
    time_is_weekend:           { type: "bool",   ops: BOOLO },
    time_is_daytime:           { type: "bool",   ops: BOOLO },
    var_maintenance_mode:      { type: "bool",   ops: BOOLO },
    var_temp_setpoint:         { type: "number", ops: NUM },
    tank_level:                { type: "number", ops: NUM },
    power_kw:                  { type: "number", ops: NUM },
  };
  const METRIC_NAMES = Object.keys(METRICS);
  const builder = document.getElementById("builder");

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
  function opt(v, label, sel) { const o = document.createElement("option"); o.value = v; o.textContent = label || v; if (sel) o.selected = true; return o; }

  function valueControl(metric, operator, value) {
    const meta = METRICS[metric] || { type: "text" };
    let c;
    if (operator === "between") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = "low, high";
    } else if (operator === "in") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = meta.type === "number" ? "e.g. 30, 50, 70" : "a, b, c";
    } else if (meta.type === "bool") {
      c = document.createElement("select"); c.className = "c-val";
      c.appendChild(opt("true", "true", String(value) === "true"));
      c.appendChild(opt("false", "false", String(value) !== "true"));
    } else if (meta.type === "number") {
      c = document.createElement("input"); c.className = "c-val"; c.type = "number"; c.step = "any";
      c.value = value != null ? value : ""; c.placeholder = "number";
    } else {
      c = document.createElement("input"); c.className = "c-val"; c.type = "text";
      c.value = value != null ? value : ""; c.placeholder = "text";
    }
    c.setAttribute("aria-label", "condition value");
    return c;
  }
  function fillOps(sel, metric, chosen) {
    sel.innerHTML = "";
    const ops = (METRICS[metric] || { ops: [] }).ops;
    ops.forEach(o => sel.appendChild(opt(o, o, o === chosen)));
    if (!ops.includes(chosen) && ops.length) sel.value = ops[0];
  }
  function condRow(cond) {
    cond = cond || { metric: METRIC_NAMES[0], operator: "", value: "", for: "" };
    const row = el("div", "cond row");
    const metricWrap = el("div"); const m = document.createElement("select"); m.className = "c-metric";
    m.setAttribute("aria-label", "metric");
    METRIC_NAMES.forEach(n => m.appendChild(opt(n, n, n === cond.metric)));
    if (!METRICS[cond.metric]) m.value = METRIC_NAMES[0];
    metricWrap.appendChild(m);
    const opWrap = el("div"); const o = document.createElement("select"); o.className = "c-op";
    o.setAttribute("aria-label", "operator");
    fillOps(o, m.value, cond.operator); opWrap.appendChild(o);
    const valWrap = el("div", "c-val-wrap"); valWrap.appendChild(valueControl(m.value, o.value, cond.value));
    const forWrap = el("div", "c-for-wrap"); const f = document.createElement("input"); f.className = "c-for"; f.type = "text";
    f.placeholder = "for (e.g. 10m)"; f.value = cond.for || ""; f.setAttribute("aria-label", "sustain duration");
    f.title = "optional: the condition must hold continuously this long (e.g. 30s, 10m, 2h)";
    forWrap.appendChild(f);
    const rmWrap = el("div", "rm"); const rm = el("button", "secondary danger mini", "×"); rm.type = "button"; rmWrap.appendChild(rm);
    function noValue() { const meta = METRICS[m.value] || {}; return o.value === "changed" || (meta.type === "alert" && o.value === "any"); }
    function syncValVisible() { valWrap.style.display = noValue() ? "none" : ""; }
    function rebuildVal(keep) { valWrap.innerHTML = ""; valWrap.appendChild(valueControl(m.value, o.value, keep)); syncValVisible(); }
    m.addEventListener("change", () => { fillOps(o, m.value, o.value); rebuildVal(null); });
    o.addEventListener("change", () => rebuildVal(null));
    rm.addEventListener("click", () => { const card = row.closest(".rule-card"); row.remove(); refreshCombine(card); });
    syncValVisible();
    row.appendChild(metricWrap); row.appendChild(opWrap); row.appendChild(valWrap); row.appendChild(forWrap); row.appendChild(rmWrap);
    return row;
  }
  function refreshCombine(card) {
    if (!card) return;
    card.querySelector(".combine-wrap").style.display = card.querySelectorAll(".cond").length > 1 ? "" : "none";
  }
  function ruleCard(rule) {
    rule = rule || { name: "", description: "", topic: "", on_match: "", on_clear: "", enabled: true, combine: "any", conditions: [] };
    const card = el("div", "rule-card");
    card.innerHTML =
      '<div class="rhead"><span class="idx"></span>' +
      '<label class="enabled-lbl" style="display:flex;align-items:center;gap:7px;margin:0;font-weight:600" ' +
      'title="Disabled rules are not evaluated and publish nothing">' +
      '<input type="checkbox" class="f-enabled" style="width:auto;margin:0"> enabled</label></div>' +
      '<div class="row"><div><label>Name <input class="f-name"></label></div>' +
      '<div><label>Topic <input class="f-topic"></label></div></div>' +
      '<label>Description <span class="hint">(optional)</span> <input class="f-desc"></label>' +
      '<div class="row"><div><label>Payload when matched <span class="hint">(on_match)</span> <input class="f-onmatch"></label></div>' +
      '<div><label>Payload when cleared <span class="hint">(on_clear, optional)</span> <input class="f-onclear"></label></div></div>' +
      '<div class="combine-wrap"><label>When there are multiple conditions, match' +
      ' <select class="f-combine"></select></label></div>' +
      '<label style="margin-top:14px">Conditions</label><div class="conds"></div>' +
      '<div class="btnrow"><button type="button" class="secondary mini add-cond">+ Add condition</button>' +
      '<button type="button" class="danger mini remove-rule">Remove rule</button></div>';
    card.querySelector(".f-name").value = rule.name || "";
    card.querySelector(".f-topic").value = rule.topic || "";
    card.querySelector(".f-desc").value = rule.description || "";
    card.querySelector(".f-onmatch").value = rule.on_match || "";
    card.querySelector(".f-onclear").value = rule.on_clear || "";
    card.querySelector(".f-enabled").checked = rule.enabled !== false;
    const comb = card.querySelector(".f-combine");
    comb.appendChild(opt("any", "ANY is true (OR)", rule.combine !== "all"));
    comb.appendChild(opt("all", "ALL are true (AND)", rule.combine === "all"));
    const conds = card.querySelector(".conds");
    (rule.conditions && rule.conditions.length ? rule.conditions : [null]).forEach(c => conds.appendChild(condRow(c)));
    card.querySelector(".add-cond").addEventListener("click", () => { conds.appendChild(condRow()); refreshCombine(card); });
    card.querySelector(".remove-rule").addEventListener("click", () => { card.remove(); reindex(); });
    refreshCombine(card);
    return card;
  }
  function reindex() {
    [...builder.querySelectorAll(".rule-card")].forEach((c, i) => {
      c.querySelector(".idx").textContent = "Rule " + (i + 1) + (i === 0 ? " · headline" : "");
    });
  }
  function collect() {
    return [...builder.querySelectorAll(".rule-card")].map(card => {
      const conds = [...card.querySelectorAll(".cond")].map(row => {
        const metric = row.querySelector(".c-metric").value;
        const operator = row.querySelector(".c-op").value;
        const meta = METRICS[metric] || {};
        const noVal = operator === "changed" || (meta.type === "alert" && operator === "any");
        let value = "";
        if (!noVal) { const ctrl = row.querySelector(".c-val"); value = ctrl ? ctrl.value : ""; }
        const forv = (row.querySelector(".c-for").value || "").trim();
        return { metric, operator, value, for: forv };
      });
      return {
        name: card.querySelector(".f-name").value.trim(),
        description: card.querySelector(".f-desc").value.trim(),
        topic: card.querySelector(".f-topic").value.trim(),
        on_match: card.querySelector(".f-onmatch").value,
        on_clear: card.querySelector(".f-onclear").value,
        enabled: card.querySelector(".f-enabled").checked,
        combine: card.querySelector(".f-combine").value,
        conditions: conds,
      };
    });
  }
  function validateForm(data) {
    if (!data.length) return "Add at least one rule.";
    const durRe = /^\d+(\.\d+)?\s*[smh]?$/;
    for (let i = 0; i < data.length; i++) {
      const r = data[i], label = "Rule " + (i + 1);
      if (!r.name) return label + ": name is required.";
      if (!r.topic) return "Rule '" + r.name + "': topic is required.";
      if (r.on_match === "") return "Rule '" + r.name + "': the on_match payload is required.";
      if (!r.conditions.length) return "Rule '" + r.name + "': add at least one condition.";
      for (const c of r.conditions) {
        const meta = METRICS[c.metric] || {};
        if (c.for && !durRe.test(c.for.trim())) return "Rule '" + r.name + "': '" + c.metric + "' for must be a duration like 10m, 30s, 2h.";
        if (c.operator === "changed") continue;
        if (meta.type === "alert" && c.operator === "any") continue;
        if (c.operator === "between") {
          const ps = c.value.split(",").map(s => s.trim()).filter(s => s !== "");
          if (ps.length !== 2 || ps.some(p => isNaN(Number(p)))) return "Rule '" + r.name + "': " + c.metric + " between needs two numbers 'low, high'.";
          continue;
        }
        if (c.operator === "in") {
          const ps = c.value.split(",").map(s => s.trim()).filter(s => s !== "");
          if (!ps.length) return "Rule '" + r.name + "': " + c.metric + " in needs at least one value.";
          if (meta.type === "number" && ps.some(p => isNaN(Number(p)))) return "Rule '" + r.name + "': " + c.metric + " in needs numeric values.";
          continue;
        }
        if (c.value === "") return "Rule '" + r.name + "': the " + c.metric + " condition needs a value.";
        if (meta.type === "number" && isNaN(Number(c.value))) return "Rule '" + r.name + "': " + c.metric + " needs a numeric value.";
      }
    }
    return "";
  }

  document.getElementById("add-rule").addEventListener("click", () => { builder.appendChild(ruleCard()); reindex(); });
  document.querySelectorAll(".tab").forEach(t => t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.getElementById("tab-form").style.display = t.dataset.tab === "form" ? "" : "none";
    document.getElementById("tab-yaml").style.display = t.dataset.tab === "yaml" ? "" : "none";
  }));

  /* ---- YAML tab (heuristic shape check) --------------------------------- */
  const ta = document.getElementById("rules_yaml");
  const errBox = document.getElementById("rules-err");
  function validateYaml(text) {
    if (!text.trim()) return "Rules list is empty.";
    const lines = text.replace(/\t/g, "  ").split("\n");
    const items = []; let cur = null, startLine = 0;
    lines.forEach((ln, i) => {
      if (/^- /.test(ln)) { if (cur !== null) items.push({ text: cur, line: startLine }); cur = ln + "\n"; startLine = i + 1; }
      else if (cur !== null) { cur += ln + "\n"; }
    });
    if (cur !== null) items.push({ text: cur, line: startLine });
    if (!items.length) return "Expected a YAML list (each rule starts with '- ').";
    for (let k = 0; k < items.length; k++) {
      const blk = items[k].text.replace(/^- /, "  ");
      const label = "rule #" + (k + 1) + " (line " + items[k].line + ")";
      for (const key of ["name", "when", "topic", "on_match"])
        if (!new RegExp("(^|\\n)\\s*" + key + ":").test(blk)) return label + ": missing '" + key + "'.";
      const mm = blk.match(/metric:\s*([A-Za-z_0-9]+)/g) || [];
      if (!mm.length) return label + ": needs at least one 'metric:' under 'when'.";
      for (const x of mm) { const n = x.split(":")[1].trim(); if (!METRIC_NAMES.includes(n)) return label + ": unknown metric '" + n + "'."; }
      const pl = blk.match(/(on_match|on_clear):\s*([^\n#]*)/g) || [];
      for (const p of pl) {
        const v = p.split(":").slice(1).join(":").trim();
        if (/^(on|off|yes|no|true|false)$/i.test(v)) return label + ": quote the payload \"" + v + "\" (unquoted it becomes a boolean).";
      }
    }
    return "";
  }
  function checkYaml(showOk) {
    const err = validateYaml(ta.value); errBox.textContent = err; errBox.style.color = err ? "#fda4a4" : "#86efac";
    if (!err && showOk) errBox.textContent = "Looks valid ✓"; return !err;
  }
  const EXAMPLE = "\n- name: vent_fan\n  enabled: true\n  when:\n    all:\n      - { metric: temperature, operator: \">\", value: 85, for: \"10m\" }\n      - { metric: time_is_daytime, operator: \"==\", value: true }\n  topic: \"facility/vent_fan\"\n  on_match: \"ON\"\n  on_clear: \"OFF\"\n";
  document.getElementById("add-example").addEventListener("click", () => { ta.value = ta.value.replace(/\s*$/, "") + "\n" + EXAMPLE; ta.focus(); checkYaml(true); });
  document.getElementById("check").addEventListener("click", () => checkYaml(true));
  ta.addEventListener("input", () => { errBox.textContent = ""; });

  /* ---- submit (mode depends on which Save was clicked) ------------------ */
  let mode = "form";
  document.getElementById("save-form").addEventListener("click", () => mode = "form");
  document.getElementById("save-yaml").addEventListener("click", () => mode = "yaml");
  form.addEventListener("submit", e => {
    e.preventDefault();
    if (mode === "form") {
      const data = collect(); const err = validateForm(data);
      const box = document.getElementById("form-err");
      box.textContent = err; box.style.color = "#fda4a4";
      if (err) { toast("Could not save: " + err, true); return; }
      toast("Rules saved (demo — nothing was written).");
    } else {
      if (checkYaml(false)) toast("Rules saved (demo — nothing was written).");
      else toast("Could not save: " + errBox.textContent, true);
    }
  });

  /* ---- initial render --------------------------------------------------- */
  const INITIAL = window.DEMO_RULES || [];
  (INITIAL.length ? INITIAL : [null]).forEach(r => builder.appendChild(ruleCard(r)));
  reindex();
  document.querySelector('.tab[data-tab="form"]').click();
})();

/* =========================================================================
   ACTIVITY  ·  read-only audit-log viewer (sample data in the demo)
   ========================================================================= */
(function activity() {
  const tb = document.getElementById("actbody");
  if (!tb) return;
  const ago = m => new Date(Date.now() - m * 60000).toISOString().replace(/\.\d+Z$/, "Z");
  // Mock events mirroring the two shapes the live app records (monitor + UI).
  const events = [
    { ts: ago(1),   device: "maintenance_hold", action: "manual_set", state: "on", by: "admin" },
    { ts: ago(3),   device: "irrigation_rain_inhibit", source: "auto", state: "on", by: "monitor" },
    { ts: ago(18),  variable: "maintenance_mode", action: "variable_set", value: true, by: "admin" },
    { ts: ago(46),  device: "vent_fan", source: "auto", state: "off", by: "monitor" },
    { ts: ago(95),  device: "irrigation_rain_inhibit", source: "auto", state: "off", by: "monitor" },
    { ts: ago(140), device: "vent_fan", source: "manual", state: "on", by: "admin" },
  ];
  function describe(e) {
    if (e.action === "manual_set")   return { what: e.device, action: "manual override", detail: String(e.state).toUpperCase() };
    if (e.action === "variable_set") return { what: e.variable, action: "variable set", detail: String(e.value) };
    const src = e.source === "manual" ? "manual" : "automatic";
    return { what: e.device, action: src + " state change", detail: String(e.state).toUpperCase() };
  }
  function pillFor(d) {
    if (d.action === "manual override" || /^manual/.test(d.action)) return '<span class="pill on">' + esc(d.action) + "</span>";
    if (d.action === "variable set") return '<span class="pill na">' + esc(d.action) + "</span>";
    return '<span class="pill off">' + esc(d.action) + "</span>";
  }
  document.getElementById("act-count").textContent = events.length + " recent";
  tb.innerHTML = "";
  for (const e of events) {
    const d = describe(e); const tr = document.createElement("tr");
    tr.innerHTML = '<td class="muted" title="' + esc(e.ts || "") + '">' + esc(agoText(e.ts)) + "</td>" +
      "<td>" + esc(d.what || "—") + "</td><td>" + pillFor(d) + "</td>" +
      "<td>" + esc(d.detail || "—") + "</td><td class=\"muted\">" + esc(e.by || "—") + "</td>";
    tb.appendChild(tr);
  }
})();

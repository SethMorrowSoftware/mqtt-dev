# Roadmap — v2: a customizable "Conditions → Actions" controller

> Status: **proposal / spec only.** Nothing here is built yet; the current v1
> controller is unchanged. This document is the agreed plan for evolving it from
> a weather→irrigation tool into a general, web‑customizable on/off automation
> hub — **without breaking existing installs.**

## Goal

Generalize from *NWS weather → one MQTT irrigation directive* to:

```
[ Sources ] → [ Context: named metrics ] → [ Rules ] → [ Devices (on/off) ] → [ Actions: MQTT ]
```

…with everything editable from the web UI, optional **manual override** of devices,
and the same reliability/security posture as today.

## Scope (locked)

| Decision | Choice |
|---|---|
| Output kind | **On/off only** (relays/switches). No analog/dimming. |
| Manual control from the UI | **Yes** — opt‑in, LAN‑only, authenticated, audited. |
| New inputs | **All:** schedule/time, MQTT‑in sensors, manual variables, HTTP poll. |
| Output channels | **MQTT only** for now (action layer pluggable for webhooks later). |
| History / trends | **Low priority** — optional final phase, off by default. |
| Multi‑site / roles | **No** — single site, single operator (one login). |

Explicitly **out of scope** (for now): analog outputs, non‑MQTT actions (webhooks),
multi‑user roles, any cloud‑initiated control. The outbound read‑only status page
stays read‑only.

---

## Core model

- **Source** — a pluggable input that publishes namespaced **metrics** into a shared
  context each cycle (and, where possible, on event). `weather.*`, `time.*`,
  `tank.level`, `flags.season_summer`, …
- **Device** — a named on/off thing. Its **desired** state comes from a rule
  (`when`), optionally gated by a **window** and smoothed by **hysteresis**, and can
  be **manually overridden**. On each on→off / off→on transition it fires **actions**.
- **Action** — what to do on a transition. v2 ships `mqtt` (publish); the registry
  allows adding `webhook`/`notify` later without touching the engine.

A v1 "rule" becomes a v2 "device" with two MQTT actions — see migration below.

---

## Config schema v2 (illustrative)

```yaml
version: 2

site:
  latitude: 41.2459
  longitude: -74.2735
  user_agent: "facility-controller (ops@example.com)"   # NWS contact
  poll_interval_minutes: 15

mqtt:   { host: localhost, port: 1883, qos: 1, retain: true, client_id: ... }   # as v1
web:    { enabled: true, host: 0.0.0.0, port: 8080, username: "", password: "",
          allow_manual_control: false }                  # NEW: gates manual override
slack:  { ... }            # as v1
status_push: { ... }       # as v1 (still read-only/outbound)

sources:
  - { name: weather, type: weather, lookback_hours: 24 }   # NWS via site lat/long
  - { name: clock,   type: schedule }                      # time.hour, time.dow, time.is_daytime, sun times
  - name: tank
    type: mqtt_in           # subscribe to a sensor topic, expose as a metric
    topic: sensors/tank/level
    metric: tank.level
    parse: number           # number | bool | string | json
  - name: power
    type: http_poll
    url: "https://meter.local/api"
    interval_minutes: 5
    map: { power.kw: "$.current_kw" }     # metric <- JSONPath
  - name: flags
    type: manual            # operator-set virtual variables (toggled in the UI)
    variables:
      season_summer: { kind: bool, default: false }

devices:
  - name: rain_inhibit
    description: "Hold irrigation when raining or >= 0.25 in / 24h"
    enabled: true
    manual: auto            # auto | on | off   (persisted across restarts)
    when:                   # desired ON when this evaluates true (nested any/all/not)
      any:
        - { metric: weather.is_raining,     op: "==", value: true }
        - { metric: weather.precip_accum_in, op: ">=", value: 0.25 }
    window: { from: "00:00", to: "24:00", days: [mon,tue,wed,thu,fri,sat,sun] }  # optional
    hysteresis: { min_on: 0m, min_off: 0m, cooldown: 0m }                        # optional
    on:  { mqtt: { topic: "irrigation/rain_inhibit", payload: "INHIBIT", retain: true } }
    off: { mqtt: { topic: "irrigation/rain_inhibit", payload: "ALLOW",  retain: true } }
```

### Engine: how a device resolves each cycle
1. If `enabled: false` → idle (no actions).
2. If `manual` is `on`/`off` → **desired = manual** (operator wins). Manual bypasses
   hysteresis (intent is explicit) but is still logged.
3. Else evaluate `when` inside `window`:
   - outside the window → desired = **off**;
   - any referenced metric unavailable → **hold last state** (preserve today's fail‑safe);
   - otherwise desired = the boolean result.
4. Apply `hysteresis`/`min_on`/`min_off`/`cooldown` (anti‑short‑cycle for real loads).
5. If the committed state changed, fire the matching `on`/`off` action(s). Keep the
   current **self‑healing publish** (a failed MQTT publish doesn't commit the state,
   so it retries next cycle).

### New rule operators
Add to the current set: `between`, `in`, `changed`, and `for: <duration>` (condition
sustained for N minutes). Keep `unknown → hold`.

---

## Sources (input plugins)

Each `type` is a small module registered by name. Spec per type:

- **weather** — today's NWS logic, namespaced under `weather.*`.
- **schedule** — `time.hour`, `time.minute`, `time.dow`, `time.is_between("06:00","20:00")`,
  `time.is_daytime` (sunrise/sunset from site lat/long). No external calls.
- **mqtt_in** — subscribe to a topic, coerce the payload (`parse`), expose as a metric.
  Enables **event‑driven** reaction (re‑evaluate on message), not just polling.
- **http_poll** — GET a JSON endpoint on an interval, map fields via JSONPath to metrics.
- **manual** — operator‑set virtual variables (booleans/enums) toggled in the UI; useful
  as rule inputs ("maintenance mode", "summer").

The rule builder discovers available metrics dynamically from the active sources
(dropdowns populate automatically).

---

## Manual control + security (because control is now possible)

Today's safety is that the system is *push‑only*. Manual override introduces a
control surface, so:

- A global **`web.allow_manual_control`** (default **false**). When false, the UI is
  display‑only exactly like today.
- Enabling manual control **requires a web login to be set** (refuse otherwise).
- Each device gets **Auto / On / Off** controls in the dashboard; choosing On/Off sets
  `device.manual`, **persisted** (small `overrides.json`) so it survives restarts.
- **Audit log** (append‑only file): every manual change and every automatic state change,
  with timestamp and the authenticated user.
- The **cloud status page stays strictly read‑only** — it can never issue commands.

---

## Web UI changes

- **Dashboard → device grid:** one card per device (state ON/OFF, the condition summary,
  last change, MQTT topic, and Auto/On/Off buttons when control is enabled). The current
  conditions + rules panels remain.
- **Sources page:** add/edit inputs; live metric preview.
- **Builder:** per‑device `when` (nested any/all/not), window, hysteresis, and on/off
  actions, with metric dropdowns sourced live.
- **Neutral branding:** "Automation / Conditions → Actions"; *irrigation becomes a preset*,
  not the product identity. The demo + cloud‑status pages track the same UI.

---

## Architecture / refactor

Convert the two scripts into a small package with a plugin registry:

```
controller/
  config.py     # load + validate + migrate (v1 -> v2)
  context.py    # the metric namespace
  engine.py     # device resolution: window, hysteresis, manual, action dispatch
  loop.py       # poll loop + optional event-driven (mqtt_in) re-eval
  sources/      weather.py  schedule.py  mqtt_in.py  http_poll.py  manual.py   (+ registry)
  actions/      mqtt.py     (+ registry; webhook/notify later)
  web/          app.py + templates
  store.py      # overrides.json now; SQLite later (optional, Phase 4)
```

`sources/` and `actions/` self‑register by `type` string, which is what makes the
system "as customizable as possible" without engine changes.

---

## Backward compatibility & migration (non‑negotiable)

- A **`version:` key** gates the schema. A **v1 → v2 migrator** runs at load:
  - `location` + `user_agent` + `poll_interval_minutes` + `precipitation` → `site` + a
    `weather` source;
  - each v1 `rule` → a v2 `device`: `when` carried over; `topic`/`on_match`/`on_clear` →
    `on`/`off` MQTT actions (retain from `mqtt.retain`); `manual: auto`; zero hysteresis;
  - `mqtt`/`web`/`slack`/`status_push` carried unchanged.
- **MQTT topics/payloads are identical after migration**, so PLCs/subscribers need no
  changes. Services, installer, and update path are unchanged.
- A fresh install still behaves like today out of the box (one weather source, one
  device, manual control off). Everything new is opt‑in.

---

## Phased delivery (each phase = its own PR: tests + docs + demo/cloud parity + CI)

- **Phase 0 — Foundations (invisible):** package refactor; `version:` + v1→v2 migrator
  (no behavior change); de‑hardcode the dashboard hero (auto‑detect first device).
- **Phase 1 — Engine:** nested `any/all/not`; `between`/`in`/`changed`/`for`; per‑device
  `enabled`; **time windows**; **hysteresis / min‑on / min‑off / cooldown**. *Biggest
  reliability gain for switching real loads.*
  - **Delivered:** nested `any/all/not`, the `between`/`in` operators, per‑rule
    `enabled`, **time windows**, **hysteresis (min‑on/min‑off/cooldown)**, and the
    stateful **`changed`** operator + **`for:` sustain** modifier (engine +
    validation + tests + docs; back‑compatible). **Phase 1 engine is complete.**
  - **Builder UI:** the form builder now edits single‑level rules end‑to‑end —
    `enabled`, `between`/`in`, the `changed` operator, and per‑condition `for:`.
    Nested/`not`, time windows, and hysteresis remain YAML‑tab only (the Rules
    page auto‑opens the YAML editor when a rule uses them).
  - **Still to come (own PRs):** a builder UI for the remaining nested/`not`/
    window/hysteresis constructs (optional polish). Then **Phase 2** (device
    model + manual control).
- **Phase 2 — Devices + manual control:** device/channel model; generalized device‑grid
  dashboard; **manual Auto/On/Off** (opt‑in, persisted, audited) with the security gating
  above. (Actions still MQTT.)
  - **Delivered:** **manual Auto/On/Off** — `web.allow_manual_control` (fail‑closed,
    requires a login), per‑device override persisted to `overrides.json` (overlay on
    config; manual wins and bypasses hysteresis), an append‑only `audit.log` of manual
    and automatic changes, an authenticated `POST /api/control` endpoint, dashboard
    Auto/On/Off buttons, and a read‑only "manual" indicator on the cloud status page.
  - **Delivered (cont.):** an in‑UI **Activity** page (`/activity` + `/api/audit`)
    that renders the audit log in plain language; a **device‑grid dashboard** (cards
    with inline Auto/On/Off, a status legend, and a getting‑started empty state);
    onboarding/inline help; and **branding** as *The Castle Fun Center · MQTT Command
    Center* across the web UI, demo, and cloud‑status pages.
  - **Delivered (cont.):** a **System** page (`/system` + `/api/system` + `/api/logs`)
    — at‑a‑glance health (monitor running/stale, MQTT, config validity, poll
    freshness), a configuration summary (rule/metric/input counts and files in use),
    and a **live runtime‑log viewer** with level filtering. The monitor mirrors its
    log to a rolling `log_file` (default `monitor.log`) so the separate web‑UI process
    can tail it. Mirrored in the static demo.
  - Phase 2 is complete.
- **Phase 3 — Inputs:** `schedule`, `manual` variables, `mqtt_in` sensors, `http_poll`;
  dynamic metric discovery in the builder; optional event‑driven re‑eval on MQTT input.
  - **Delivered:** **schedule/clock metrics** (`time_hour`, `time_minute`,
    `time_weekday`, `time_is_weekend`); **operator‑set `variables`** (bool/number
    flags declared in config, toggled from the dashboard, persisted to
    `variables.json`, audited) surfaced as `var_<name>` metrics; and **dynamic metric
    discovery** — the builder dropdowns now include declared variables live.
  - **Delivered (cont.):** **`mqtt_in` sensors** — `mqtt_inputs:` subscribes on the
    existing broker connection and exposes each payload (`number`/`bool`/`string`) as
    a rule metric, discovered by the builder; unavailable until first message
    (fail‑safe hold).
  - **Delivered (cont.):** **`http_poll`** — `http_inputs:` GETs a JSON endpoint on
    an interval and maps fields (dotted path) to typed metrics, discovered by the
    builder; best‑effort with fail‑safe hold.
  - **Delivered (cont.):** **`time_is_daytime`** — a dependency‑free sunrise/sunset
    flag from the site lat/long (handles polar day/night). **Phase 3 input sources
    are complete.**
  - **Optional remaining:** event‑driven re‑eval on MQTT input (today inputs are
    read each poll cycle).
- **Phase 4 — History (optional, low priority):** SQLite event log + simple trends;
  persisted overrides move into the store.

Phases 0–2 already deliver "control anything on/off, fully customizable, with manual
override"; Phase 3 adds the rich inputs; Phase 4 is the nice‑to‑have.

---

## Testing / CI

- Unit tests per source and action; **migration tests** (a real v1 `config.yaml`
  migrates to a v2 that produces byte‑identical MQTT behavior); engine **state‑machine
  tests** for window/hysteresis/manual precedence and the unknown→hold fail‑safe.
- Keep the existing Python‑version matrix **and** the end‑to‑end `install-smoke` job;
  add a check that a v1 config still boots unchanged.

## Guiding principles (carry over from v1)

- **Simple by default, powerful when needed** — defaults reproduce today's behavior.
- **Fail‑safe** — unknown inputs hold state; failed publishes retry.
- **Local control, outbound‑only status** — never expose control to the internet.
- **Everything web‑editable**, validated before save, with a friendly demo mirror.

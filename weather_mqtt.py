#!/usr/bin/env python3
"""
weather_mqtt.py -- Monitor precipitation from the National Weather Service
(api.weather.gov) and publish MQTT messages so irrigation PLCs know when NOT
to water.

Primary job:
  - Pull measured rainfall over a rolling window (default 24h) and whether it
    is precipitating right now from the nearest NWS observation station.
  - Evaluate rules from config.yaml. The default rule says "if it is raining
    OR it has rained >= X inches in the last 24h, tell the PLCs to inhibit
    watering" by publishing a retained MQTT message.
  - Publish only when a rule's state changes (so the bus isn't spammed),
    with retain=True so a PLC that connects later immediately gets the
    current directive.

No API key is required. The NWS API is free and US-only.

Run:   python weather_mqtt.py --config config.yaml
Test:  python weather_mqtt.py --config config.yaml --once --dry-run --verbose
"""

import argparse
import json
import logging
import os
import re
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yaml
import paho.mqtt.client as mqtt

LOG = logging.getLogger("weather_mqtt")
NWS_API = "https://api.weather.gov"
CACHE_FILE = Path("nws_location_cache.json")

# Words in NWS present-weather / textDescription that mean "it's precipitating".
# Note "freezing" is intentionally NOT here: alone it matches "Freezing Fog"
# (not precipitation). "Freezing Rain"/"Freezing Drizzle" still match via
# "rain"/"drizzle".
PRECIP_WORDS = (
    "rain", "drizzle", "shower", "thunderstorm", "sleet",
    "snow", "wintry", "ice pellets", "hail",
)
# Phrases that mean the precip is NOT falling at the station, so they must not
# trip is_raining (which would wrongly hold irrigation closed).
NOT_HERE_WORDS = ("vicinity", "in the area")

# Canonical metric catalogue: value type + the operators each accepts. This is
# the single source of truth shared by config validation here and the web UI's
# rule builder (which imports it), so the two can never drift apart.
NUMERIC_COMPARE = ("<", "<=", ">", ">=", "==", "!=")
# Set-style operators (ROADMAP Phase 1 engine): `between` takes an inclusive
# [low, high] pair; `in` takes a list of allowed values.
SET_OPS = ("between", "in")
NUMBER_OPS = NUMERIC_COMPARE + SET_OPS
TEXT_OPS = ("contains", "equals", "in")
METRIC_SPECS = {
    "is_raining":                {"type": "bool",   "ops": ("==", "!=")},
    "precip_accum_in":           {"type": "number", "ops": NUMBER_OPS},
    "precipitation_probability": {"type": "number", "ops": NUMBER_OPS},
    "temperature":               {"type": "number", "ops": NUMBER_OPS},
    "wind_speed_mph":            {"type": "number", "ops": NUMBER_OPS},
    "humidity":                  {"type": "number", "ops": NUMBER_OPS},
    "short_forecast":            {"type": "text",   "ops": TEXT_OPS},
    "active_alert":              {"type": "alert",  "ops": ("any", "contains", "equals")},
}


MAX_WHEN_DEPTH = 25   # guard against pathological deeply-nested configs


def _validate_condition(cond, rule_name):
    """Validate one rule condition's metric/operator/value. Raises ValueError."""
    if not isinstance(cond, dict) or "metric" not in cond:
        raise ValueError(f"rule '{rule_name}': each condition needs a 'metric'")
    metric = cond["metric"]
    spec = METRIC_SPECS.get(metric)
    if spec is None:
        raise ValueError(f"rule '{rule_name}': unknown metric '{metric}' "
                         f"(valid: {', '.join(sorted(METRIC_SPECS))})")
    op = cond.get("operator")
    # `for:` is an optional sustain modifier on any condition (must be a duration).
    if cond.get("for") is not None and parse_duration(cond["for"], None) is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' for: {cond['for']!r} must "
                         "be a duration like '10m', '30s', '2h', or minutes")
    # `changed` works on any metric and needs no value.
    if op == "changed":
        return
    if metric == "active_alert" and op in (None, "any"):
        return  # the "any active alert" form needs no value
    if op not in spec["ops"]:
        raise ValueError(f"rule '{rule_name}': operator '{op}' is not valid for "
                         f"metric '{metric}' (valid: {', '.join(spec['ops'])})")
    if "value" not in cond or cond["value"] is None:
        raise ValueError(f"rule '{rule_name}': condition on '{metric}' needs a value")
    value = cond["value"]
    if op == "between":
        _validate_between_value(value, metric, rule_name)
    elif op == "in":
        _validate_in_value(value, spec["type"], metric, rule_name)
    elif spec["type"] == "number" and _as_number(value, None, f"{metric} value") is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' value "
                         f"{value!r} must be a number")


def _validate_between_value(value, metric, rule_name):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"rule '{rule_name}': '{metric}' between needs a "
                         "[low, high] pair")
    lo = _as_number(value[0], None, "between low")
    hi = _as_number(value[1], None, "between high")
    if lo is None or hi is None:
        raise ValueError(f"rule '{rule_name}': '{metric}' between bounds must be numbers")
    if lo > hi:
        raise ValueError(f"rule '{rule_name}': '{metric}' between low must be <= high")


def _validate_in_value(value, vtype, metric, rule_name):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"rule '{rule_name}': '{metric}' in needs a non-empty list")
    if vtype == "number":
        for v in value:
            if _as_number(v, None, "in item") is None:
                raise ValueError(f"rule '{rule_name}': '{metric}' in list must be "
                                 "all numbers")


def _validate_rule_when(when, rule_name, _depth=0):
    """Validate a rule's `when`: a single condition, or a nested group built from
    `any` (OR), `all` (AND), and `not` (negation), to arbitrary depth."""
    if _depth > MAX_WHEN_DEPTH:
        raise ValueError(f"rule '{rule_name}': condition nesting is too deep "
                         f"(max {MAX_WHEN_DEPTH})")
    if isinstance(when, dict) and ("any" in when or "all" in when):
        if len(when) != 1:
            raise ValueError(f"rule '{rule_name}': an any/all group must have exactly "
                             "one of 'any' or 'all' as its only key")
        mode = "any" if "any" in when else "all"
        group = when[mode]
        if not isinstance(group, list) or not group:
            raise ValueError(f"rule '{rule_name}': '{mode}' must be a non-empty list")
        for c in group:
            _validate_rule_when(c, rule_name, _depth + 1)
    elif isinstance(when, dict) and "not" in when:
        if len(when) != 1:
            raise ValueError(f"rule '{rule_name}': 'not' must be the only key in its group")
        _validate_rule_when(when["not"], rule_name, _depth + 1)
    else:
        _validate_condition(when, rule_name)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# Sensible floors/limits so a typo in config.yaml (or the web UI) can never put
# the monitor into a tight loop hammering the free NWS API, or hand paho an
# illegal QoS. These are clamped (with a warning) rather than fatal so the
# monitor keeps running on the last-known-good behavior.
MIN_POLL_MINUTES = 1
MIN_LOOKBACK_HOURS = 1
MAX_LOOKBACK_HOURS = 720      # 30 days; NWS observation history is limited anyway

# Config schema version. A missing `version:` is treated as 1 so every existing
# install keeps loading unchanged. The v2 "Conditions -> Actions" schema (see
# ROADMAP.md) is not implemented yet; the gate exists now so a v2 file is
# rejected with a clear message instead of being silently mis-parsed as v1.
CURRENT_SCHEMA_VERSION = 1


def _as_number(value, default, name):
    """Coerce a YAML scalar to int/float, falling back to default with a warn."""
    if isinstance(value, bool):  # bool is a subclass of int; reject it explicitly
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        s = str(value).strip()
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except (TypeError, ValueError):
        LOG.warning("%s=%r is not a number; using %r", name, value, default)
        return default


def validate_config(cfg):
    """Validate structure and sanitize/clamp numeric fields in place.

    Raises ValueError for problems that make the config unusable (missing
    sections, no coordinates, empty rules, malformed rules). Out-of-range
    numbers are clamped with a warning so a small mistake never takes the
    monitor down. Returns the same (mutated) cfg for convenience.
    """
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a mapping")

    # Schema-version gate. Absent == 1 (every current install). Anything other
    # than 1 is rejected clearly rather than mis-read against the v1 structure.
    version = cfg.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"config 'version' must be an integer (got {version!r})")
    if version != CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"config version {version} is not supported by this release "
            f"(expected {CURRENT_SCHEMA_VERSION}). The v2 schema in ROADMAP.md "
            "is not implemented yet.")
    cfg["version"] = version

    for key in ("location", "user_agent", "mqtt", "rules"):
        if key not in cfg:
            raise ValueError(f"config is missing required section: '{key}'")

    loc = cfg["location"]
    if not isinstance(loc, dict) or "latitude" not in loc or "longitude" not in loc:
        raise ValueError("config.location needs 'latitude' and 'longitude'")
    lat = _as_number(loc["latitude"], None, "location.latitude")
    lon = _as_number(loc["longitude"], None, "location.longitude")
    if lat is None or lon is None:
        raise ValueError("location.latitude/longitude must be numbers")
    if not (-90 <= lat <= 90):
        raise ValueError(f"location.latitude {lat} out of range (-90..90)")
    if not (-180 <= lon <= 180):
        raise ValueError(f"location.longitude {lon} out of range (-180..180)")
    loc["latitude"], loc["longitude"] = lat, lon

    if not cfg.get("user_agent") or not str(cfg["user_agent"]).strip():
        raise ValueError("user_agent must be set (NWS requires a real contact)")

    if not isinstance(cfg["rules"], list) or not cfg["rules"]:
        raise ValueError("'rules' must be a non-empty list")
    seen_names = set()
    for r in cfg["rules"]:
        if not isinstance(r, dict):
            raise ValueError("each rule must be a mapping")
        for req in ("name", "when", "topic", "on_match"):
            if req not in r:
                raise ValueError(f"rule '{r.get('name', '?')}' is missing '{req}'")
        name = r["name"]
        if name in seen_names:
            raise ValueError(f"duplicate rule name '{name}' (names must be unique)")
        seen_names.add(name)
        # Validate the condition(s) so one malformed rule is caught here rather
        # than blowing up mid-cycle in the monitor.
        _validate_rule_when(r["when"], name)
        # Per-rule on/off switch (default on). A disabled rule is left idle:
        # it's evaluated against nothing and publishes no actions this cycle.
        en = r.get("enabled", True)
        if isinstance(en, str):
            en = en.strip().lower() not in ("false", "0", "no", "off", "")
        r["enabled"] = bool(en)
        # Optional time window + hysteresis (anti-short-cycle) per rule.
        if r.get("window") is not None:
            _validate_window(r["window"], name)
        if r.get("hysteresis") is not None:
            _validate_hysteresis(r["hysteresis"], name)
        # Config-declared manual state (auto|on|off). The web UI sets runtime
        # overrides in overrides.json instead; this is just the fallback/default.
        man = str(r.get("manual", "auto")).strip().lower()
        if man not in ("auto", "on", "off"):
            LOG.warning("Rule '%s': manual=%r invalid; using 'auto'", name, r.get("manual"))
            man = "auto"
        r["manual"] = man

    # --- defaults + clamping for the forgiving numeric knobs ---
    poll = _as_number(cfg.get("poll_interval_minutes", 15), 15, "poll_interval_minutes")
    if poll < MIN_POLL_MINUTES:
        LOG.warning("poll_interval_minutes=%s is below the %d-minute floor; "
                    "clamping (be a good citizen of the free NWS API)",
                    poll, MIN_POLL_MINUTES)
        poll = MIN_POLL_MINUTES
    cfg["poll_interval_minutes"] = poll

    cfg.setdefault("always_publish", False)
    cfg["always_publish"] = bool(cfg["always_publish"])
    cfg.setdefault("state_file", "weather_state.json")
    # Where runtime manual overrides + the audit trail live (Phase 2).
    cfg.setdefault("overrides_file", "overrides.json")
    cfg.setdefault("audit_file", "audit.log")

    precip = cfg.setdefault("precipitation", {})
    lb = _as_number(precip.get("lookback_hours", 24), 24, "precipitation.lookback_hours")
    lb = max(MIN_LOOKBACK_HOURS, min(MAX_LOOKBACK_HOURS, int(lb)))
    precip["lookback_hours"] = lb

    web = cfg.setdefault("web", {})
    web.setdefault("enabled", True)
    web.setdefault("host", "0.0.0.0")
    web["port"] = _clamp_port(_as_number(web.get("port", 8080), 8080, "web.port"))
    web.setdefault("username", "")     # blank = no auth (use only on trusted LAN)
    web.setdefault("password", "")
    # Manual on/off control of devices from the dashboard. Default off so the UI
    # stays display-only exactly like today. Fail closed: enabling it requires a
    # web login (username AND password), else it is forced back off with a warning.
    amc = bool(web.get("allow_manual_control", False))
    if amc and not (str(web.get("username") or "") and str(web.get("password") or "")):
        LOG.warning("web.allow_manual_control requires a web login (username + "
                    "password); disabling manual control until one is set")
        amc = False
    web["allow_manual_control"] = amc

    mq = cfg["mqtt"]
    if not isinstance(mq, dict):
        raise ValueError("config.mqtt must be a mapping")
    mq.setdefault("host", "localhost")
    mq["port"] = _clamp_port(_as_number(mq.get("port", 1883), 1883, "mqtt.port"))
    mq.setdefault("username", "")
    mq.setdefault("password", "")
    mq.setdefault("client_id", "weather-mqtt-controller")
    qos = int(_as_number(mq.get("qos", 1), 1, "mqtt.qos"))
    if qos not in (0, 1, 2):
        LOG.warning("mqtt.qos=%s invalid; using 1", qos)
        qos = 1
    mq["qos"] = qos
    mq.setdefault("retain", True)
    mq["retain"] = bool(mq["retain"])
    mq.setdefault("status_topic", "")   # optional: JSON snapshot of conditions

    # --- Slack alerts (optional) ---
    slack = cfg.setdefault("slack", {})
    slack.setdefault("enabled", False)
    slack["enabled"] = bool(slack["enabled"])
    slack.setdefault("bot_token", "")      # or set SLACK_BOT_TOKEN in the env
    slack.setdefault("channel", "")        # channel name (#alerts) or ID (C0…)
    mins = _as_number(slack.get("broker_unreachable_minutes", 60), 60,
                      "slack.broker_unreachable_minutes")
    slack["broker_unreachable_minutes"] = max(1, int(mins))

    # --- Remote status push (optional, read-only/outbound) ---
    sp = cfg.setdefault("status_push", {})
    sp.setdefault("enabled", False)
    sp["enabled"] = bool(sp["enabled"])
    sp.setdefault("url", "")          # https endpoint that receives the snapshot
    sp.setdefault("token", "")        # shared secret sent in X-Status-Token

    # Payloads must be strings. Unquoted ON/OFF/YES/NO in YAML parse as
    # booleans -- coerce and warn so a PLC never gets "True" by surprise.
    for r in cfg["rules"]:
        for k in ("on_match", "on_clear"):
            if k in r and not isinstance(r[k], str):
                if isinstance(r[k], bool):
                    LOG.warning("Rule '%s': %s=%r looks like an unquoted YAML "
                                "boolean (ON/OFF/YES/NO). Quote it in config.yaml "
                                "to publish the literal text.", r.get("name"), k, r[k])
                r[k] = str(r[k])
    return cfg


def _clamp_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        return 8080
    return port if 1 <= port <= 65535 else 8080


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return validate_config(cfg)


# ---------------------------------------------------------------------------
# NWS / weather.gov client
# ---------------------------------------------------------------------------
def nws_get(url, user_agent, retries=3, timeout=20):
    """GET a weather.gov endpoint with the required User-Agent + retries.

    Retries transient failures (network errors, 5xx, 429) with exponential
    backoff. A non-retryable client error (e.g. 400/403/404) fails fast --
    retrying a rejected User-Agent or a bad station id only wastes time and
    pesters a free API.
    """
    headers = {"User-Agent": user_agent, "Accept": "application/geo+json"}
    delay = 2
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as e:
                    raise RuntimeError(f"NWS returned non-JSON for {url}: {e}")
            retryable = r.status_code == 429 or r.status_code >= 500
            LOG.warning("NWS %s returned HTTP %s (attempt %d/%d)%s",
                        url, r.status_code, attempt, retries,
                        "" if retryable else " -- not retrying")
            if not retryable:
                raise RuntimeError(
                    f"NWS request rejected with HTTP {r.status_code}: {url}")
        except requests.RequestException as e:
            LOG.warning("NWS request error for %s: %s (attempt %d/%d)",
                        url, e, attempt, retries)
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"NWS request failed after {retries} attempts: {url}")


def resolve_location(lat, lon, user_agent, station_override=None):
    """Resolve lat/lon -> forecast grid + nearest station. Cached to disk."""
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if (cached.get("lat") == lat and cached.get("lon") == lon
                    and cached.get("station_override") == station_override):
                LOG.info("Using cached NWS location data")
                return cached
        except Exception:
            pass  # fall through and re-resolve

    LOG.info("Resolving NWS grid point for %s,%s ...", lat, lon)
    points = nws_get(f"{NWS_API}/points/{lat},{lon}", user_agent)
    props = (points or {}).get("properties") or {}
    forecast_hourly = props.get("forecastHourly")
    stations_url = props.get("observationStations")
    if not forecast_hourly or not stations_url:
        raise RuntimeError(
            f"NWS /points response missing forecast/station URLs for {lat},{lon} "
            "(is the location inside US coverage?)")
    info = {
        "lat": lat,
        "lon": lon,
        "station_override": station_override,
        "forecast_hourly": forecast_hourly,
        "stations_url": stations_url,
        "grid_id": props.get("gridId"),
        "station_id": station_override,
    }
    if not station_override:
        try:
            stations = nws_get(info["stations_url"], user_agent)
            feats = stations.get("features", [])
            if feats:
                info["station_id"] = feats[0]["properties"]["stationIdentifier"]
        except Exception as e:
            LOG.warning("Could not resolve observation station: %s", e)

    CACHE_FILE.write_text(json.dumps(info))
    LOG.info("Resolved grid %s; observation station %s",
             info.get("grid_id"), info.get("station_id"))
    return info


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------
def c_to_f(c):
    return None if c is None else round((c * 9 / 5) + 32, 1)


def to_mm(value, unit_code):
    """Normalize an NWS length value (m / mm / cm) to millimeters."""
    if value is None:
        return None
    unit = (unit_code or "").split(":")[-1].lower()
    if unit in ("m", "meter", "meters"):
        return value * 1000.0
    if unit in ("cm", "centimeter", "centimeters"):
        return value * 10.0
    if unit in ("in", "inch", "inches", "[in_i]"):
        return value * 25.4
    # "mm", "millimeter", or unknown -> assume millimeters
    return float(value)


def mm_to_in(mm):
    return None if mm is None else round(mm / 25.4, 2)


# ---------------------------------------------------------------------------
# Precipitation
# ---------------------------------------------------------------------------
def _says_precip(text):
    """True if `text` names precipitation falling at the station (not nearby)."""
    t = (text or "").lower()
    if not t:
        return False
    if any(w in t for w in NOT_HERE_WORDS):
        return False  # e.g. "Showers in Vicinity" -- not at the station
    return any(word in t for word in PRECIP_WORDS)


def detect_raining(obs_props):
    """True if precipitating now, False if clearly not, None if unknown."""
    seen = False
    for w in (obs_props.get("presentWeather") or []):
        seen = True
        if w.get("inVicinity"):
            continue  # phenomenon is near, not at, the station
        if _says_precip((w.get("weather") or "") + " " + (w.get("rawString") or "")):
            return True
    text = (obs_props.get("textDescription") or "").strip()
    if text:
        seen = True
        if _says_precip(text):
            return True
    return False if seen else None


def fetch_precip_accum_in(station_id, user_agent, hours, now=None):
    """Measured precip over the last `hours`, in inches.

    Sums each hour's `precipitationLastHour`, de-duplicated into hourly
    buckets so more-frequent observations don't double-count. Returns None
    when the station reports no precipitation data at all (so a rule can
    leave its state unchanged rather than wrongly read "dry").
    """
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{NWS_API}/stations/{station_id}/observations?start={quote(start)}"
    data = nws_get(url, user_agent)
    return _accumulate_precip(data, hours, now)


def _accumulate_precip(data, hours, now):
    """Pure helper (no network) so it can be unit-tested with a fixture."""
    cutoff = now - timedelta(hours=hours)
    buckets = {}  # "YYYY-MM-DDTHH" -> max mm reported in that hour
    for feat in data.get("features", []):
        p = feat.get("properties", {})
        ts = p.get("timestamp")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when < cutoff:
            continue
        plh = p.get("precipitationLastHour") or {}
        mm = to_mm(plh.get("value"), plh.get("unitCode"))
        if mm is None:
            continue
        # Bucket by the parsed UTC hour, not the raw string, so the same instant
        # written with different timezone offsets can't land in two buckets and
        # double-count.
        key = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
        buckets[key] = max(buckets.get(key, 0.0), mm)

    if not buckets:
        return None  # station did not report any precip values this window
    return mm_to_in(sum(buckets.values()))


def fetch_conditions(loc, user_agent, lookback_hours):
    """Return a dict of current weather metrics for rule evaluation."""
    metrics = {
        "temperature": None,                 # degF
        "wind_speed_mph": None,
        "precipitation_probability": None,   # % (forecast, NOT measured)
        "precip_accum_in": None,             # measured rainfall over lookback
        "is_raining": None,                  # bool: precipitating right now
        "humidity": None,                    # %
        "short_forecast": "",
        "active_alerts": [],                 # list of NWS event names
    }

    # --- Hourly forecast: US units, includes forecast precip probability ---
    try:
        hourly = nws_get(loc["forecast_hourly"], user_agent)
        period = hourly["properties"]["periods"][0]
        metrics["temperature"] = float(period["temperature"])  # degF
        metrics["short_forecast"] = period.get("shortForecast", "")

        pop = period.get("probabilityOfPrecipitation", {}).get("value")
        metrics["precipitation_probability"] = float(pop) if pop is not None else 0.0

        ws = period.get("windSpeed", "") or ""           # e.g. "10 to 15 mph"
        nums = [int(s) for s in ws.replace("to", " ").split() if s.isdigit()]
        metrics["wind_speed_mph"] = float(max(nums)) if nums else 0.0
    except Exception as e:
        LOG.warning("Hourly forecast unavailable: %s", e)

    # --- Latest measured observation: temp/humidity + is_raining now ---
    if loc.get("station_id"):
        try:
            obs = nws_get(
                f"{NWS_API}/stations/{loc['station_id']}/observations/latest",
                user_agent,
            )
            op = obs["properties"]
            t = op.get("temperature", {}).get("value")      # degC
            if t is not None:
                metrics["temperature"] = c_to_f(t)
            h = op.get("relativeHumidity", {}).get("value")  # %
            if h is not None:
                metrics["humidity"] = round(h, 1)
            metrics["is_raining"] = detect_raining(op)
        except Exception as e:
            LOG.warning("Latest observation unavailable: %s", e)

        # --- Measured precip accumulation over the lookback window ---
        try:
            metrics["precip_accum_in"] = fetch_precip_accum_in(
                loc["station_id"], user_agent, lookback_hours)
        except Exception as e:
            LOG.warning("Precip accumulation unavailable: %s", e)
    else:
        LOG.warning("No observation station resolved; precipitation metrics "
                    "(precip_accum_in, is_raining) will be unavailable")

    # --- Active NWS alerts for this point ---
    try:
        alerts = nws_get(
            f"{NWS_API}/alerts/active?point={loc['lat']},{loc['lon']}",
            user_agent,
        )
        events = []
        for feat in alerts.get("features", []):
            ev = feat.get("properties", {}).get("event")
            if ev:
                events.append(ev)
        metrics["active_alerts"] = events
    except Exception as e:
        LOG.warning("Alerts unavailable: %s", e)

    return metrics


# ---------------------------------------------------------------------------
# Rule evaluation
# ---------------------------------------------------------------------------
NUMERIC_OPS = {
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _eval_condition(cond, metrics, rule_name, state=None, now=None):
    """One condition: True/False, or None if its metric is unavailable.

    Two history-dependent constructs need the per-cycle `state`/`now`:
      - operator `changed` -> True when the metric differs from last cycle;
      - a `for: <duration>` modifier -> the base condition must hold continuously
        for that long before it counts as True.
    """
    base = _eval_base(cond, metrics, rule_name, state)
    dur = cond.get("for")
    if dur is not None and state is not None and now is not None:
        base = _apply_for(base, _cond_key(rule_name, cond),
                          parse_duration(dur, 0), state, now)
    return base


def _eval_base(cond, metrics, rule_name, state):
    """The condition's value before any `for:` sustain gate is applied."""
    metric = cond["metric"]
    op = cond.get("operator")
    value = cond.get("value")

    # `changed`: did this metric's value move since the previous cycle?
    if op == "changed":
        if state is None:
            return None
        cur = metrics.get(metric)
        if cur is None:
            return None
        prev = state.prev_metrics.get(metric, _UNSET)
        if prev is _UNSET:
            return False          # first observation -> nothing to compare to yet
        return cur != prev

    # Special metric: active NWS alerts
    if metric == "active_alert":
        alerts = metrics.get("active_alerts", [])
        if op in (None, "any"):
            return len(alerts) > 0
        if op == "contains":
            return any(str(value).lower() in a.lower() for a in alerts)
        if op == "equals":
            return any(a == value for a in alerts)
        LOG.warning("Rule '%s': unknown alert operator '%s'", rule_name, op)
        return False

    # Text metric: short forecast
    if metric == "short_forecast":
        text = metrics.get("short_forecast", "") or ""
        if op == "contains":
            return str(value).lower() in text.lower()
        if op == "equals":
            return text.lower() == str(value).lower()
        if op == "in":
            return any(text.lower() == str(v).lower() for v in (value or []))
        LOG.warning("Rule '%s': unknown text operator '%s'", rule_name, op)
        return False

    # Numeric / boolean metrics
    current = metrics.get(metric)
    if current is None:
        LOG.warning("Rule '%s': metric '%s' unavailable this cycle",
                    rule_name, metric)
        return None
    if op == "between":
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            return None
        lo = _as_number(value[0], None, "between low")
        hi = _as_number(value[1], None, "between high")
        if lo is None or hi is None:
            return None
        return lo <= current <= hi
    if op == "in":
        items = value or []
        if isinstance(current, bool):
            return current in items
        return any(n is not None and current == n
                   for n in (_as_number(v, None, "in item") for v in items))
    fn = NUMERIC_OPS.get(op)
    if fn is None:
        LOG.warning("Rule '%s': unknown operator '%s'", rule_name, op)
        return None
    return fn(current, value)


def _eval_node(node, metrics, rule_name, state=None, now=None, _depth=0):
    """Recursively evaluate a `when` node with three-valued logic.

    Returns True, False, or None (a referenced metric was unavailable -> the
    caller leaves the rule's state unchanged). Groups: `any` (OR), `all` (AND),
    and `not` (negation), nestable to arbitrary depth; a leaf is a single
    {metric, operator, value} condition. Unknown (None) propagates so the
    fail-safe "hold last state" behaviour is preserved through nesting.
    """
    if _depth > MAX_WHEN_DEPTH + 5:   # defensive; validation already bounds depth
        return None
    if isinstance(node, dict) and ("any" in node or "all" in node):
        mode = "any" if "any" in node else "all"
        results = [_eval_node(c, metrics, rule_name, state, now, _depth + 1)
                   for c in node[mode]]
        if mode == "any":
            if any(r is True for r in results):
                return True
            if any(r is None for r in results):
                return None      # could still be true once missing data returns
            return False
        if any(r is False for r in results):   # all
            return False
        if any(r is None for r in results):
            return None
        return True
    if isinstance(node, dict) and "not" in node:
        inner = _eval_node(node["not"], metrics, rule_name, state, now, _depth + 1)
        return None if inner is None else (not inner)
    return _eval_condition(node, metrics, rule_name, state, now)


def evaluate_rule(rule, metrics, state=None, now=None):
    """Evaluate a rule's `when` (single condition, or a nested any/all/not
    group). Returns True, False, or None (metric(s) unavailable).

    `state`/`now` are needed only by the history-dependent constructs
    (`changed` operator and `for:` sustain); without them those evaluate to
    None/unsustained, so plain rules need no engine state."""
    return _eval_node(rule["when"], metrics, rule["name"], state, now)


# Sentinel distinguishing "metric never observed" from "observed value None".
_UNSET = object()


class EngineState:
    """Per-monitor history that the `changed` operator and `for:` sustain gate
    need across cycles. Created once and threaded into evaluate_rule each cycle;
    `observe()` is called at the end of a cycle to remember this cycle's metrics
    for the next one's `changed` comparison."""

    def __init__(self):
        self.prev_metrics = {}      # metric name -> value seen last cycle
        self.cond_since = {}        # condition key -> datetime it first held true

    def observe(self, metrics):
        self.prev_metrics = dict(metrics)


def _cond_key(rule_name, cond):
    """Stable identity for a leaf condition's `for:` timer, tied to the rule and
    the condition's content (so an edit re-arms the timer rather than reusing a
    stale one)."""
    return "|".join(str(x) for x in (
        rule_name, cond.get("metric"), cond.get("operator"),
        cond.get("value"), cond.get("for")))


def _apply_for(base, key, dur, state, now):
    """Gate a condition's `base` result behind a sustain duration: only True
    once `base` has held True continuously for `dur` seconds. False/None reset
    the timer (and propagate, preserving the unknown->hold fail-safe)."""
    if base is None:
        state.cond_since.pop(key, None)
        return None
    if not base:
        state.cond_since.pop(key, None)
        return False
    since = state.cond_since.get(key)
    if since is None:
        since = state.cond_since[key] = now
    return (now - since).total_seconds() >= dur


# ---------------------------------------------------------------------------
# Engine: time windows + hysteresis (ROADMAP Phase 1)
# ---------------------------------------------------------------------------
# A rule's evaluated result is its *desired* state. Two optional layers turn
# that into the *committed* state the monitor actually publishes:
#   - `window`: outside its active hours/days the desired state is forced OFF;
#   - `hysteresis`: min_on / min_off / cooldown timers suppress rapid flapping
#     so a real load (pump, valve, compressor) isn't short-cycled.
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def parse_duration(value, default=0):
    """Parse a duration into whole seconds. Accepts '30s', '10m', '2h', or a
    bare number (minutes). Returns `default` for None/blank/garbage."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value * 60)            # bare number == minutes
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(value).lower())
    if not m:
        return default
    return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "": 60}[m.group(2)])


def _parse_hhmm(s):
    """'HH:MM' -> minutes past midnight (0..1440). '24:00' is allowed (end of day)."""
    parts = str(s).strip().split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"time '{s}' must be HH:MM")
    h, mi = int(parts[0]), int(parts[1])
    total = h * 60 + mi
    if not (0 <= mi <= 59) or not (0 <= total <= 1440):
        raise ValueError(f"time '{s}' is out of range")
    return total


def _validate_window(win, rule_name):
    if not isinstance(win, dict):
        raise ValueError(f"rule '{rule_name}': window must be a mapping")
    for k in ("from", "to"):
        if k in win and win[k] is not None:
            try:
                _parse_hhmm(win[k])
            except ValueError as e:
                raise ValueError(f"rule '{rule_name}': window.{k}: {e}")
    days = win.get("days")
    if days is not None:
        if not isinstance(days, list) or not days:
            raise ValueError(f"rule '{rule_name}': window.days must be a non-empty list")
        for d in days:
            if str(d).strip().lower()[:3] not in WEEKDAYS:
                raise ValueError(f"rule '{rule_name}': window.days has invalid day '{d}' "
                                 f"(use {', '.join(WEEKDAYS)})")


def in_window(win, now):
    """True if local civil time `now` (a datetime) is inside `win`.

    `from`/`to` default to the whole day; `to` is exclusive so adjacent windows
    don't overlap. A window whose `from` is later than its `to` wraps past
    midnight (e.g. 22:00->06:00). `days` (mon..sun) filters by weekday.
    """
    if not win:
        return True
    days = win.get("days")
    if days:
        allowed = {str(d).strip().lower()[:3] for d in days}
        if WEEKDAYS[now.weekday()] not in allowed:
            return False
    start = _parse_hhmm(win.get("from", "00:00"))
    end = _parse_hhmm(win.get("to", "24:00"))
    cur = now.hour * 60 + now.minute
    if start == end:
        return True                       # zero-length/degenerate -> treat as always on
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end       # wraps past midnight


def _validate_hysteresis(hyst, rule_name):
    if not isinstance(hyst, dict):
        raise ValueError(f"rule '{rule_name}': hysteresis must be a mapping")
    for k in ("min_on", "min_off", "cooldown"):
        if k in hyst and hyst[k] is not None and parse_duration(hyst[k], None) is None:
            raise ValueError(f"rule '{rule_name}': hysteresis.{k} must be a duration "
                             "like '10m', '30s', '2h', or a number of minutes")


def apply_hysteresis(hyst, prev, desired, last_change, now):
    """Smooth a `desired` bool into the committed bool using min_on/min_off/
    cooldown. `prev` is the current committed state (None if never set),
    `last_change` the datetime it last changed, `now` the current time.

    Returns the state to commit: `desired` when a transition is allowed, or
    `prev` when a timer is still holding the current state.
    """
    if prev is None or prev == desired or not hyst:
        return desired
    elapsed = (now - last_change).total_seconds() if last_change else float("inf")
    if elapsed < parse_duration(hyst.get("cooldown"), 0):
        return prev
    hold = parse_duration(hyst.get("min_on" if prev else "min_off"), 0)
    if elapsed < hold:
        return prev
    return desired


def resolve_desired(rule, metrics, now_local, state=None, now=None):
    """The rule's desired state after the time-window gate: outside the window
    the desired state is OFF; inside it is the evaluated `when` (True/False/
    None, where None means hold)."""
    win = rule.get("window")
    if win and not in_window(win, now_local):
        return False
    return evaluate_rule(rule, metrics, state, now)


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Manual control: override store + audit log (ROADMAP Phase 2)
# ---------------------------------------------------------------------------
# Operators can override a device to forced ON/OFF from the dashboard. The
# override is persisted (overrides.json) so it survives restarts, and is applied
# as an overlay on top of the config so config edits don't wipe it. "auto" means
# "no override" -> let the rules decide; it is stored as the absence of a key.
MANUAL_STATES = ("auto", "on", "off")


def load_overrides(path):
    """Read the manual-override map {device_name: 'on'|'off'} from disk. Robust:
    a missing or corrupt file yields {} (no overrides)."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): v for k, v in data.items() if v in ("on", "off")}


def set_override(path, name, state):
    """Persist one device's override. 'auto' clears it (removes the key).
    Atomic write. Returns the resulting overrides map. Raises ValueError on a
    bad state."""
    if state not in MANUAL_STATES:
        raise ValueError(f"manual state must be one of {MANUAL_STATES}")
    overrides = load_overrides(path)
    if state == "auto":
        overrides.pop(name, None)
    else:
        overrides[name] = state
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(overrides, indent=2))
    tmp.replace(path)
    return overrides


def effective_manual(rule, overrides):
    """The manual state in force for a rule: a runtime override wins over the
    config-declared `manual`, otherwise 'auto'."""
    name = rule.get("name")
    if name in overrides:
        return overrides[name]
    return rule.get("manual", "auto")


def audit(path, **event):
    """Append one JSON event to the audit log. Best-effort: never raises."""
    try:
        event.setdefault("ts", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with open(path, "a") as f:
            f.write(json.dumps(event, sort_keys=True) + "\n")
    except Exception as e:
        LOG.warning("Could not write audit log %s: %s", path, e)


# ---------------------------------------------------------------------------
# MQTT
# ---------------------------------------------------------------------------
def make_mqtt_client(mq):
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=mq["client_id"],
    )
    if mq.get("username"):
        client.username_pw_set(mq["username"], mq.get("password", ""))

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOG.error("MQTT connect failed: %s", reason_code)
        else:
            LOG.info("Connected to MQTT broker %s:%s", mq["host"], mq["port"])

    def on_disconnect(client, userdata, flags, reason_code, properties):
        LOG.warning("Disconnected from MQTT broker (%s); auto-reconnecting",
                    reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


# ---------------------------------------------------------------------------
# Slack alerting (broker-unreachable)
# ---------------------------------------------------------------------------
class BrokerWatch:
    """Tracks how long the MQTT broker has been unreachable and decides when to
    fire a Slack alert (once on threshold breach) and a recovery notice.

    Pure/​deterministic (takes `now` as an argument) so it's unit-testable
    without sleeping or a real clock. `update()` returns one of:
      "down"      -> broker has been down past the threshold; alert now
      "recovered" -> broker is back after we had alerted; send the all-clear
      None        -> nothing to announce
    """

    def __init__(self, threshold_minutes=60):
        self.threshold = timedelta(minutes=max(1, int(threshold_minutes)))
        self.down_since = None
        self.alerted = False

    def update(self, connected, now):
        if connected:
            recovered = self.alerted
            self.down_since = None
            self.alerted = False
            return "recovered" if recovered else None
        if self.down_since is None:
            self.down_since = now
        if not self.alerted and (now - self.down_since) >= self.threshold:
            self.alerted = True
            return "down"
        return None

    def downtime_minutes(self, now):
        if self.down_since is None:
            return 0
        return int((now - self.down_since).total_seconds() // 60)


def slack_token(slack):
    """Bot token from the env (preferred) or config. Env wins so the secret can
    stay out of config.yaml."""
    return os.environ.get("SLACK_BOT_TOKEN") or (slack.get("bot_token") or "")


def notify_slack(slack, text):
    """Post a message to Slack via chat.postMessage. Best-effort: never raises."""
    if not slack or not slack.get("enabled"):
        return False
    token = slack_token(slack)
    channel = slack.get("channel", "")
    if not token or not channel:
        LOG.warning("Slack alert wanted but bot token or channel is not set "
                    "(set slack.channel and SLACK_BOT_TOKEN or slack.bot_token)")
        return False
    try:
        r = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
            timeout=10,
        )
        data = r.json()
        if not data.get("ok"):
            LOG.warning("Slack alert rejected: %s", data.get("error"))
            return False
        LOG.info("Slack alert sent to %s", channel)
        return True
    except Exception as e:
        LOG.warning("Slack alert failed to send: %s", e)
        return False


# ---------------------------------------------------------------------------
# State snapshot (consumed by the web UI + optional remote status page)
# ---------------------------------------------------------------------------
def build_snapshot(metrics, rule_rows, lookback, connected, manual_control=False):
    """The status object the dashboard(s) consume."""
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "lookback_hours": lookback,
        "mqtt_connected": connected,
        "manual_control": bool(manual_control),
        "metrics": metrics,
        "rules": rule_rows,
    }


def write_state(path, snapshot):
    """Atomically write the snapshot JSON for the local web UI."""
    try:
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        tmp.replace(path)
    except Exception as e:
        LOG.warning("Could not write state file %s: %s", path, e)


def push_status(cfg, snapshot):
    """POST the snapshot to an external read-only dashboard. Outbound-only and
    best-effort: never raises, never affects control. Auth via X-Status-Token."""
    if not cfg or not cfg.get("enabled"):
        return False
    url = cfg.get("url", "")
    if not url:
        LOG.warning("status_push enabled but no url is set")
        return False
    headers = {"Content-Type": "application/json"}
    token = cfg.get("token", "")
    if token:
        headers["X-Status-Token"] = token
    try:
        r = requests.post(url, json=snapshot, headers=headers, timeout=10)
        if r.status_code // 100 != 2:
            LOG.warning("status push to %s returned HTTP %s", url, r.status_code)
            return False
        return True
    except Exception as e:
        LOG.warning("status push failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Precipitation-driven MQTT controller (NWS / weather.gov)")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll then exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Evaluate rules and log, but don't publish MQTT")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    cfg = load_config(args.config)
    ua = cfg["user_agent"]
    lat = cfg["location"]["latitude"]
    lon = cfg["location"]["longitude"]
    station_override = cfg["location"].get("station_id")
    mq = cfg["mqtt"]

    stop = {"flag": False}

    def handle_sig(signum, frame):
        LOG.info("Signal %s received, shutting down ...", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    def interruptible_sleep(seconds):
        slept = 0
        while slept < seconds and not stop["flag"]:
            time.sleep(min(5, seconds - slept))
            slept += 5

    # Resolve location with backoff instead of crashing if NWS is unreachable at
    # boot -- otherwise systemd would restart us into a tight crash-loop during
    # an outage. Stays inside the process so SIGTERM still stops us promptly.
    loc = None
    delay = 5
    while not stop["flag"]:
        try:
            loc = resolve_location(lat, lon, ua, station_override)
            break
        except Exception as e:
            LOG.error("Location resolution failed (%s); retrying in %ds", e, delay)
            interruptible_sleep(delay)
            delay = min(delay * 2, 300)
    if loc is None:
        LOG.info("Stopped before location was resolved.")
        return

    client = None
    if not args.dry_run:
        client = make_mqtt_client(mq)
        client.connect_async(mq["host"], int(mq["port"]), keepalive=60)
        client.loop_start()

    last_state = {}            # rule name -> bool
    last_change = {}           # rule name -> iso timestamp of last published change
    engine_state = EngineState()   # history for `changed` / `for:` across cycles
    broker_watch = BrokerWatch(cfg["slack"]["broker_unreachable_minutes"])

    while not stop["flag"]:
        # Reload config each cycle so web-UI edits to rules / thresholds /
        # interval take effect without a restart. Location & MQTT connection
        # are fixed at startup (changing those needs a restart).
        try:
            cfg = load_config(args.config)
        except Exception as e:
            LOG.error("Config reload failed, keeping previous: %s", e)
        lookback = cfg["precipitation"]["lookback_hours"]
        interval = max(MIN_POLL_MINUTES, cfg["poll_interval_minutes"]) * 60
        rules = cfg["rules"]
        state_file = cfg["state_file"]
        # Manual overrides are an overlay re-read each cycle (like config), so the
        # web UI's Auto/On/Off takes effect on the next poll without a restart.
        overrides = load_overrides(cfg["overrides_file"])
        audit_file = cfg["audit_file"]
        allow_manual = cfg["web"].get("allow_manual_control", False)
        # Connection params (host/port/user/client_id) are fixed at startup, but
        # qos/retain/status_topic are publish-time options we can honor live so
        # web-UI edits to them take effect on the next cycle without a restart.
        mq_live = cfg["mqtt"]
        qos, retain = mq_live["qos"], mq_live["retain"]
        status_topic = mq_live.get("status_topic", "")

        try:
            m = fetch_conditions(loc, ua, lookback)
            LOG.info("Conditions: temp=%s F  humidity=%s%%  wind=%s mph  "
                     "raining=%s  precip_%dh=%s in  precip_prob=%s%%  '%s'  "
                     "alerts=%s",
                     m["temperature"], m["humidity"], m["wind_speed_mph"],
                     m["is_raining"], lookback, m["precip_accum_in"],
                     m["precipitation_probability"], m["short_forecast"],
                     m["active_alerts"] or "none")

            if client is not None and status_topic:
                client.publish(status_topic, json.dumps(m),
                               qos=qos, retain=retain)

            now_utc = datetime.now(timezone.utc)
            now_local = now_utc.astimezone()    # system local civil time for windows
            rule_rows = []
            for rule in rules:
              try:
                enabled = rule.get("enabled", True)
                prev = last_state.get(rule["name"])
                manual = effective_manual(rule, overrides)
                # Resolution order: disabled -> idle; a manual on/off wins and
                # bypasses window+hysteresis (intent is explicit); otherwise the
                # window-gated, hysteresis-smoothed rule result. None == hold.
                if not enabled:
                    result = None
                elif manual in ("on", "off"):
                    result = (manual == "on")
                else:
                    desired = resolve_desired(rule, m, now_local, engine_state, now_utc)
                    if desired is None:
                        result = None
                    else:
                        result = apply_hysteresis(
                            rule.get("hysteresis"), prev, desired,
                            _parse_iso(last_change.get(rule["name"])), now_utc)
                if enabled and result is not None:
                    changed = (prev is None) or (prev != result) or cfg["always_publish"]
                    # Assume committed unless a real publish fails below. A failed
                    # publish leaves last_state unchanged so the next cycle retries
                    # the directive instead of silently dropping a state change.
                    commit = True
                    if changed:
                        payload = rule["on_match"] if result else rule.get("on_clear", "")
                        if payload == "" and not result:
                            pass  # no clear payload configured; nothing to publish
                        else:
                            topic = rule["topic"]
                            if client is None:
                                LOG.info("[DRY-RUN] would publish '%s' -> %s "
                                         "(rule '%s', match=%s)",
                                         payload, topic, rule["name"], result)
                            else:
                                info = client.publish(topic, payload,
                                                      qos=qos, retain=retain)
                                if info.rc != mqtt.MQTT_ERR_SUCCESS:
                                    commit = False
                                    LOG.warning("Publish to %s returned rc=%s "
                                                "(broker offline? will retry next "
                                                "cycle)", topic, info.rc)
                                else:
                                    LOG.info("Published '%s' -> %s (rule '%s', "
                                             "match=%s)", payload, topic,
                                             rule["name"], result)
                            if commit and prev != result:
                                last_change[rule["name"]] = now_utc.isoformat(
                                    timespec="seconds")
                                audit(audit_file, device=rule["name"],
                                      state="on" if result else "off",
                                      source="manual" if manual in ("on", "off")
                                      else "auto", by="monitor")
                    if commit:
                        last_state[rule["name"]] = result

                rule_rows.append({
                    "name": rule["name"],
                    "description": rule.get("description", ""),
                    "topic": rule["topic"],
                    "enabled": enabled,
                    "manual": manual,
                    "active": last_state.get(rule["name"]),
                    "current_payload": (rule["on_match"]
                                        if last_state.get(rule["name"]) else
                                        rule.get("on_clear", ""))
                    if last_state.get(rule["name"]) is not None else None,
                    "last_change": last_change.get(rule["name"]),
                })
              except Exception as e:
                # One malformed/erroring rule must not take down the whole
                # cycle; log it and keep evaluating the rest.
                LOG.warning("Rule '%s' failed this cycle, skipping: %s",
                            rule.get("name", "?") if isinstance(rule, dict) else rule, e)

            # Remember this cycle's metrics so next cycle's `changed` can compare.
            engine_state.observe(m)

            connected = bool(client is not None and client.is_connected())
            snapshot = build_snapshot(m, rule_rows, lookback, connected, allow_manual)
            write_state(state_file, snapshot)
            push_status(cfg.get("status_push", {}), snapshot)

        except Exception as e:
            LOG.error("Poll cycle failed: %s", e)

        # Broker-reachability watch runs every cycle, independent of the weather
        # fetch above, so a Slack alert fires even during an NWS outage.
        if client is not None:
            slack_cfg = cfg.get("slack", {})
            broker_watch.threshold = timedelta(
                minutes=cfg["slack"]["broker_unreachable_minutes"])
            now = datetime.now(timezone.utc)
            trigger = broker_watch.update(client.is_connected(), now)
            if trigger == "down":
                mins = broker_watch.downtime_minutes(now)
                notify_slack(slack_cfg,
                             f":red_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` has been unreachable for "
                             f"~{mins} min. Irrigation directives are not being "
                             f"published.")
            elif trigger == "recovered":
                notify_slack(slack_cfg,
                             f":large_green_circle: *weather-mqtt*: MQTT broker "
                             f"`{mq['host']}:{mq['port']}` is reachable again.")

        if args.once:
            break

        interruptible_sleep(interval)  # so SIGTERM is handled promptly

    if client is not None:
        client.loop_stop()
        client.disconnect()
    LOG.info("Stopped.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Offline unit tests for weather_mqtt -- no network required.

Run:  python test_weather_mqtt.py
"""
from datetime import datetime, timezone

import weather_mqtt as w


def _obs(ts, value_mm, unit="wmoUnit:mm"):
    return {"properties": {"timestamp": ts,
                           "precipitationLastHour": {"value": value_mm,
                                                     "unitCode": unit}}}


def test_unit_conversion():
    assert w.to_mm(5, "wmoUnit:mm") == 5.0
    assert w.to_mm(0.005, "wmoUnit:m") == 5.0          # meters -> mm
    assert w.to_mm(0.5, "wmoUnit:cm") == 5.0           # cm -> mm
    assert w.to_mm(None, "wmoUnit:mm") is None
    assert w.mm_to_in(25.4) == 1.0


def test_accumulation_sums_hourly_buckets():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 6.35),   # 0.25 in
        _obs("2026-06-27T10:53:00+00:00", 12.7),   # 0.50 in
        _obs("2026-06-27T09:53:00+00:00", 0.0),
    ]}
    # 6.35 + 12.7 = 19.05 mm = 0.75 in
    assert w._accumulate_precip(data, 24, now) == 0.75


def test_accumulation_dedups_within_an_hour():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    # Two obs in the same clock-hour must not double count; take the max.
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 6.35),
        _obs("2026-06-27T11:20:00+00:00", 5.0),
    ]}
    assert w._accumulate_precip(data, 24, now) == 0.25


def test_accumulation_ignores_outside_window_and_meters():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        _obs("2026-06-27T11:53:00+00:00", 0.00635, "wmoUnit:m"),  # 6.35mm in
        _obs("2026-06-25T11:53:00+00:00", 100.0),                 # 2 days old
    ]}
    assert w._accumulate_precip(data, 24, now) == 0.25


def test_accumulation_none_when_no_data():
    now = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    data = {"features": [
        {"properties": {"timestamp": "2026-06-27T11:53:00+00:00",
                        "precipitationLastHour": {"value": None}}},
    ]}
    assert w._accumulate_precip(data, 24, now) is None


def test_detect_raining():
    assert w.detect_raining({"textDescription": "Light Rain"}) is True
    assert w.detect_raining({"presentWeather": [{"weather": "drizzle"}]}) is True
    assert w.detect_raining({"textDescription": "Clear"}) is False
    assert w.detect_raining({}) is None   # nothing reported -> unknown


def test_compound_any_rule():
    rule = {"name": "irr", "when": {"any": [
        {"metric": "is_raining", "operator": "==", "value": True},
        {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
    ]}}
    # raining now -> match even if accumulation low
    assert w.evaluate_rule(rule, {"is_raining": True, "precip_accum_in": 0.0}) is True
    # not raining but enough accumulation -> match
    assert w.evaluate_rule(rule, {"is_raining": False, "precip_accum_in": 0.3}) is True
    # dry both ways -> no match
    assert w.evaluate_rule(rule, {"is_raining": False, "precip_accum_in": 0.0}) is False


def test_compound_unknown_is_failsafe():
    rule = {"name": "irr", "when": {"any": [
        {"metric": "is_raining", "operator": "==", "value": True},
        {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
    ]}}
    # accumulation known-false but rain unknown -> None (leave state unchanged)
    assert w.evaluate_rule(rule, {"is_raining": None, "precip_accum_in": 0.0}) is None
    # one branch true is enough regardless of the unknown
    assert w.evaluate_rule(rule, {"is_raining": None, "precip_accum_in": 0.5}) is True


def test_nested_any_all_not():
    # not(temperature < 35)  AND  (raining OR accum >= 0.25)
    rule = {"name": "r", "when": {"all": [
        {"not": {"metric": "temperature", "operator": "<", "value": 35}},
        {"any": [
            {"metric": "is_raining", "operator": "==", "value": True},
            {"metric": "precip_accum_in", "operator": ">=", "value": 0.25},
        ]},
    ]}}
    # warm (not<35 -> true) and raining -> true
    assert w.evaluate_rule(rule, {"temperature": 50, "is_raining": True,
                                  "precip_accum_in": 0.0}) is True
    # cold (not<35 -> false) kills the ALL regardless of the rain branch
    assert w.evaluate_rule(rule, {"temperature": 20, "is_raining": True,
                                  "precip_accum_in": 1.0}) is False
    # warm, dry both ways -> false
    assert w.evaluate_rule(rule, {"temperature": 50, "is_raining": False,
                                  "precip_accum_in": 0.0}) is False


def test_not_propagates_unknown():
    rule = {"name": "r", "when": {"not": {"metric": "is_raining",
                                          "operator": "==", "value": True}}}
    assert w.evaluate_rule(rule, {"is_raining": True}) is False
    assert w.evaluate_rule(rule, {"is_raining": False}) is True
    assert w.evaluate_rule(rule, {"is_raining": None}) is None   # unknown stays unknown


def test_between_operator():
    rule = {"name": "r", "when": {"metric": "temperature",
                                  "operator": "between", "value": [40, 80]}}
    assert w.evaluate_rule(rule, {"temperature": 60}) is True
    assert w.evaluate_rule(rule, {"temperature": 40}) is True    # inclusive
    assert w.evaluate_rule(rule, {"temperature": 80}) is True    # inclusive
    assert w.evaluate_rule(rule, {"temperature": 81}) is False
    assert w.evaluate_rule(rule, {"temperature": None}) is None  # fail-safe


def test_in_operator_numeric_and_text():
    rnum = {"name": "r", "when": {"metric": "humidity",
                                  "operator": "in", "value": [30, 50, 70]}}
    assert w.evaluate_rule(rnum, {"humidity": 50}) is True
    assert w.evaluate_rule(rnum, {"humidity": 55}) is False
    rtext = {"name": "r", "when": {"metric": "short_forecast",
                                   "operator": "in", "value": ["Sunny", "Clear"]}}
    assert w.evaluate_rule(rtext, {"short_forecast": "clear"}) is True   # case-insensitive
    assert w.evaluate_rule(rtext, {"short_forecast": "Rain"}) is False


def test_validate_accepts_nested_and_new_ops():
    w.validate_config(_min_cfg(rules=[{
        "name": "complex", "topic": "t", "on_match": "1",
        "when": {"all": [
            {"not": {"metric": "is_raining", "operator": "==", "value": True}},
            {"metric": "temperature", "operator": "between", "value": [40, 90]},
            {"metric": "humidity", "operator": "in", "value": [10, 20, 30]},
        ]}}]))


def test_validate_rejects_bad_between_and_in():
    for when, needle in [
        ({"metric": "temperature", "operator": "between", "value": 5}, "between needs"),
        ({"metric": "temperature", "operator": "between", "value": [90, 10]}, "low must be <= high"),
        ({"metric": "temperature", "operator": "between", "value": ["a", "b"]}, "must be numbers"),
        ({"metric": "humidity", "operator": "in", "value": []}, "non-empty list"),
        ({"metric": "humidity", "operator": "in", "value": ["x"]}, "all numbers"),
        ({"not": {"metric": "bogus", "operator": "<", "value": 1}}, "unknown metric"),
        ({"any": []}, "non-empty list"),
    ]:
        try:
            w.validate_config(_min_cfg(rules=[{
                "name": "r", "topic": "t", "on_match": "1", "when": when}]))
            raise AssertionError(f"expected ValueError containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_validate_enabled_default_and_coercion():
    cfg = w.validate_config(_min_cfg())
    assert cfg["rules"][0]["enabled"] is True            # default on
    cfg2 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "enabled": False,
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg2["rules"][0]["enabled"] is False
    cfg3 = w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1", "enabled": "false",
        "when": {"metric": "temperature", "operator": "<", "value": 5}}]))
    assert cfg3["rules"][0]["enabled"] is False          # string coerced


def test_parse_duration():
    assert w.parse_duration("30s") == 30
    assert w.parse_duration("10m") == 600
    assert w.parse_duration("2h") == 7200
    assert w.parse_duration(5) == 300          # bare number == minutes
    assert w.parse_duration("0m") == 0
    assert w.parse_duration(None, 0) == 0
    assert w.parse_duration("garbage", 99) == 99


def test_in_window_hours_days_and_wrap():
    from datetime import datetime
    mon_10 = datetime(2026, 6, 29, 10, 0)   # Monday 10:00
    mon_05 = datetime(2026, 6, 29, 5, 0)    # Monday 05:00
    sun_10 = datetime(2026, 6, 28, 10, 0)   # Sunday 10:00
    assert w.in_window(None, mon_10) is True                                  # no window -> always
    assert w.in_window({"from": "06:00", "to": "20:00"}, mon_10) is True
    assert w.in_window({"from": "06:00", "to": "20:00"}, mon_05) is False     # before open
    assert w.in_window({"from": "06:00", "to": "20:00",
                        "days": ["mon", "tue"]}, sun_10) is False             # wrong day
    # overnight window 22:00 -> 06:00 wraps past midnight
    assert w.in_window({"from": "22:00", "to": "06:00"}, mon_05) is True
    assert w.in_window({"from": "22:00", "to": "06:00"}, mon_10) is False
    # `to` is exclusive
    assert w.in_window({"from": "06:00", "to": "10:00"}, mon_10) is False


def test_apply_hysteresis_min_on_off_cooldown():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    # first commit: no prior state -> desired wins regardless of timers
    assert w.apply_hysteresis({"min_off": "10m"}, None, True, None, t0) is True
    # currently ON, want OFF, min_on=10m not yet elapsed -> hold ON
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, t0, t0 + timedelta(minutes=5)) is True
    # ...once min_on elapses, allow OFF
    assert w.apply_hysteresis({"min_on": "10m"}, True, False, t0, t0 + timedelta(minutes=11)) is False
    # currently OFF, want ON, min_off holds it OFF until elapsed
    assert w.apply_hysteresis({"min_off": "10m"}, False, True, t0, t0 + timedelta(minutes=5)) is False
    assert w.apply_hysteresis({"min_off": "10m"}, False, True, t0, t0 + timedelta(minutes=11)) is True
    # cooldown blocks any transition regardless of direction
    assert w.apply_hysteresis({"cooldown": "30m"}, True, False, t0, t0 + timedelta(minutes=20)) is True
    # no hysteresis config -> desired passes straight through
    assert w.apply_hysteresis(None, True, False, t0, t0 + timedelta(minutes=1)) is False


def test_resolve_desired_window_gate():
    from datetime import datetime
    rule = {"name": "r", "window": {"from": "06:00", "to": "20:00"},
            "when": {"metric": "temperature", "operator": ">", "value": 50}}
    inside = datetime(2026, 6, 29, 10, 0)
    outside = datetime(2026, 6, 29, 22, 0)
    assert w.resolve_desired(rule, {"temperature": 60}, inside) is True
    assert w.resolve_desired(rule, {"temperature": 40}, inside) is False
    assert w.resolve_desired(rule, {"temperature": 60}, outside) is False    # window forces off
    # no window -> just the evaluation
    assert w.resolve_desired({"name": "r", "when": rule["when"]},
                             {"temperature": 60}, outside) is True


def test_validate_window_and_hysteresis():
    w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1",
        "window": {"from": "06:00", "to": "20:00", "days": ["mon", "fri"]},
        "hysteresis": {"min_on": "10m", "min_off": "5m", "cooldown": "0m"},
        "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
    for win, needle in [
        ({"from": "25:00", "to": "26:00"}, "out of range"),
        ({"from": "6am", "to": "20:00"}, "HH:MM"),
        ({"days": ["funday"]}, "invalid day"),
        ({"days": []}, "non-empty list"),
    ]:
        try:
            w.validate_config(_min_cfg(rules=[{
                "name": "r", "topic": "t", "on_match": "1", "window": win,
                "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
            raise AssertionError(f"expected ValueError for window {win}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"
    try:
        w.validate_config(_min_cfg(rules=[{
            "name": "r", "topic": "t", "on_match": "1",
            "hysteresis": {"min_on": "ten minutes"},
            "when": {"metric": "temperature", "operator": ">", "value": 50}}]))
        raise AssertionError("expected ValueError for bad hysteresis duration")
    except ValueError as e:
        assert "duration" in str(e)


def test_changed_operator():
    st = w.EngineState()
    rule = {"name": "r", "when": {"metric": "temperature", "operator": "changed"}}
    # first cycle: nothing to compare to yet -> False, then remember 70
    assert w.evaluate_rule(rule, {"temperature": 70}, st) is False
    st.observe({"temperature": 70})
    # same value -> not changed
    assert w.evaluate_rule(rule, {"temperature": 70}, st) is False
    st.observe({"temperature": 70})
    # different value -> changed
    assert w.evaluate_rule(rule, {"temperature": 72}, st) is True
    st.observe({"temperature": 72})
    # metric unavailable -> unknown (fail-safe)
    assert w.evaluate_rule(rule, {"temperature": None}, st) is None
    # without engine state the construct can't evaluate -> None
    assert w.evaluate_rule(rule, {"temperature": 80}) is None


def test_for_sustain_modifier():
    from datetime import datetime, timedelta, timezone
    st = w.EngineState()
    t0 = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    rule = {"name": "r", "when": {"metric": "temperature", "operator": ">",
                                  "value": 85, "for": "10m"}}
    # condition true but not yet sustained 10 min -> False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=5)) is False
    # sustained past 10 min -> True
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=11)) is True
    # drops below -> resets the timer (False), and must re-accumulate
    assert w.evaluate_rule(rule, {"temperature": 80}, st, t0 + timedelta(minutes=12)) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=13)) is False
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=24)) is True
    # unknown breaks continuity -> None (fail-safe), and resets timer
    assert w.evaluate_rule(rule, {"temperature": None}, st, t0 + timedelta(minutes=25)) is None
    assert w.evaluate_rule(rule, {"temperature": 90}, st, t0 + timedelta(minutes=26)) is False


def test_validate_changed_and_for():
    w.validate_config(_min_cfg(rules=[{
        "name": "r", "topic": "t", "on_match": "1",
        "when": {"all": [
            {"metric": "temperature", "operator": "changed"},
            {"metric": "humidity", "operator": ">", "value": 80, "for": "15m"},
        ]}}]))
    # bad `for` duration is rejected
    try:
        w.validate_config(_min_cfg(rules=[{
            "name": "r", "topic": "t", "on_match": "1",
            "when": {"metric": "temperature", "operator": ">", "value": 5,
                     "for": "soon"}}]))
        raise AssertionError("expected ValueError for bad for: duration")
    except ValueError as e:
        assert "must" in str(e) and "duration" in str(e)


def test_single_condition_rule():
    rule = {"name": "freeze", "when": {"metric": "temperature",
                                       "operator": "<=", "value": 35}}
    assert w.evaluate_rule(rule, {"temperature": 30}) is True
    assert w.evaluate_rule(rule, {"temperature": 40}) is False
    assert w.evaluate_rule(rule, {"temperature": None}) is None


def test_alert_and_text_metrics():
    assert w._eval_condition({"metric": "active_alert", "operator": "any"},
                             {"active_alerts": ["Flood Warning"]}, "r") is True
    assert w._eval_condition({"metric": "active_alert", "operator": "contains",
                              "value": "Winter"},
                             {"active_alerts": ["Flood Warning"]}, "r") is False
    assert w._eval_condition({"metric": "short_forecast", "operator": "contains",
                              "value": "rain"},
                             {"short_forecast": "Light Rain"}, "r") is True


def test_load_config_coerces_boolean_payloads(tmp=None):
    import tempfile, os, yaml
    cfg = {
        "location": {"latitude": 1, "longitude": 2},
        "user_agent": "x (a@b.com)",
        "mqtt": {},
        "rules": [{
            "name": "r", "topic": "t", "on_match": True, "on_clear": False,
            "when": {"metric": "temperature", "operator": "<", "value": 5},
        }],
    }
    p = tempfile.mktemp(suffix=".yaml")
    with open(p, "w") as f:
        yaml.safe_dump(cfg, f)
    try:
        loaded = w.load_config(p)
        r = loaded["rules"][0]
        assert r["on_match"] == "True" and isinstance(r["on_match"], str)
        assert r["on_clear"] == "False" and isinstance(r["on_clear"], str)
    finally:
        os.unlink(p)


def test_broker_watch_alert_and_recovery():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    bw = w.BrokerWatch(threshold_minutes=60)
    assert bw.update(True, t0) is None                                  # healthy
    assert bw.update(False, t0) is None                                 # just went down
    assert bw.update(False, t0 + timedelta(minutes=59)) is None         # not yet
    assert bw.update(False, t0 + timedelta(minutes=61)) == "down"       # threshold crossed
    assert bw.update(False, t0 + timedelta(minutes=120)) is None        # no re-alert
    assert bw.update(True, t0 + timedelta(minutes=121)) == "recovered"  # back up
    assert bw.update(True, t0 + timedelta(minutes=122)) is None         # steady


def test_broker_watch_brief_flap_no_alert():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)
    bw = w.BrokerWatch(threshold_minutes=60)
    bw.update(False, t0)
    # recovers before the threshold -> never alerted, so no "recovered" either
    assert bw.update(True, t0 + timedelta(minutes=5)) is None


def test_notify_slack_disabled_is_noop():
    # disabled, and missing token/channel: both must short-circuit without raising
    assert w.notify_slack({"enabled": False}, "x") is False
    assert w.notify_slack({"enabled": True, "channel": "", "bot_token": ""}, "x") is False


def test_slack_token_env_precedence():
    import os
    saved = os.environ.get("SLACK_BOT_TOKEN")
    try:
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-env"
        assert w.slack_token({"bot_token": "xoxb-config"}) == "xoxb-env"
        del os.environ["SLACK_BOT_TOKEN"]
        assert w.slack_token({"bot_token": "xoxb-config"}) == "xoxb-config"
    finally:
        if saved is not None:
            os.environ["SLACK_BOT_TOKEN"] = saved
        else:
            os.environ.pop("SLACK_BOT_TOKEN", None)


def test_validate_config_slack_defaults_and_clamp():
    cfg = w.validate_config(_min_cfg(slack={"enabled": True, "broker_unreachable_minutes": 0}))
    assert cfg["slack"]["enabled"] is True
    assert cfg["slack"]["broker_unreachable_minutes"] == 1   # clamped up from 0
    cfg2 = w.validate_config(_min_cfg())
    assert cfg2["slack"] == {"enabled": False, "bot_token": "", "channel": "",
                             "broker_unreachable_minutes": 60}


def test_validate_config_status_push_defaults():
    cfg = w.validate_config(_min_cfg())
    assert cfg["status_push"] == {"enabled": False, "url": "", "token": ""}
    cfg2 = w.validate_config(_min_cfg(status_push={"enabled": True, "url": "https://x/i.php",
                                                   "token": "t"}))
    assert cfg2["status_push"]["enabled"] is True
    assert cfg2["status_push"]["url"] == "https://x/i.php"


def test_push_status_guards_and_payload():
    # disabled / missing url -> no-op, no network, returns False
    assert w.push_status({"enabled": False}, {"a": 1}) is False
    assert w.push_status({"enabled": True, "url": ""}, {"a": 1}) is False

    # enabled -> posts the snapshot with the token header (stub requests.post)
    captured = {}

    class _Resp:
        status_code = 200

    def _fake_post(url, json=None, headers=None, timeout=None):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _Resp()

    real = w.requests.post
    w.requests.post = _fake_post
    try:
        ok = w.push_status({"enabled": True, "url": "https://h/ingest.php", "token": "sek"},
                           {"updated": "t", "metrics": {}})
        assert ok is True
        assert captured["url"] == "https://h/ingest.php"
        assert captured["headers"]["X-Status-Token"] == "sek"
        assert captured["json"] == {"updated": "t", "metrics": {}}

        # non-2xx -> False
        class _Bad:
            status_code = 401
        w.requests.post = lambda *a, **k: _Bad()
        assert w.push_status({"enabled": True, "url": "https://h/i", "token": "x"}, {}) is False
    finally:
        w.requests.post = real


def test_setup_wizard_renders_valid_config():
    """Whatever the wizard collects, the file it would write must load + validate."""
    try:
        import setup_wizard
    except Exception as e:
        print(f"  SKIP  test_setup_wizard_renders_valid_config ({e})")
        return
    import yaml
    answers = {
        "lat": 41.25, "lon": -74.27, "user_agent": "weather-mqtt-controller (a@b.com)",
        "threshold": 0.25, "lookback": 24, "poll": 15,
        "web_host": "0.0.0.0", "web_port": 8080, "web_user": "admin", "web_pass": "pw",
        "mqtt_host": "localhost", "mqtt_port": 1883, "mqtt_user": "", "mqtt_pass": "",
        "slack_enabled": "false", "slack_channel": "", "slack_token": "", "slack_minutes": 60,
    }
    parsed = yaml.safe_load(setup_wizard._render(answers))
    w.validate_config(parsed)
    assert parsed["web"]["username"] == "admin"
    assert parsed["location"]["latitude"] == 41.25
    # and with no web auth
    answers2 = dict(answers, web_user="", web_pass="")
    w.validate_config(yaml.safe_load(setup_wizard._render(answers2)))


def test_detect_raining_ignores_vicinity_and_fog():
    # precip near but not at the station must NOT read as raining
    assert w.detect_raining({"textDescription": "Showers in Vicinity"}) is False
    assert w.detect_raining({"presentWeather": [{"weather": "rain", "inVicinity": True}]}) is False
    # freezing fog is not precipitation
    assert w.detect_raining({"textDescription": "Freezing Fog"}) is False
    # but real precip still detected, including freezing rain/drizzle
    assert w.detect_raining({"textDescription": "Freezing Rain"}) is True
    assert w.detect_raining({"textDescription": "Light Rain"}) is True


def test_to_mm_handles_inches():
    assert w.to_mm(1, "wmoUnit:[in_i]") == 25.4
    assert w.to_mm(0.5, "wmoUnit:in") == 12.7


def test_validate_rejects_bad_conditions():
    base = {"location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b.com)",
            "mqtt": {}}
    cases = [
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "bogus", "operator": "<", "value": 1}}], "unknown metric"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "temperature", "operator": "contains", "value": 1}}], "not valid"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"metric": "temperature", "operator": "<", "value": "cold"}}], "must be a number"),
        ([{"name": "r", "topic": "t", "on_match": "X",
           "when": {"operator": "<", "value": 1}}], "needs a 'metric'"),
    ]
    for rules, needle in cases:
        try:
            w.validate_config(dict(base, rules=rules))
            raise AssertionError(f"expected ValueError containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"
    # active_alert/any needs no value -> accepted
    w.validate_config(dict(base, rules=[{
        "name": "a", "topic": "t", "on_match": "1",
        "when": {"metric": "active_alert", "operator": "any"}}]))


def test_validate_rejects_duplicate_rule_names():
    cfg = _min_cfg(rules=[
        {"name": "dup", "topic": "t1", "on_match": "X",
         "when": {"metric": "temperature", "operator": "<", "value": 5}},
        {"name": "dup", "topic": "t2", "on_match": "Y",
         "when": {"metric": "humidity", "operator": ">", "value": 5}},
    ])
    try:
        w.validate_config(cfg)
        raise AssertionError("expected ValueError for duplicate rule name")
    except ValueError as e:
        assert "duplicate" in str(e)


def _min_cfg(**over):
    cfg = {
        "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)",
        "mqtt": {},
        "rules": [{
            "name": "r", "topic": "t", "on_match": "ON", "on_clear": "OFF",
            "when": {"metric": "temperature", "operator": "<", "value": 5},
        }],
    }
    cfg.update(over)
    return cfg


def test_validate_clamps_poll_and_lookback():
    cfg = w.validate_config(_min_cfg(poll_interval_minutes=0,
                                     precipitation={"lookback_hours": 100000}))
    assert cfg["poll_interval_minutes"] == w.MIN_POLL_MINUTES
    assert cfg["precipitation"]["lookback_hours"] == w.MAX_LOOKBACK_HOURS
    cfg = w.validate_config(_min_cfg(precipitation={"lookback_hours": -5}))
    assert cfg["precipitation"]["lookback_hours"] == w.MIN_LOOKBACK_HOURS


def test_validate_clamps_qos_and_port():
    cfg = w.validate_config(_min_cfg(mqtt={"qos": 9, "port": 99999}))
    assert cfg["mqtt"]["qos"] == 1
    assert cfg["mqtt"]["port"] == 8080  # clamp helper falls back on out-of-range


def test_validate_rejects_bad_coordinates():
    for bad in ({"latitude": 200, "longitude": 0}, {"latitude": 0, "longitude": 999}):
        try:
            w.validate_config(_min_cfg(location=bad))
            raise AssertionError(f"expected ValueError for {bad}")
        except ValueError:
            pass


def test_validate_rejects_missing_user_agent():
    cfg = _min_cfg()
    cfg["user_agent"] = "   "
    try:
        w.validate_config(cfg)
        raise AssertionError("expected ValueError for blank user_agent")
    except ValueError:
        pass


def test_validate_version_default_and_accepts_one():
    # absent -> defaulted to the current schema version (back-compat)
    cfg = w.validate_config(_min_cfg())
    assert cfg["version"] == w.CURRENT_SCHEMA_VERSION == 1
    # explicit version: 1 is accepted unchanged
    cfg2 = w.validate_config(_min_cfg(version=1))
    assert cfg2["version"] == 1


def test_validate_rejects_unknown_version():
    # a future v2 file must be rejected clearly, not mis-parsed as v1
    for bad in (2, 0, "1", True):
        try:
            w.validate_config(_min_cfg(version=bad))
            raise AssertionError(f"expected ValueError for version={bad!r}")
        except ValueError:
            pass


def test_validate_rejects_empty_rules():
    try:
        w.validate_config(_min_cfg(rules=[]))
        raise AssertionError("expected ValueError for empty rules")
    except ValueError:
        pass


def test_validate_string_numbers_are_coerced():
    cfg = w.validate_config(_min_cfg(
        location={"latitude": "41.5", "longitude": "-74.2"},
        poll_interval_minutes="30"))
    assert cfg["location"]["latitude"] == 41.5
    assert cfg["poll_interval_minutes"] == 30


def test_webui_settings_roundtrip_and_validation():
    """Web UI save path: valid POST persists, invalid POST is rejected.

    Skipped automatically if Flask/ruamel (the optional web extras) aren't
    installed, so the monitor-only test run still passes.
    """
    try:
        import webui
    except Exception as e:  # Flask / ruamel not installed -> skip, don't fail
        print(f"  SKIP  test_webui_settings_roundtrip_and_validation ({e})")
        return

    import tempfile, os, yaml
    base = {
        "location": {"latitude": 41.0, "longitude": -74.0},
        "user_agent": "x (a@b.com)",
        "poll_interval_minutes": 15,
        "precipitation": {"lookback_hours": 24},
        "mqtt": {"host": "localhost", "port": 1883, "qos": 1, "retain": True},
        "rules": [{"name": "irrigation_rain_inhibit", "topic": "irrigation/rain_inhibit",
                   "on_match": "INHIBIT", "on_clear": "ALLOW",
                   "when": {"metric": "is_raining", "operator": "==", "value": True}}],
        "web": {"enabled": True, "host": "0.0.0.0", "port": 8080,
                "username": "", "password": ""},
    }
    p = tempfile.mktemp(suffix=".yaml")
    with open(p, "w") as f:
        yaml.safe_dump(base, f)
    webui.CONFIG_PATH = p
    app = webui.app
    app.config["TESTING"] = True
    c = app.test_client()
    try:
        good = {
            "latitude": "40.5", "longitude": "-75.5", "station_id": "KPHL",
            "poll_interval_minutes": "20", "user_agent": "y (c@d.com)",
            "lookback_hours": "12", "always_publish": "true",
            "mqtt_host": "broker", "mqtt_port": "8883", "mqtt_username": "u",
            "mqtt_password": "pw", "mqtt_client_id": "cid", "mqtt_qos": "2",
            "mqtt_retain": "false", "status_topic": "s/t",
            "web_host": "127.0.0.1", "web_port": "9090",
            "web_username": "", "web_password": "",
        }
        r = c.post("/settings", data=good)
        assert b"Settings saved" in r.data, "valid settings should save"
        saved = yaml.safe_load(open(p))
        assert saved["mqtt"]["qos"] == 2 and saved["mqtt"]["client_id"] == "cid"
        assert saved["web"]["port"] == 9090 and saved["poll_interval_minutes"] == 20
        # the monitor must still accept what the UI wrote
        w.validate_config(yaml.safe_load(open(p)))

        bad = dict(good, latitude="999")
        r = c.post("/settings", data=bad)
        assert b"Could not save" in r.data, "out-of-range latitude must be rejected"
        # file unchanged by the rejected save
        assert yaml.safe_load(open(p))["location"]["latitude"] == 40.5

        # healthz + api/state respond
        assert c.get("/healthz").status_code in (200, 500)
        assert c.get("/api/state").status_code in (200, 503)
    finally:
        for suffix in ("", ".bak", ".tmp"):
            try:
                os.unlink(p + suffix)
            except OSError:
                pass


def test_webui_structured_rule_builder():
    """The form builder's JSON -> rules conversion produces output the monitor
    accepts, with correctly typed values; bad input is rejected."""
    try:
        import webui
    except Exception as e:
        print(f"  SKIP  test_webui_structured_rule_builder ({e})")
        return
    import yaml, copy

    items = [
        {"name": "irr", "description": "hold", "topic": "irrigation/x",
         "on_match": "INHIBIT", "on_clear": "ALLOW", "combine": "any",
         "conditions": [
             {"metric": "is_raining", "operator": "==", "value": "true"},
             {"metric": "precip_accum_in", "operator": ">=", "value": "0.25"}]},
        {"name": "freeze", "topic": "f/z", "on_match": "ON", "combine": "any",
         "conditions": [{"metric": "temperature", "operator": "<=", "value": "35"}]},
        {"name": "alert", "topic": "f/a", "on_match": "1", "combine": "any",
         "conditions": [{"metric": "active_alert", "operator": "any", "value": ""}]},
    ]
    built = webui._rules_from_structured(items)
    parsed = yaml.safe_load(webui.dump_raw({"rules": built}))["rules"]

    # value typing survived the YAML round-trip
    assert parsed[0]["when"]["any"][0]["value"] is True            # bool
    assert parsed[0]["when"]["any"][1]["value"] == 0.25            # float
    assert parsed[1]["when"]["value"] == 35                        # int, single condition flattened
    assert "value" not in parsed[2]["when"]                        # active_alert/any -> no value
    # the monitor accepts what the builder produced
    w.validate_config(copy.deepcopy({
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b)",
        "mqtt": {}, "rules": parsed}))

    # round-trips back to the editable shape
    struct = webui._rule_to_structured(parsed[0])
    assert struct["combine"] == "any" and len(struct["conditions"]) == 2
    assert struct["conditions"][0] == {"metric": "is_raining", "operator": "==",
                                       "value": "true", "for": ""}

    # rejections
    for bad, needle in [
        ([{"name": "", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "<", "value": "1"}]}], "name is required"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "bogus", "operator": "<", "value": "1"}]}], "unknown metric"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "contains", "value": "1"}]}], "not valid"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any",
           "conditions": [{"metric": "temperature", "operator": "<", "value": "abc"}]}], "numeric"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": []}], "at least one condition"),
        ([], "at least one rule"),
    ]:
        try:
            webui._rules_from_structured(bad)
            raise AssertionError(f"expected rejection containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def test_webui_builder_advanced_constructs():
    """The form builder round-trips the constructs added in Phase 1: between/in,
    the value-less `changed` operator, a per-condition `for:` sustain, and a
    disabled rule -- producing YAML the monitor accepts."""
    try:
        import webui
    except Exception as e:
        print(f"  SKIP  test_webui_builder_advanced_constructs ({e})")
        return
    import yaml, copy

    items = [
        {"name": "comfort", "topic": "f/comfort", "on_match": "ON", "on_clear": "OFF",
         "enabled": True, "combine": "all", "conditions": [
             {"metric": "temperature", "operator": "between", "value": "40, 80", "for": "10m"},
             {"metric": "humidity", "operator": "in", "value": "30, 50, 70", "for": ""},
         ]},
        {"name": "alert_pulse", "topic": "f/pulse", "on_match": "1", "enabled": True,
         "combine": "any", "conditions": [
             {"metric": "temperature", "operator": "changed", "value": "", "for": ""}]},
        {"name": "off_rule", "topic": "f/off", "on_match": "X", "enabled": False,
         "combine": "any", "conditions": [
             {"metric": "wind_speed_mph", "operator": ">", "value": "25", "for": ""}]},
    ]
    built = webui._rules_from_structured(items)
    parsed = yaml.safe_load(webui.dump_raw({"rules": built}))["rules"]

    assert parsed[0]["when"]["all"][0]["value"] == [40, 80]          # between -> list
    assert parsed[0]["when"]["all"][0]["for"] == "10m"               # for preserved
    assert parsed[0]["when"]["all"][1]["value"] == [30, 50, 70]      # in -> numeric list
    assert parsed[1]["when"]["operator"] == "changed"
    assert "value" not in parsed[1]["when"]                          # changed -> no value
    assert parsed[2]["enabled"] is False                             # disabled preserved
    assert "enabled" not in parsed[0]                                # default-on stays clean
    # the monitor accepts what the builder produced
    w.validate_config(copy.deepcopy({
        "location": {"latitude": 1, "longitude": 2}, "user_agent": "x (a@b)",
        "mqtt": {}, "rules": parsed}))

    # round-trips back to the editable shape
    s0 = webui._rule_to_structured(parsed[0])
    assert s0["conditions"][0] == {"metric": "temperature", "operator": "between",
                                   "value": "40, 80", "for": "10m"}
    assert webui._rule_to_structured(parsed[2])["enabled"] is False

    # rejections surface clearly
    for bad, needle in [
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "temperature", "operator": "between", "value": "40", "for": ""}]}], "two numbers"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "humidity", "operator": "in", "value": "", "for": ""}]}], "at least one"),
        ([{"name": "r", "topic": "t", "on_match": "x", "combine": "any", "conditions": [
            {"metric": "temperature", "operator": ">", "value": "5", "for": "soon"}]}], "valid duration"),
    ]:
        try:
            webui._rules_from_structured(bad)
            raise AssertionError(f"expected rejection containing {needle!r}")
        except ValueError as e:
            assert needle in str(e), f"got {e!r}, wanted {needle!r}"


def run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if run() else 0)

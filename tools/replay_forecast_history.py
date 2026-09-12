"""Replay recorded forecast inputs through two versions of the real templates.

Input is a JSON list of {label, data: {period, entities}} windows from
ha_get_history(minimal_response=False, significant_changes_only=False).
No Home Assistant connection is made. Keep real exports and output private.

This is a settled-state replay, not an emulation of HA's event scheduling or
physical battery behavior. Recorded effective forecasts and tomorrow scores
are inputs; the new Sonnentag score is calculated from recorded source values.
If sun.sun's next-event attributes were excluded by Recorder, the recorded
sensor.sun_next_rising/setting values supply them (second-level precision).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from tests.ha_harness import (FakeHass, TZ, _setup, find_template_entity,
                              load_yaml)
import jinja2


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("History timestamps must include a timezone")
    # Sort on the UTC timeline: two 02:30 readings during the autumn clock
    # change are distinct, even though local datetimes can compare equal.
    return parsed.astimezone(dt.timezone.utc)


class ForecastVersion:
    def __init__(self, config):
        self.hass = FakeHass()
        env = _setup(jinja2.Environment(), self.hass)
        self.env = env
        self.templates = {}
        for kind, uid in [("sensor", "opti_forecast_score"),
                          ("sensor", "opti_forecast_score_sonnentag"),
                          ("binary_sensor", "opti_peak_horizont_lang")]:
            try:
                entity = find_template_entity(config, kind, uid)
            except KeyError:
                if uid == "opti_forecast_score":
                    raise
                continue
            self.templates[uid] = {
                key: env.from_string(entity[key])
                for key in ("state", "availability") if key in entity
            }
        self.horizon = None

    def evaluate(self, now, states, attrs):
        self.hass.now_value = now.astimezone(TZ)
        self.hass.states_map = dict(states)
        self.hass.attrs_map = {key: dict(value) for key, value in attrs.items()}
        sun_attrs = self.hass.attrs_map.setdefault("sun.sun", {})
        for key in ("next_rising", "next_setting"):
            if key not in sun_attrs:
                value = states.get("sensor.sun_" + key)
                if value and value not in ("unknown", "unavailable"):
                    sun_attrs[key] = value

        def entity(uid):
            templates = self.templates[uid]
            availability = templates.get("availability")
            if availability is not None and availability.render().strip().lower() not in (
                    "true", "on", "yes", "1"):
                return "unavailable"
            return templates["state"].render().strip()

        sonnentag = None
        if "opti_forecast_score_sonnentag" in self.templates:
            sonnentag = entity("opti_forecast_score_sonnentag")
            self.hass.states_map["sensor.opti_forecast_score_sonnentag"] = sonnentag
        if "opti_peak_horizont_lang" in self.templates:
            self.hass.this_state = self.horizon
            _setup(self.env, self.hass)
            self.horizon = "on" if entity("opti_peak_horizont_lang").lower() == "true" else "off"
        return {"score": entity("opti_forecast_score"),
                "sonnentag": sonnentag, "horizon_long": self.horizon}


def replay_window(window, before_config, after_config):
    data = window["data"]
    if not data.get("success"):
        raise ValueError("History query was not successful")
    if data.get("query_params", {}).get("minimal_response") is not False:
        raise ValueError("Replay requires history with full attributes")
    if data.get("query_params", {}).get("significant_changes_only") is not False:
        raise ValueError("Replay requires all attribute changes")
    start, end = (timestamp(data["period"][key]) for key in ("start", "end"))
    if end <= start:
        raise ValueError("History window must have positive duration")
    updates = defaultdict(list)
    missing = []
    initially_missing = []
    for item in data["entities"]:
        if item.get("has_more"):
            raise ValueError(f"Truncated history: {item['entity_id']}")
        records = sorted(item.get("states", []), key=lambda record:
                         timestamp(record.get("last_updated") or record["last_changed"]))
        if not records:
            missing.append(item["entity_id"])
        elif min(timestamp(r.get("last_updated") or r["last_changed"])
                 for r in records) > start:
            initially_missing.append(item["entity_id"])
        for record in records:
            if not isinstance(record.get("attributes"), dict):
                raise ValueError(f"Missing attributes: {item['entity_id']}")
            when = timestamp(record.get("last_updated") or record["last_changed"])
            # HA may retain an initial record's original timestamp. Replay it
            # as initial state, never as an observation outside this window.
            when = max(start, when)
            if when <= end:
                updates[when].append((item["entity_id"], record))
    updates[start]
    tick = start.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    while tick <= end:
        updates[tick]
        tick += dt.timedelta(minutes=1)
    before, after = ForecastVersion(before_config), ForecastVersion(after_config)
    states, attrs, rows = {}, {}, []
    for when, changes in sorted(updates.items()):
        for entity_id, record in changes:
            states[entity_id] = record["state"]
            attrs[entity_id] = record["attributes"]
        rows.append({"time": when.astimezone(TZ).isoformat(),
                     "recorded_score": states.get("sensor.opti_forecast_score"),
                     "before": before.evaluate(when, states, attrs),
                     "after": after.evaluate(when, states, attrs)})
    return {"label": window.get("label", "window"), "missing_entities": missing,
            "initially_missing_entities": initially_missing,
            "initial_horizon": "unknown (no assumed restored state)",
            "snapshots": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("history", type=pathlib.Path)
    parser.add_argument("--before", required=True, type=pathlib.Path)
    parser.add_argument("--after", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    before, after = load_yaml(args.before), load_yaml(args.after)
    windows = json.loads(args.history.read_text())
    results = [replay_window(window, before, after) for window in windows]
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
    print(f"Replayed {len(results)} windows; output contains private historical values.")


if __name__ == "__main__":
    main()

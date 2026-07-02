import datetime as dt

from .ha_harness import REPO, TZ, FakeHass, load_yaml, render, render_native


def test_render_basic_state():
    hass = FakeHass(states={"sensor.x": "42.5"})
    assert render(hass, "{{ states('sensor.x') | float(0) + 1 }}") == "43.5"


def test_has_value_and_defaults():
    hass = FakeHass(states={"sensor.a": "unavailable"})
    assert render(hass, "{{ has_value('sensor.a') }}") == "False"
    assert render(hass, "{{ states('sensor.fehlt') | float(-1) }}") == "-1"


def test_native_returns_dict():
    hass = FakeHass()
    result = render_native(hass, "{{ {'a': 1, 'b': none} }}")
    assert result == {"a": 1, "b": None}


def test_time_functions():
    now = dt.datetime(2026, 1, 15, 18, 30, tzinfo=TZ)
    hass = FakeHass(now=now)
    out = render(hass, "{{ (today_at('00:00') + timedelta(hours=26)).isoformat() }}")
    assert out == "2026-01-16T02:00:00+01:00"


def test_load_repo_yaml():
    cfg = load_yaml(REPO / "packages" / "opti_derived.yaml")
    assert "template" in cfg

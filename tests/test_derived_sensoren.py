import datetime as dt

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render


def _cfg():
    return load_yaml(REPO / "packages" / "opti_derived.yaml")


def _entity(kind, unique_id):
    return find_template_entity(_cfg(), kind, unique_id)


def _score_state(hass):
    entity = _entity("sensor", "opti_forecast_score")
    return render(hass, entity["state"])


def _score_attr(hass, attr):
    entity = _entity("sensor", "opti_forecast_score")
    return render(hass, entity["attributes"][attr])


def _target_soc_state(hass):
    entity = _entity("sensor", "opti_target_soc")
    return render(hass, entity["state"])


def _target_soc_attr(hass, attr):
    entity = _entity("sensor", "opti_target_soc")
    return render(hass, entity["attributes"][attr])


# ---------------------------------------------------------------------------
# 2a. Abend-Fallback opti_forecast_score
# ---------------------------------------------------------------------------

def test_score_abend_fallback():
    # 21:45, next_setting zeigt auf morgen -> nach_sunset. score_tomorrow "8"
    # verfuegbar -> uebernommen statt der (nachts nutzlosen) alten Formel.
    now = dt.datetime(2026, 1, 15, 21, 45, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 16, 16, 30, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "0",
            "sensor.opti_battery_capacity_kwh": "10",
            "sensor.opti_soc": "50",
            "sensor.opti_forecast_score_tomorrow": "8",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_state(hass) == "8"

    # score_tomorrow unavailable -> Fallback greift nicht, alte Formel bleibt
    # (remaining=0 -> Score 0, unabhaengig von nach_sunset).
    hass2 = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "0",
            "sensor.opti_battery_capacity_kwh": "10",
            "sensor.opti_soc": "50",
            "sensor.opti_forecast_score_tomorrow": "unavailable",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_state(hass2) == "0"


def test_score_vor_sonnenaufgang_normale_formel():
    # 05:00, next_setting zeigt auf HEUTE Abend (18:00) -> kein Fallback,
    # normale Formel laeuft, obwohl es noch dunkel ist.
    now = dt.datetime(2026, 1, 15, 5, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "12",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "95",
            "sensor.opti_house_consumption_w": "400",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    # needed = 13 * (1 - 0.95) = 0.65; h = 13h; verbrauch = 0.4*13 = 5.2kWh
    # surplus = max(12 - 5.2, 0) = 6.8 -> ratio > 1 -> Score gedeckelt auf 10.
    assert int(_score_state(hass)) > 0


# ---------------------------------------------------------------------------
# 2b. Geglaetteter Hausverbrauch
# ---------------------------------------------------------------------------

def test_score_geglaetteter_verbrauch():
    # 08:00, next_setting HEUTE 18:00 (kein Fallback) -> h=10h.
    # cap=13kWh, soc=0% -> needed=13kWh. remaining=12kWh.
    now = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    base_states = {
        "sensor.opti_forecast_remaining_today_kwh": "12",
        "sensor.opti_battery_capacity_kwh": "13",
        "sensor.opti_soc": "0",
        "sensor.opti_house_consumption_w": "2400",
    }

    # 60min-Sensor 400W -> verbrauch = 0.4*10 = 4kWh -> surplus = 8kWh
    # ratio = 8/13 = 0.6154 -> Score = round(6.154) = 6.
    hass_60min = FakeHass(
        states={**base_states, "sensor.opti_house_consumption_60min_w": "400"},
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_state(hass_60min) == "6"

    # 60min-Sensor unavailable -> faellt auf Momentanwert 2400W zurueck ->
    # verbrauch = 2.4*10 = 24kWh -> surplus = max(12-24,0) = 0 -> Score 0.
    hass_momentary = FakeHass(
        states={**base_states, "sensor.opti_house_consumption_60min_w": "unavailable"},
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_state(hass_momentary) == "0"


# ---------------------------------------------------------------------------
# 2c. estimate10-Guard
# ---------------------------------------------------------------------------

def test_score_estimate10_null_ignoriert():
    # estimate10 = 0 (Solcast-Artefakt "keine Schaetzung") darf remaining
    # nicht auf 0 druecken - median (8 kWh) muss statt dessen zaehlen.
    now = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "8",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "0",
        },
        attrs={
            "sun.sun": {"next_setting": next_setting},
            "sensor.opti_forecast_today_kwh": {"estimate10": 0},
        },
        now=now,
    )
    assert float(_score_attr(hass, "remaining_kwh")) == 8.0


# ---------------------------------------------------------------------------
# 2d. Division-durch-0-Guard opti_target_soc
# ---------------------------------------------------------------------------

def test_target_soc_cap_null():
    hass = FakeHass(
        states={
            "sensor.opti_battery_capacity_kwh": "0",
            "sensor.opti_forecast_remaining_today_kwh": "5",
            "input_number.maxsoc": "95",
            "input_number.minsoc": "10",
            "input_boolean.hausakku_aus_netz_laden": "off",
        },
        this_attributes={},
    )
    assert float(_target_soc_state(hass)) == 95.0


def test_target_soc_geglaettet():
    # netzladen off, kein sun.sun-next_setting -> remaining_hours=6 (else-Zweig).
    # restproduktion=8kWh, cap=10kWh.
    base_states = {
        "sensor.opti_battery_capacity_kwh": "10",
        "sensor.opti_forecast_remaining_today_kwh": "8",
        "input_number.maxsoc": "95",
        "input_number.minsoc": "10",
        "input_boolean.hausakku_aus_netz_laden": "off",
        "sensor.opti_house_consumption_w": "1500",
    }

    # 60min-Sensor 500W -> net_available = max(0, 8 - 0.5*6) = 5 -> ratio=0.5
    hass_60min = FakeHass(
        states={**base_states, "sensor.opti_house_consumption_60min_w": "500"},
        this_attributes={},
    )
    assert float(_target_soc_attr(hass_60min, "ratio")) == 0.5

    # 60min unavailable -> Momentanwert 1500W -> net_available = max(0, 8-9)=0
    hass_momentary = FakeHass(
        states={**base_states, "sensor.opti_house_consumption_60min_w": "unavailable"},
        this_attributes={},
    )
    assert float(_target_soc_attr(hass_momentary, "ratio")) == 0.0

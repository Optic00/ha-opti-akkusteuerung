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


def _score_availability(hass):
    entity = _entity("sensor", "opti_forecast_score")
    return render(hass, entity["availability"])


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
            "sensor.opti_forecast_effective_remaining_kwh": "0",
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
            "sensor.opti_forecast_effective_remaining_kwh": "0",
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
    # normale Formel laeuft, obwohl es noch dunkel ist. remaining kommt jetzt
    # direkt vom zentralen Effective-Sensor (Feature #30) statt aus einem
    # lokalen median/P10-Blend - die P10-Auswahl selbst sitzt in
    # test_forecast_effective.py.
    now = dt.datetime(2026, 1, 15, 5, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_effective_remaining_kwh": "12",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "95",
            "sensor.opti_house_consumption_w": "400",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    # needed = 13 * (1 - 0.95) = 0.65; h = 13h; verbrauch = 0.4*13 = 5.2kWh
    # surplus = max(12 - 5.2, 0) = 6.8 -> ratio > 1 -> Score gedeckelt auf 10.
    assert _score_state(hass) == "10"


# ---------------------------------------------------------------------------
# 2b. Geglaetteter Hausverbrauch
# ---------------------------------------------------------------------------

def test_score_geglaetteter_verbrauch():
    # 08:00, next_setting HEUTE 18:00 (kein Fallback) -> h=10h.
    # cap=13kWh, soc=0% -> needed=13kWh. remaining=12kWh (Effective-Sensor).
    now = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    base_states = {
        "sensor.opti_forecast_effective_remaining_kwh": "12",
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
# 2c. remaining_kwh / pv_surplus_kwh / ueberschuss_ueber_voll_kwh lesen den
#    zentralen Effective-Sensor (Feature #30). Die P10-/estimate10-Guard-
#    Semantik selbst (inkl. "P10 kommt vom Rest-Tag-, nicht vom Ganztags-
#    Sensor") sitzt jetzt in test_forecast_effective.py - hier wird nur noch
#    geprueft, dass alle drei Attribute + der State konsistent denselben
#    Effective-Wert konsumieren.
# ---------------------------------------------------------------------------

def test_score_p10_nutzt_rest_tag_sensor():
    # Effective-Sensor liefert 1.0 kWh (waere z.B. das Rest-P10 bei Median 2.0).
    now = dt.datetime(2026, 1, 15, 14, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_effective_remaining_kwh": "1.0",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "0",
            "sensor.opti_house_consumption_w": "0",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert float(_score_attr(hass, "remaining_kwh")) == 1.0
    # Score rechnet mit remaining=1.0: needed=13, verbrauch=0 -> surplus=1.0
    # -> ratio = 1/13 -> round(0.769) = 1.
    assert _score_state(hass) == "1"
    # Auch die Surplus-Attribute muessen denselben Effective-Wert nutzen
    # (verbrauch=0 -> pv_surplus = remaining = 1.0).
    assert float(_score_attr(hass, "pv_surplus_kwh")) == 1.0

    # ueberschuss_ueber_voll diskriminierend pruefen: cap=0.5, soc=0
    # -> needed=0.5 -> ueberschuss = 1.0 - 0.5 = 0.5.
    hass_klein = FakeHass(
        states={
            "sensor.opti_forecast_effective_remaining_kwh": "1.0",
            "sensor.opti_battery_capacity_kwh": "0.5",
            "sensor.opti_soc": "0",
            "sensor.opti_house_consumption_w": "0",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert float(_score_attr(hass_klein, "ueberschuss_ueber_voll_kwh")) == 0.5


def test_score_surplus_attribute_estimate10_null_guard():
    # Effective-Sensor liefert 8.0 kWh (waere z.B. der Median, falls estimate10
    # als "keine Schaetzung" verworfen wurde - dieser Guard sitzt jetzt in
    # test_forecast_effective.py).
    # cap=5, soc=0 -> needed=5; verbrauch=0 -> surplus=8, ueberschuss=8-5=3.
    now = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_effective_remaining_kwh": "8",
            "sensor.opti_battery_capacity_kwh": "5",
            "sensor.opti_soc": "0",
            "sensor.opti_house_consumption_w": "0",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert float(_score_attr(hass, "pv_surplus_kwh")) == 8.0
    assert float(_score_attr(hass, "ueberschuss_ueber_voll_kwh")) == 3.0


# ---------------------------------------------------------------------------
# 2f. Availability opti_forecast_score
# ---------------------------------------------------------------------------

def test_score_availability_abend_entkoppelt():
    # Nach Sonnenuntergang mit verfuegbarem Morgen-Score darf der Score nicht
    # unavailable werden, nur weil die Tages-Sensoren wegkippen - der
    # Abend-Zweig braucht sie nicht (Kommentar im Sensor verspricht das).
    now = dt.datetime(2026, 1, 15, 21, 45, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 16, 16, 30, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_score_tomorrow": "8",
            "sensor.opti_forecast_remaining_today_kwh": "unavailable",
            "sensor.opti_battery_capacity_kwh": "unavailable",
            "sensor.opti_soc": "unavailable",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_availability(hass) == "True"
    assert _score_state(hass) == "8"


def test_score_availability_abend_fallback_alte_formel():
    # Nach Sonnenuntergang OHNE Morgen-Score: Fallback auf die alte Formel,
    # dafuer reichen die Tages-Sensoren (opti_forecast_today_kwh wird nicht
    # mehr verlangt).
    now = dt.datetime(2026, 1, 15, 21, 45, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 16, 16, 30, tzinfo=TZ).isoformat()
    hass = FakeHass(
        states={
            "sensor.opti_forecast_score_tomorrow": "unavailable",
            "sensor.opti_forecast_remaining_today_kwh": "0",
            "sensor.opti_battery_capacity_kwh": "10",
            "sensor.opti_soc": "50",
        },
        attrs={"sun.sun": {"next_setting": next_setting}},
        now=now,
    )
    assert _score_availability(hass) == "True"
    assert _score_state(hass) == "0"


def test_score_availability_tag():
    # Tagsueber braucht die Formel Rest-Prognose, Kapazitaet und SoC -
    # aber NICHT mehr opti_forecast_today_kwh (Score liest P10 seit dem
    # Quell-Fix vom Rest-Tag-Sensor).
    now = dt.datetime(2026, 1, 15, 8, 0, tzinfo=TZ)
    next_setting = dt.datetime(2026, 1, 15, 18, 0, tzinfo=TZ).isoformat()
    attrs = {"sun.sun": {"next_setting": next_setting}}

    hass_ok = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "8",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "50",
        },
        attrs=attrs,
        now=now,
    )
    assert _score_availability(hass_ok) == "True"

    hass_missing = FakeHass(
        states={
            "sensor.opti_forecast_remaining_today_kwh": "unavailable",
            "sensor.opti_battery_capacity_kwh": "13",
            "sensor.opti_soc": "50",
        },
        attrs=attrs,
        now=now,
    )
    assert _score_availability(hass_missing) == "False"


# ---------------------------------------------------------------------------
# 2d. Division-durch-0-Guard opti_target_soc
# ---------------------------------------------------------------------------

def test_target_soc_cap_null():
    hass = FakeHass(
        states={
            "sensor.opti_battery_capacity_kwh": "0",
            "sensor.opti_forecast_effective_remaining_kwh": "5",
            "input_number.maxsoc": "95",
            "input_number.minsoc": "10",
            "input_boolean.hausakku_aus_netz_laden": "off",
        },
        this_attributes={},
    )
    assert float(_target_soc_state(hass)) == 95.0


def test_target_soc_geglaettet():
    # netzladen off, kein sun.sun-next_setting -> remaining_hours=6 (else-Zweig).
    # restproduktion=8kWh (Effective-Sensor), cap=10kWh.
    base_states = {
        "sensor.opti_battery_capacity_kwh": "10",
        "sensor.opti_forecast_effective_remaining_kwh": "8",
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


# ---------------------------------------------------------------------------
# 2e. opti_target_soc konsumiert den zentralen Effective-Sensor (Feature #30)
#    an allen fuenf Stellen (state/branch/level/ratio/net_available_kwh)
#    konsistent. Die P10-/estimate10-Sicherheitsnetz-Semantik selbst (Median
#    vs. P10, Guards fuer fehlendes/nulles/hoeheres estimate10) sitzt jetzt in
#    test_forecast_effective.py, direkt am Effective-Sensor - keine
#    Semantik-Abdeckung verloren, nur die Zustaendigkeit verschoben.
# ---------------------------------------------------------------------------

def test_target_soc_effective_sensor_alle_fuenf_stellen():
    # Effective-Sensor liefert 3.0 kWh (entspraeche z.B. einem P10-gedeckelten
    # Median von 8 kWh - siehe test_forecast_effective.py fuer diese Auswahl).
    # ratio = 3/10 = 0.3 < 0.375 -> tiefste Stufe -> maxsoc statt 90%.
    # Prueft alle fuenf restproduktion-Stellen gleichzeitig (state/level/ratio/
    # net_available_kwh/branch) - das deckt die Fuenffach-Duplikation ab.
    hass = FakeHass(
        states={
            "sensor.opti_battery_capacity_kwh": "10",
            "sensor.opti_forecast_effective_remaining_kwh": "3",
            "sensor.opti_house_consumption_w": "0",
            "input_number.maxsoc": "95",
            "input_number.minsoc": "10",
            "input_boolean.hausakku_aus_netz_laden": "off",
        },
        this_attributes={},
    )
    assert float(_target_soc_state(hass)) == 95.0
    assert float(_target_soc_attr(hass, "net_available_kwh")) == 3.0
    assert float(_target_soc_attr(hass, "ratio")) == 0.3
    assert _target_soc_attr(hass, "level") == "0"
    assert "ratio=0.3" in _target_soc_attr(hass, "branch")

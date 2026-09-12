"""opti_forecast_score zwischen Mitternacht und Sonnenaufgang (Mitternachts-Dip)."""
import datetime as dt

from .ha_harness import REPO, TZ, FakeHass, find_template_entity, load_yaml, render

# 00:00:00,14: Abend-Fallback ist vorbei (next_setting zeigt auf heute), die
# Rest-Prognose steht noch auf 0, der Sonnenaufgang liegt heute.
MITTERNACHT = dt.datetime(2026, 9, 11, 0, 0, 0, 140000, tzinfo=TZ)
SETTING_HEUTE = "2026-09-11T19:40:00+02:00"
RISING_HEUTE = "2026-09-11T06:58:00+02:00"


def _render(hass, part="state"):
    entity = find_template_entity(
        load_yaml(REPO / "packages" / "opti_derived.yaml"), "sensor", "opti_forecast_score")
    template = entity[part] if part in ("state", "availability") else entity["attributes"][part]
    return render(hass, template)


def _hass(*, now=MITTERNACHT, next_setting=SETTING_HEUTE, next_rising=RISING_HEUTE,
          sonnentag="10", tomorrow="10", remaining="0", cap="10", soc="50"):
    return FakeHass(
        now=now,
        states={
            "sensor.opti_forecast_score_sonnentag": sonnentag,
            "sensor.opti_forecast_score_tomorrow": tomorrow,
            "sensor.opti_forecast_remaining_today_kwh": remaining,
            "sensor.opti_forecast_effective_remaining_kwh": remaining,
            "sensor.opti_battery_capacity_kwh": cap,
            "sensor.opti_soc": soc,
            "sensor.opti_house_consumption_60min_w": "500",
        },
        attrs={"sun.sun": {"next_setting": next_setting, "next_rising": next_rising}},
    )


def test_mitternacht_kein_dip_auf_null():
    # Frueher: Tages-Formel mit remaining=0 -> Score 0 fuer Sekundenbruchteile.
    hass = _hass()
    assert _render(hass) == "10"
    assert _render(hass, "reason") == "Nacht -> Sonnentag-Score"


def test_nachts_zaehlt_sonnentag_auch_wenn_er_schlecht_ist():
    # Keine pauschale Unterdrueckung: ein echter schlechter Sonnentag bleibt 0,
    # obwohl die Tages-Formel mit 20 kWh Rest-Prognose 10 ergaebe.
    assert _render(_hass(sonnentag="0", remaining="20")) == "0"


def test_ohne_sonnentag_score_bleibt_tagesformel():
    hass = _hass(sonnentag="unavailable", remaining="0")
    assert _render(hass, "availability") == "True"
    assert _render(hass) == "0"
    assert _render(hass, "reason") == "PV-Fit"


def test_availability_nachts_braucht_nur_sonnentag():
    hass = _hass(sonnentag="7", remaining="unavailable", cap="unavailable",
                 soc="unavailable")
    assert _render(hass, "availability") == "True"
    assert _render(hass) == "7"


def test_nach_sonnenaufgang_wieder_tagesformel():
    # Der Formelwechsel liegt jetzt am Sonnenaufgang: next_rising zeigt auf
    # morgen, also rechnet wieder die Tages-Formel (remaining 0 -> 0).
    hass = _hass(now=dt.datetime(2026, 9, 11, 7, 30, tzinfo=TZ),
                 next_rising="2026-09-12T07:00:00+02:00", sonnentag="10")
    assert _render(hass) == "0"
    assert _render(hass, "reason") == "PV-Fit"


def test_abend_bleibt_morgen_score():
    # Vor Mitternacht unveraendert score_tomorrow, nicht der Sonnentag-Score.
    hass = _hass(now=dt.datetime(2026, 9, 10, 22, 0, tzinfo=TZ),
                 tomorrow="7", sonnentag="3")
    assert _render(hass) == "7"
    assert _render(hass, "reason") == "Abend -> Morgen-Score"

"""Isolierte native HA-Pruefung der Nacht-Sensoren aus opti_derived.yaml.

Ausfuehren in einer separaten Umgebung mit homeassistant==2026.9.1:
    python tools/validate_night_ha.py

Die Instanzen verwenden ausschliesslich synthetische States in einem neuen
Temp-Verzeichnis. Es werden weder Konfiguration noch echte Integrationen einer
Home-Assistant-Installation geladen.
"""

import asyncio
import copy
from datetime import datetime
import pathlib
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

import yaml
from homeassistant import bootstrap, config_entries, loader
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


ROOT = pathlib.Path(__file__).resolve().parent.parent
LOCAL_TIME = datetime(2026, 1, 15, 2, 0, tzinfo=ZoneInfo("Europe/Berlin"))


def _entity(config, domain, unique_id):
    return next(
        entity
        for block in config["template"]
        for entity in block.get(domain, [])
        if entity.get("unique_id") == unique_id
    )


async def _new_hass(config_dir):
    hass = HomeAssistant(config_dir)
    hass.config.time_zone = "Europe/Berlin"
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await bootstrap.async_load_base_functionality(hass)
    return hass


async def _settle(hass):
    # Template dependency updates may enqueue another template update.
    await hass.async_block_till_done()
    await hass.async_block_till_done()


def _assert_state(hass, entity_id, expected):
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} wurde nicht angelegt"
    assert state.state == expected, (entity_id, expected, state)
    return state


async def _validate_horizon(horizon):
    config_dir = tempfile.mkdtemp(prefix="opti-night-horizon-ha-")
    hass = await _new_hass(config_dir)
    hass.states.async_set("sensor.opti_forecast_score_sonnentag", "3")
    assert await async_setup_component(
        hass, "template", {"template": [{"binary_sensor": [copy.deepcopy(horizon)]}]}
    )
    await hass.async_start()
    await _settle(hass)

    async def score(value, expected):
        hass.states.async_set("sensor.opti_forecast_score_sonnentag", value)
        await _settle(hass)
        _assert_state(hass, "binary_sensor.opti_peak_horizont_lang", expected)

    _assert_state(hass, "binary_sensor.opti_peak_horizont_lang", "off")
    await score("1", "on")
    await score("2", "on")
    await score("3", "off")
    await score("2", "off")
    await score("unavailable", "on")
    await score("3", "off")
    await hass.async_stop()

    # Der gespeicherte Zustand ist off. Vor dem Registrieren der Listener liegt
    # bereits ein neuer Score vor; der HA-Start muss ihn trotzdem auswerten.
    restarted = await _new_hass(config_dir)
    restarted.states.async_set("sensor.opti_forecast_score_sonnentag", "1")
    assert await async_setup_component(
        restarted,
        "template",
        {"template": [{"binary_sensor": [copy.deepcopy(horizon)]}]},
    )
    await _settle(restarted)
    _assert_state(restarted, "binary_sensor.opti_peak_horizont_lang", "off")
    await restarted.async_start()
    await _settle(restarted)
    _assert_state(restarted, "binary_sensor.opti_peak_horizont_lang", "on")
    await restarted.async_stop()


async def _validate_night_fallback(sun_score, forecast_score):
    config_dir = tempfile.mkdtemp(prefix="opti-night-score-ha-")
    hass = await _new_hass(config_dir)
    hass.states.async_set(
        "sun.sun",
        "below_horizon",
        {
            "next_rising": "2026-01-15T08:15:00+01:00",
            "next_setting": "2026-01-15T16:45:00+01:00",
        },
    )
    hass.states.async_set(
        "sensor.opti_forecast_today_kwh", "12", {"estimate10": 12}
    )
    hass.states.async_set("sensor.opti_house_consumption_w", "1000")
    hass.states.async_set("sensor.opti_house_consumption_60min_w", "1000")
    hass.states.async_set("input_number.opti_forecast_optimismus", "0")

    template = {
        "template": [
            {
                "sensor": [
                    copy.deepcopy(sun_score),
                    copy.deepcopy(forecast_score),
                ]
            }
        ]
    }
    assert await async_setup_component(hass, "template", template)
    await hass.async_start()
    await _settle(hass)
    _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "5")
    score = _assert_state(hass, "sensor.opti_forecast_score", "5")
    assert score.attributes["reason"] == "Nacht -> Sonnentag-Score", score

    # Ein echtes State-Event der indirekten Quelle muss zuerst den Sonnentag-
    # Score und danach dessen Nacht-Konsumenten neu berechnen.
    hass.states.async_set(
        "sensor.opti_forecast_today_kwh", "2.4", {"estimate10": 2.4}
    )
    await _settle(hass)
    _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "1")
    _assert_state(hass, "sensor.opti_forecast_score", "1")

    hass.states.async_set("sensor.opti_forecast_today_kwh", "unavailable")
    await _settle(hass)
    _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "unavailable")
    _assert_state(hass, "sensor.opti_forecast_score", "unavailable")
    await hass.async_stop()


async def _validate_midnight_rollover(sun_score, forecast_score, clock):
    """Belege den Quellenwechsel und ein verspaetetes Forecast-Rollover.

    Der Zeitwechsel selbst wird absichtlich durch ein echtes sun.sun-State-
    Event ausgewertet. Das prueft den Template-Abhaengigkeitsgraphen stabil,
    ohne HA-internes Minutentimer-Verhalten nachzubauen.
    """
    config_dir = tempfile.mkdtemp(prefix="opti-midnight-score-ha-")
    hass = await _new_hass(config_dir)
    sun_attributes = {
        "next_rising": "2026-01-15T08:15:00+01:00",
        "next_setting": "2026-01-15T16:45:00+01:00",
    }
    hass.states.async_set("sun.sun", "below_horizon", sun_attributes)
    # Um 23:59 gilt der Morgenwert. Der heutige Ganztagswert ist absichtlich
    # noch der alte, bereits verbrauchte Vortagswert.
    hass.states.async_set(
        "sensor.opti_forecast_tomorrow_kwh", "12", {"estimate10": 12}
    )
    hass.states.async_set("sensor.opti_forecast_score_tomorrow", "5")
    hass.states.async_set(
        "sensor.opti_forecast_today_kwh", "0", {"estimate10": 0}
    )
    hass.states.async_set("sensor.opti_forecast_remaining_today_kwh", "0")
    hass.states.async_set("sensor.opti_forecast_effective_remaining_kwh", "0")
    hass.states.async_set("sensor.opti_house_consumption_w", "1000")
    hass.states.async_set("sensor.opti_house_consumption_60min_w", "1000")
    hass.states.async_set("sensor.opti_battery_capacity_kwh", "10")
    hass.states.async_set("sensor.opti_soc", "50")
    hass.states.async_set("input_number.opti_forecast_optimismus", "0")

    assert await async_setup_component(
        hass,
        "template",
        {
            "template": [
                {
                    "sensor": [
                        copy.deepcopy(sun_score),
                        copy.deepcopy(forecast_score),
                    ]
                }
            ]
        },
    )
    await hass.async_start()
    await _settle(hass)
    sun_state = _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "5")
    assert sun_state.attributes["quelle"] == "sensor.opti_forecast_tomorrow_kwh"
    score = _assert_state(hass, "sensor.opti_forecast_score", "5")
    assert score.attributes["reason"] == "Abend -> Morgen-Score", score

    # Nach Mitternacht zeigt next_rising auf heute. Das sun.sun-Event ist der
    # reale Dependency-Trigger; bei verzoegertem Rollover darf der alte
    # Ganztagswert sichtbar als Score 0 bleiben.
    clock["now"] = datetime(
        2026, 1, 15, 0, 1, tzinfo=ZoneInfo("Europe/Berlin")
    )
    hass.states.async_set(
        "sun.sun", "below_horizon", {**sun_attributes, "test_tick": "00:01"}
    )
    await _settle(hass)
    sun_state = _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "0")
    assert sun_state.attributes["quelle"] == "sensor.opti_forecast_today_kwh"
    score = _assert_state(hass, "sensor.opti_forecast_score", "0")
    assert score.attributes["reason"] == "Nacht -> Sonnentag-Score", score

    # Ein frueheres Rollover der Restprognose gehoert nachts nicht zur aktiven
    # Formel und darf den sichtbaren Vortags-Ganztagswert nicht ueberdecken.
    hass.states.async_set("sensor.opti_forecast_remaining_today_kwh", "12")
    hass.states.async_set("sensor.opti_forecast_effective_remaining_kwh", "12")
    await _settle(hass)
    _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "0")
    _assert_state(hass, "sensor.opti_forecast_score", "0")

    # Erst das spaetere Ganztags-Rollover aktualisiert ueber zwei echte
    # Template-Dependencies Sonnentag-Score und Nacht-Fallback.
    hass.states.async_set(
        "sensor.opti_forecast_today_kwh", "12", {"estimate10": 12}
    )
    await _settle(hass)
    _assert_state(hass, "sensor.opti_forecast_score_sonnentag", "5")
    _assert_state(hass, "sensor.opti_forecast_score", "5")
    await hass.async_stop()


async def main():
    config = yaml.safe_load((ROOT / "packages/opti_derived.yaml").read_text())
    horizon = _entity(config, "binary_sensor", "opti_peak_horizont_lang")
    sun_score = _entity(config, "sensor", "opti_forecast_score_sonnentag")
    forecast_score = _entity(config, "sensor", "opti_forecast_score")

    clock = {"now": LOCAL_TIME}
    with patch("homeassistant.util.dt.now", side_effect=lambda: clock["now"]):
        await _validate_horizon(horizon)
        await _validate_night_fallback(sun_score, forecast_score)
        clock["now"] = datetime(
            2026, 1, 14, 23, 59, tzinfo=ZoneInfo("Europe/Berlin")
        )
        await _validate_midnight_rollover(sun_score, forecast_score, clock)

    print(
        "Native HA night integration passed: horizon hysteresis, conservative "
        "fallback, restart reevaluation, dependency updates, availability and "
        "midnight source rollover.",
        flush=True,
    )


if __name__ == "__main__":
    asyncio.run(main())

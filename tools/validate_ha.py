"""Optionale Integration-Pruefung mit echtem Home Assistant, ausschliesslich lokal.

In einer separaten venv mit homeassistant==2026.9.0 ausfuehren:
    python tools/validate_ha.py

Validiert alle aktiven Template-Abschnitte und Automations-Schemas. Startet dann
zwei isolierte HA-Instanzen in einem neuen Temp-Verzeichnis mit synthetischen
Sensorwerten, um Ladedeckel, Daten-Recovery und Restore nach Neustart zu pruefen.
Keine Verbindung zur echten Anlage; keine Modbus-Integration wird geladen.
Das Temp-Verzeichnis bleibt fuer die Fehlersuche erhalten.
"""
import asyncio
import copy
import pathlib
import tempfile
import yaml
from homeassistant.core import HomeAssistant
from homeassistant import loader, config_entries, bootstrap
from homeassistant.setup import async_setup_component
from homeassistant.components.template.config import async_validate_config_section
from homeassistant.components.automation.config import async_validate_config_item

ROOT = pathlib.Path(__file__).resolve().parent.parent

async def main():
    config_dir = tempfile.mkdtemp(prefix='opti-ha-offline-')
    hass = HomeAssistant(config_dir)
    hass.config.time_zone = 'Europe/Berlin'
    loader.async_setup(hass)
    hass.config_entries = config_entries.ConfigEntries(hass, {})
    await bootstrap.async_load_base_functionality(hass)
    count = 0
    for path in [*sorted((ROOT/'packages').glob('*.yaml')), ROOT/'opti_mapping.example.yaml']:
        data = yaml.safe_load(path.read_text())
        for block in data.get('template', []):
            assert await async_validate_config_section(hass, copy.deepcopy(block)) is not None, path
            count += 1
        for automation in data.get('automation', []):
            assert await async_validate_config_item(hass, 'automation', copy.deepcopy(automation)) is not None, path
    for path in (ROOT/'automations').glob('*.yaml'):
        for automation in yaml.safe_load(path.read_text()):
            assert await async_validate_config_item(hass, 'automation', copy.deepcopy(automation)) is not None, path
    print('Native HA template sections validated:', count, flush=True)
    data = yaml.safe_load((ROOT/'packages/opti_derived.yaml').read_text())
    latch = next(b for b in data['template'] if any(e.get('unique_id') == 'opti_ladedeckel_aktiv' for e in b.get('binary_sensor', [])))
    runtime = next(e for b in data['template'] for e in b.get('sensor', []) if e.get('unique_id') == 'opti_runtime_h')
    for eid, val in {'sensor.opti_soc':'93','input_number.maxsoc':'95','input_number.minsoc':'10','sensor.opti_battery_capacity_kwh':'10','sensor.opti_house_consumption_w':'1000','sensor.opti_pv_power_w':'0'}.items():
        hass.states.async_set(eid,val)
    assert await async_setup_component(hass, 'template', {'template':[latch, {'sensor':[runtime]}]})
    await hass.async_start()
    await hass.async_block_till_done()
    async def step(soc, expected, maximum=None):
        if maximum is not None: hass.states.async_set('input_number.maxsoc',str(maximum))
        hass.states.async_set('sensor.opti_soc',str(soc))
        await hass.async_block_till_done()
        state=hass.states.get('binary_sensor.opti_ladedeckel_aktiv')
        assert state and state.state==expected, (soc,maximum,state)
    await step(93,'off')
    await step(95,'on')
    await step(93,'on')
    await step('unavailable','on')
    await step(93,'on')
    await step(91.9,'off')
    await step(93,'off')
    await step(95,'on')
    await step(98,'off',100)
    await step(100,'on')
    await step(98,'on')
    await step(97,'on')
    await step(96.9,'off')
    hass.states.async_set('sensor.opti_soc','50')
    await hass.async_block_till_done()
    assert hass.states.get('sensor.opti_runtime_h').state=='4.0'
    hass.states.async_set('sensor.opti_pv_power_w','unavailable')
    await hass.async_block_till_done()
    assert hass.states.get('sensor.opti_runtime_h').state=='unavailable'
    await step(100,'on')
    await step(98,'on')
    await hass.async_stop()
    print('Native HA event integration: latch transitions and runtime availability passed.',flush=True)
    # Use a second actual instance with the saved restore-state store.
    restored=HomeAssistant(config_dir)
    restored.config.time_zone = 'Europe/Berlin'
    loader.async_setup(restored)
    restored.config_entries = config_entries.ConfigEntries(restored, {})
    await bootstrap.async_load_base_functionality(restored)
    restored.states.async_set('sensor.opti_soc','98')
    restored.states.async_set('input_number.maxsoc','100')
    assert await async_setup_component(restored,'template',{'template':[latch]})
    await restored.async_start()
    await restored.async_block_till_done()
    state=restored.states.get('binary_sensor.opti_ladedeckel_aktiv')
    assert state and state.state=='on' and state.attributes['maxsoc']==100, state
    await restored.async_stop()
    print('Native HA cold restart: latch and maxsoc restored.',flush=True)

if __name__ == "__main__":
    asyncio.run(main())

"""Constants and the user-facing data-source contract."""
from homeassistant.const import Platform

DOMAIN = "opti_akku"
NAME = "Opti Akku"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.SELECT, Platform.BUTTON]
MODES = ("Akku Automatisch", "Akku schnell Laden", "Akku schnell Entladen", "Akku Pause", "Akku nur Laden", "Akku Netzladen", "Akku nur Entladen", "Akku Dynamisch", "Akku 0.2C Laden")
DEFAULT_PORT = 502
DEFAULT_UNIT_ID = 3
UPDATE_SECONDS = 15
RECONCILE_SECONDS = 120
CONF_SOURCES = "sources"
CONF_SETTINGS = "settings"
# key: (canonical virtual ID, German label, expected family)
SOURCE_DEFINITIONS = {
    "house_consumption": ("sensor.opti_house_consumption_w", "Hausverbrauch", "power"),
    "pv_generation": ("sensor.opti_pv_generation_w", "PV-Erzeugung gesamt (DC)", "power"),
    "pv_power": ("sensor.opti_pv_power_w", "Hybrid-Wechselrichterleistung AC inkl. Batterie (optional überschreiben)", "power"),
    "forecast_today": ("sensor.opti_forecast_today_kwh", "PV-Prognose heute", "energy"),
    "forecast_tomorrow": ("sensor.opti_forecast_tomorrow_kwh", "PV-Prognose morgen", "energy"),
    "forecast_remaining": ("sensor.opti_forecast_remaining_today_kwh", "PV-Prognose verbleibend heute", "energy"),
    "price_current": ("sensor.opti_price_current_ct_kwh", "Aktueller Strompreis", "price"),
    "price_series": ("sensor.opti_price_series", "Preisreihe (today/tomorrow)", "price"),
    "cell_spread": ("sensor.byd_zellspreizung_ruhe", "BYD-Zellspreizung in Ruhe (optional)", "voltage_spread"),
}
EV_SOURCE_KEYS = ("ev1_mode", "ev1_charging", "ev1_power", "ev2_mode", "ev2_charging", "ev2_power")
DEFAULT_OPTIONS = {"sources": {}, "settings": {}, "price_unit": "EUR/kWh", "source_max_age": 900, "forecast_max_age": 21600, "price_max_age": 7200, "single_writer_confirmed": False, "single_inverter": False}

"""Default battery model as engine input, built from the model itself (A3/B2).

Peak precharging is fail-closed without ``sensor.opti_battery_model``; engine
fixtures that expect precharging feed this state instead of made-up attributes.
The module is imported by path, like the engine, so engine tests keep running
without Home Assistant.
"""

import importlib.util
from pathlib import Path
import sys

_SPEC = importlib.util.spec_from_file_location(
    "opti_fixture_battery_model",
    Path(__file__).parents[1] / "custom_components/opti_akku/battery_model.py",
)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

MODEL_ENTITY = _MODULE.ENGINE_ENTITY
DEFAULT_MODEL_STATE, DEFAULT_MODEL_ATTRIBUTES = _MODULE.BatteryModel.from_options(None).engine_state()


def model_states() -> dict:
    return {MODEL_ENTITY: DEFAULT_MODEL_STATE}


def model_attributes() -> dict:
    return {MODEL_ENTITY: dict(DEFAULT_MODEL_ATTRIBUTES)}

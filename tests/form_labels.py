"""Every form field and menu option the flows render has its texts.

The check wraps FlowHandler.async_show_form for Opti Akku flows, so it
covers the runtime schemas of every step the flow tests reach, including
fields added in loops or only for one backend.
"""

import json
from pathlib import Path

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowHandler

STRINGS = Path(__file__).parents[1] / "custom_components" / "opti_akku" / "strings.json"
_STRINGS = json.loads(STRINGS.read_text(encoding="utf-8"))


def missing_texts(flow: str, step_id: str, fields) -> list[str]:
    step = _STRINGS[flow]["step"].get(step_id, {})
    return [f"{flow}/{step_id}/{field}" for field in fields
            if not step.get("data", {}).get(field)
            or not step.get("data_description", {}).get(field, "").strip()]


@pytest.fixture(autouse=True)
def check_rendered_form_fields(monkeypatch):
    missing: set[str] = set()
    original = FlowHandler.async_show_form

    def show_form(self, *args, **kwargs):
        if type(self).__module__.startswith("custom_components.opti_akku"):
            schema = kwargs.get("data_schema")
            if schema is not None:
                flow = "options" if isinstance(self, config_entries.OptionsFlow) else "config"
                fields = [str(getattr(key, "schema", key)) for key in schema.schema]
                missing.update(missing_texts(flow, kwargs.get("step_id", ""), fields))
        return original(self, *args, **kwargs)

    original_menu = FlowHandler.async_show_menu

    def show_menu(self, *args, **kwargs):
        if type(self).__module__.startswith("custom_components.opti_akku"):
            flow = "options" if isinstance(self, config_entries.OptionsFlow) else "config"
            step_id = kwargs.get("step_id", "")
            labels = _STRINGS[flow]["step"].get(step_id, {}).get("menu_options", {})
            options = kwargs.get("menu_options", [])
            keys = options.keys() if isinstance(options, dict) else options
            missing.update(f"{flow}/{step_id}/menu/{key}" for key in keys if not labels.get(key))
        return original_menu(self, *args, **kwargs)

    monkeypatch.setattr(FlowHandler, "async_show_form", show_form)
    monkeypatch.setattr(FlowHandler, "async_show_menu", show_menu)
    yield
    assert not missing, sorted(missing)

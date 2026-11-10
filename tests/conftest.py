"""Shared HA fixtures. Tests never connect to real inverters."""

import sys
from pathlib import Path
from types import CodeType

import pytest

_PRODUCTION = str(Path(__file__).resolve().parents[1] / "custom_components") + "/"
_monitoring = sys.monitoring
_swallowed: dict[int, tuple[BaseException, str]] = {}


def _handled(code: CodeType, offset: int, exc: BaseException) -> None:
    # The integration catches broad exceptions (device writes, observations,
    # reports). An assertion raised by a test fake or a patched side effect
    # would silently end up there and the test would stay green.
    if isinstance(exc, AssertionError) and code.co_filename.startswith(_PRODUCTION):
        _swallowed[id(exc)] = (exc, f"{code.co_filename.removeprefix(_PRODUCTION)}:{code.co_qualname}")


def _raised(code: CodeType, offset: int, exc: BaseException) -> None:
    # ``except BaseException: cleanup(); raise`` hands the assertion back.
    _swallowed.pop(id(exc), None)


def _start_monitoring() -> int | None:
    for tool in (3, 4):
        if _monitoring.get_tool(tool) is None:
            _monitoring.use_tool_id(tool, "opti_akku_swallowed_assertions")
            events = _monitoring.events
            _monitoring.register_callback(tool, events.EXCEPTION_HANDLED, _handled)
            _monitoring.register_callback(tool, events.RAISE, _raised)
            _monitoring.register_callback(tool, events.RERAISE, _raised)
            _monitoring.set_events(tool, events.EXCEPTION_HANDLED | events.RAISE | events.RERAISE)
            return tool
    return None


_TOOL = _start_monitoring()


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def fail_on_swallowed_assertion():
    """Fail a test whose assertion was caught by integration code."""
    assert _TOOL is not None, "no free sys.monitoring tool id for the assertion guard"
    _swallowed.clear()
    yield
    found = [f"{where}: {exc!r}" for exc, where in _swallowed.values()]
    _swallowed.clear()
    if found:
        pytest.fail("AssertionError von Integrationscode verschluckt:\n" + "\n".join(found), pytrace=False)

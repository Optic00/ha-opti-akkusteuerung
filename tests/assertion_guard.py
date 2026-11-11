"""Pytest plugin: fail a test whose AssertionError integration code swallowed.

The integration catches broad exceptions (device writes, observations,
reports, the safety pause on shutdown). An assertion raised by a test fake or
a patched side effect would silently end up there and the test would stay
green. ``sys.monitoring`` reports every exception an ``except`` clause in
``custom_components`` handles; assertions that are not raised again are
reported once the complete test, including all fixture teardowns, has run,
and anything left over at the end of the session fails the run.
"""

import os
import sys
from collections.abc import Generator, Iterator
from pathlib import Path
from types import CodeType

import pytest

PRODUCTION = str(Path(__file__).resolve().parents[1] / "custom_components") + os.sep
_PREFIX = os.path.normcase(PRODUCTION)
_BETWEEN_TESTS = "zwischen Tests"
_monitoring = sys.monitoring
# Keyed by id() of the AssertionError leaf; the value keeps the object alive.
swallowed: dict[int, tuple[AssertionError, str, str]] = {}
_current_test = _BETWEEN_TESTS


def _assertions(exc: BaseException) -> Iterator[AssertionError]:
    if isinstance(exc, AssertionError):
        yield exc
    elif isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _assertions(inner)


def _handled(code: CodeType, offset: int, exc: BaseException) -> None:
    filename = os.path.normcase(code.co_filename)
    if filename.startswith(_PREFIX):
        where = f"{filename.removeprefix(_PREFIX)}:{code.co_qualname}"
        for assertion in _assertions(exc):
            swallowed[id(assertion)] = (assertion, where, _current_test)


def _raised(code: CodeType, offset: int, exc: BaseException) -> None:
    # ``except BaseException: cleanup(); raise`` or a re-raised (sub)group
    # hands the assertion back; split groups share their leaf objects.
    if swallowed:
        for assertion in _assertions(exc):
            swallowed.pop(id(assertion), None)


def _start_monitoring() -> int:
    # 0 debugger, 1 coverage, 2 profiler, 5 optimizer by convention; take any free one.
    for tool in (3, 4, 5, 2, 1, 0):
        if _monitoring.get_tool(tool) is None:
            _monitoring.use_tool_id(tool, "opti_akku_swallowed_assertions")
            events = _monitoring.events
            _monitoring.register_callback(tool, events.EXCEPTION_HANDLED, _handled)
            _monitoring.register_callback(tool, events.RAISE, _raised)
            _monitoring.register_callback(tool, events.RERAISE, _raised)
            _monitoring.set_events(tool, events.EXCEPTION_HANDLED | events.RAISE | events.RERAISE)
            return tool
    raise RuntimeError("no free sys.monitoring tool id for the assertion guard")


_start_monitoring()


def _take_report() -> str | None:
    found = [f"{where} [{test}]: {exc!r}" for exc, where, test in swallowed.values()]
    swallowed.clear()
    if not found:
        return None
    return "AssertionError von Integrationscode verschluckt:\n" + "\n".join(found)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:
    global _current_test
    _current_test = item.nodeid
    try:
        return (yield)
    finally:
        _current_test = _BETWEEN_TESTS


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, object, object]:
    # Evaluated after every fixture of the test (hass included) is torn down.
    # Nothing is cleared before a test: leftovers are reported, not dropped.
    try:
        result = yield
    except BaseException as err:
        if report := _take_report():
            err.add_note(report)
        raise
    if report := _take_report():
        pytest.fail(report, pytrace=False)
    return result


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> Generator[None, object, object]:
    # Innermost wrapper: runs after the runner tore down what an aborted run
    # left (session fixtures) and before the terminal summary is written.
    global _current_test
    _current_test = "Sitzungsende"
    result = yield
    if report := _take_report():
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("!", "verschluckte Assertions", red=True)
            reporter.write_line(report)
        else:
            sys.stderr.write(report + "\n")
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return result

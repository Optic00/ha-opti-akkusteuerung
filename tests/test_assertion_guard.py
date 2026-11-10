"""The assertion guard must see assertions swallowed by integration code."""

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import assertion_guard

ROOT = Path(__file__).resolve().parents[1]

# Code objects whose file name lies in custom_components count as integration
# code for the guard; no production module is touched.
_FAKE_SOURCE = """
def swallow(fn):
    try:
        fn()
    except BaseException:
        pass

def reraise(fn):
    try:
        fn()
    except BaseException:
        raise

def drop_assertions(fn):
    try:
        fn()
    except BaseExceptionGroup as group:
        rest = group.subgroup(lambda e: not isinstance(e, (AssertionError, BaseExceptionGroup)))
        if rest is not None:
            raise rest

def keep_assertions(fn):
    try:
        fn()
    except BaseExceptionGroup as group:
        rest = group.subgroup(lambda e: isinstance(e, AssertionError))
        if rest is not None:
            raise rest

async def swallow_async(make):
    try:
        await make()
    except Exception:
        pass
"""
fake: dict[str, object] = {}
exec(compile(_FAKE_SOURCE, assertion_guard.PRODUCTION + "assertion_guard_fake.py", "exec"), fake)


@pytest.fixture
def take():
    """Return and clear what the guard recorded, so this test stays green."""
    def taken():
        found = sorted(str(exc) for exc, _where in assertion_guard.swallowed.values())
        assertion_guard.swallowed.clear()
        return found
    yield taken
    assertion_guard.swallowed.clear()


def nested_group():
    raise ExceptionGroup("outer", [ValueError("other"),
                                   ExceptionGroup("inner", [AssertionError("leaf")])])


def test_plain_assertion_swallowed(take):
    def fail():
        raise AssertionError("plain")
    fake["swallow"](fail)
    assert take() == ["plain"]


def test_reraised_assertion_is_not_reported(take):
    def fail():
        raise AssertionError("plain")
    with pytest.raises(AssertionError):
        fake["reraise"](fail)
    assert take() == []


def test_swallowed_group_reports_nested_assertion(take):
    fake["swallow"](nested_group)
    assert take() == ["leaf"]


def test_reraised_group_is_not_reported(take):
    with pytest.raises(ExceptionGroup):
        fake["reraise"](nested_group)
    assert take() == []


def test_partially_handled_group_reports_dropped_assertion(take):
    with pytest.raises(ExceptionGroup) as raised:
        fake["drop_assertions"](nested_group)
    assert [type(e) for e in raised.value.exceptions] == [ValueError]
    assert take() == ["leaf"]


def test_partially_handled_group_keeping_assertion_is_not_reported(take):
    with pytest.raises(ExceptionGroup) as raised:
        fake["keep_assertions"](nested_group)
    assert raised.value.split(AssertionError)[1] is None  # only the assertion went on
    assert take() == []


async def test_task_group_assertion_swallowed(take):
    async def failing():
        raise AssertionError("in task")

    async def make():
        async with asyncio.TaskGroup() as group:
            group.create_task(failing())

    await fake["swallow_async"](make)
    assert take() == ["in task"]


def test_assertion_swallowed_during_fixture_teardown_fails_test(tmp_path):
    """A swallow after the test's own fixtures (e.g. HA shutdown) still counts."""
    (tmp_path / "conftest.py").write_text(textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        pytest_plugins = ["tests.assertion_guard"]
    """))
    (tmp_path / "test_inner.py").write_text(textwrap.dedent("""
        import pytest
        from tests.assertion_guard import PRODUCTION

        fake = {}
        exec(compile("def swallow(fn):\\n    try:\\n        fn()\\n    except Exception:\\n        pass\\n",
                     PRODUCTION + "assertion_guard_fake.py", "exec"), fake)

        def fail():
            assert False, "beim Shutdown verschluckt"

        @pytest.fixture(scope="module")
        def shutdown():
            yield
            fake["swallow"](fail)

        def test_clean():
            pass

        def test_with_shutdown(shutdown):
            pass
    """))
    env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rA", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=50, check=False)
    output = run.stdout + run.stderr
    assert run.returncode == 1, output
    assert "PASSED test_inner.py::test_clean" in output, output
    assert "ERROR test_inner.py::test_with_shutdown" in output, output
    assert "beim Shutdown verschluckt" in output, output
    assert "2 passed, 1 error" in output, output

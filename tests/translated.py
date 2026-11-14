"""Assert translated exceptions by key, independent of the loaded language."""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def raises_translated(cls: type[Exception], key: str) -> Iterator[pytest.ExceptionInfo]:
    with pytest.raises(cls) as info:
        yield info
    assert info.value.translation_domain == "opti_akku"
    assert info.value.translation_key == key

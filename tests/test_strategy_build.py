"""Local strategy sources must reproduce the shipped runtime independently."""
from pathlib import Path
import shutil

import pytest

from tools.build_strategy_resources import OUTPUT, SOURCE, build, render


def test_standalone_sources_reproduce_runtime(tmp_path):
    # No Git metadata, legacy checkout or installed HA configuration is present.
    copied = tmp_path / "strategy"
    shutil.copytree(SOURCE, copied)
    assert render(copied) == OUTPUT.read_text()


def test_missing_strategy_section_is_rejected(tmp_path):
    copied = tmp_path / "strategy"
    shutil.copytree(SOURCE, copied)
    (copied / "decisions.yaml").write_text("strategy: {}\n")
    with pytest.raises(ValueError, match="decisions.yaml"):
        build(copied)


def test_build_does_not_modify_sources():
    paths = list(Path(SOURCE).glob("*.yaml"))
    before = {path: path.read_bytes() for path in paths}
    build()
    assert before == {path: path.read_bytes() for path in paths}

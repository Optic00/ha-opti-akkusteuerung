"""Verify that the extracted distribution imports without the source checkout."""

import json
import tomllib
from pathlib import Path
import subprocess
import sys
import zipfile

from tools.build_package import build


def test_archive_is_self_contained(tmp_path):
    archive_path = build(tmp_path / "distribution")
    install = tmp_path / "fresh_ha"
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        assert all(name.startswith("custom_components/opti_akku/") for name in names)
        assert not any("__pycache__" in name or "/tests/" in name for name in names)
        archive.extractall(install)
    script = """
import sys
sys.path.insert(0, sys.argv[1])
from custom_components.opti_akku.engine import StrategyEngine
from datetime import datetime, UTC
engine = StrategyEngine()
result = engine.evaluate({'sensor.opti_soc': 'unavailable'}, {}, datetime.now(UTC))
assert result.mode == 'Akku Pause'
from custom_components.opti_akku import config_flow, coordinator, sma
print('Extracted integration import and fail-safe evaluation passed')
"""
    result = subprocess.run([sys.executable, "-I", "-c", script, str(install)],
                            capture_output=True, text=True, check=True, cwd=install)
    assert "passed" in result.stdout


def test_hacs_metadata_and_version():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / "custom_components/opti_akku/manifest.json").read_text())
    hacs = json.loads((root / "hacs.json").read_text())
    assert manifest["version"] == tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    assert manifest["config_flow"] is True
    assert hacs["homeassistant"] == "2026.9.1"

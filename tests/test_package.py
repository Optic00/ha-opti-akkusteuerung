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


def test_release_version_order():
    from awesomeversion import AwesomeVersion, AwesomeVersionStrategy
    from packaging.version import Version

    versions = ["0.5.2b6", "2026.9-beta1", "2026.9-beta2", "2026.9-beta3",
                "2026.9.0", "2026.9.1"]
    for before, after in zip(versions, versions[1:], strict=False):
        assert AwesomeVersion(before) < AwesomeVersion(after)
        assert Version(before) < Version(after)
    assert AwesomeVersion("2026.9-beta1").strategy in {
        AwesomeVersionStrategy.CALVER, AwesomeVersionStrategy.SEMVER,
        AwesomeVersionStrategy.SIMPLEVER, AwesomeVersionStrategy.BUILDVER,
        AwesomeVersionStrategy.PEP440,
    }
    assert AwesomeVersion("2026.9-beta1").beta


def test_release_tag_must_match_before_output(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="Release tag"):
        build(tmp_path / "bad", "2026.9-beta999")
    assert not (tmp_path / "bad").exists()


def test_release_is_reproducible(tmp_path):
    version = json.loads((Path(__file__).resolve().parents[1] / "custom_components/opti_akku/manifest.json").read_text())["version"]
    first = build(tmp_path / "a", version)
    second = build(tmp_path / "b", version)
    assert first.read_bytes() == second.read_bytes()


def test_only_tracked_files_and_no_symlinks(tmp_path, monkeypatch):
    import shutil
    import pytest
    from tools import build_package

    original = build_package.ROOT
    repository = tmp_path / "repo"
    files = subprocess.check_output(["git", "ls-files", "--", "custom_components/opti_akku", "LICENSE", "hacs.json", "pyproject.toml"], cwd=original, text=True).splitlines()
    for name in files:
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original / name, target)
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    monkeypatch.setattr(build_package, "ROOT", repository)
    component = repository / "custom_components/opti_akku"
    (component / "private_notes.py").write_text("not valid python, never packaged")
    archive = build(tmp_path / "clean")
    with zipfile.ZipFile(archive) as package:
        assert not any("private_notes" in name for name in package.namelist())
    (component / "__init__.py").unlink()
    (component / "__init__.py").symlink_to(component / "engine.py")
    with pytest.raises(ValueError, match="symlinks"):
        build(tmp_path / "unsafe")
    assert not (tmp_path / "unsafe").exists()

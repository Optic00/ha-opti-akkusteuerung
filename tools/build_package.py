"""Build a reproducible manual-install archive from tracked integration files."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"20\d{2}\.(?:[1-9]|1[0-2])(?:-beta[1-9]\d*|\.(?:0|[1-9]\d*))")


def build(output: Path, tag: str | None = None) -> Path:
    component = ROOT / "custom_components" / "opti_akku"
    manifest = json.loads((component / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    required = {"domain", "name", "version", "config_flow", "dependencies", "documentation", "codeowners"}
    if required - manifest.keys() or manifest["domain"] != "opti_akku" or "modbus" not in manifest["dependencies"]:
        raise ValueError("Invalid integration manifest")
    version = manifest["version"]
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not isinstance(version, str) or not VERSION.fullmatch(version) or project_version != version:
        raise ValueError("Manifest and project must have the same calendar version")
    if tag is not None and tag != version:
        raise ValueError("Release tag must exactly match manifest version")
    if hacs.get("homeassistant") != "2026.9.1":
        raise ValueError("Unexpected Home Assistant minimum version")
    paths = subprocess.check_output(
        ["git", "ls-files", "-z", "--", "custom_components/opti_akku"], cwd=ROOT
    ).decode().split("\0")
    contents = {}
    for name in filter(None, paths):
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("custom_components", "opti_akku"):
            raise ValueError("Unsafe component path")
        path = ROOT / relative
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != ROOT):
            raise ValueError("Component symlinks are not allowed")
        if "__pycache__" in relative.parts or not (path.suffix in (".py", ".json") or name == "custom_components/opti_akku/LICENSE" or (path.parent == component / "brand" and path.suffix == ".png")):
            raise ValueError(f"Unexpected tracked component file: {name}")
        data = path.read_bytes()
        if path.suffix == ".py":
            compile(data, name, "exec")
        if path.suffix == ".json":
            json.loads(data)
        contents[name] = data
    for name in ("__init__.py", "manifest.json", "LICENSE", "strings.json", "translations/de.json"):
        if f"custom_components/opti_akku/{name}" not in contents:
            raise ValueError(f"Missing tracked runtime file: {name}")
    if contents["custom_components/opti_akku/LICENSE"] != (ROOT / "LICENSE").read_bytes():
        raise ValueError("Component license differs from project license")
    if json.loads(contents["custom_components/opti_akku/translations/de.json"]).keys() != json.loads(contents["custom_components/opti_akku/strings.json"]).keys():
        raise ValueError("Missing German translation sections")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"opti_akku-{version}.zip"
    with tempfile.TemporaryDirectory(dir=output) as staging:
        temporary = Path(staging) / "package.zip"
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, data in sorted(contents.items()):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, data)
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() or archive.namelist() != sorted(contents):
                raise ValueError("Invalid archive structure")
            if any(archive.read(name) != data for name, data in contents.items()):
                raise ValueError("Archive content differs from source")
        temporary.replace(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".build")
    parser.add_argument("--tag", help="Require an exact match with the release tag")
    args = parser.parse_args()
    print(build(args.output, args.tag))

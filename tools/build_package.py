"""Build an installable archive containing only the custom integration."""

import argparse
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> Path:
    component = ROOT / "custom_components" / "opti_akku"
    manifest = json.loads((component / "manifest.json").read_text())
    hacs = json.loads((ROOT / "hacs.json").read_text())
    required = {"domain", "name", "version", "config_flow", "dependencies", "documentation", "codeowners"}
    if required - manifest.keys() or manifest["domain"] != "opti_akku" or "modbus" not in manifest["dependencies"]:
        raise ValueError("Invalid integration manifest")
    if hacs.get("homeassistant") != "2026.9.1":
        raise ValueError("Unexpected Home Assistant minimum version")
    for path in component.rglob("*.py"):
        compile(path.read_text(), str(path), "exec")
    for path in component.rglob("*.json"):
        json.loads(path.read_text())
    if json.loads((component / "translations/de.json").read_text()).keys() != json.loads((component / "strings.json").read_text()).keys():
        raise ValueError("Missing German translation sections")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"opti_akku-{manifest['version']}.zip"
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(component.rglob("*")):
            if path.is_file() and (path.suffix in (".py", ".json") or (path.parent == component / "brand" and path.suffix == ".png")) and "__pycache__" not in path.parts:
                archive.write(path, path.relative_to(ROOT))
        archive.write(ROOT / "LICENSE", "custom_components/opti_akku/LICENSE")
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / ".build")
    print(build(parser.parse_args().output))

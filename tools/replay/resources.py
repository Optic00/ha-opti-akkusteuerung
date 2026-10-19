"""Load the pure engine and a strategy resource from a path or a git ref."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = REPO_ROOT / "custom_components" / "opti_akku" / "engine.py"
STRATEGY_RELATIVE = "custom_components/opti_akku/resources/strategy.json"


def load_engine_module():
    """Import ``engine.py`` by path so no Home Assistant package is needed."""
    name = "opti_replay_engine"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ENGINE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_strategy(ref: str | Path | None = None) -> dict[str, Any]:
    """``None``/``HEAD`` reads the working tree; a path reads a file; else ``git show``."""
    if ref is None or str(ref) in ("", "HEAD", "worktree"):
        if str(ref) == "HEAD":
            return _from_git("HEAD")
        return json.loads((REPO_ROOT / STRATEGY_RELATIVE).read_text(encoding="utf-8"))
    candidate = Path(ref)
    if candidate.is_file():
        return json.loads(candidate.read_text(encoding="utf-8"))
    return _from_git(str(ref))


def _from_git(ref: str) -> dict[str, Any]:
    result = subprocess.run(
        ["git", "show", f"{ref}:{STRATEGY_RELATIVE}"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise FileNotFoundError(f"git show {ref}: {result.stderr.strip()}")
    return json.loads(result.stdout)

"""Load the pure engine and a strategy resource from a path or a git ref."""
from __future__ import annotations

import hashlib
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


def _git(*args: str) -> str | None:
    result = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def describe_strategy(ref: str | Path | None, resources: dict) -> dict[str, Any]:
    """Resolved origin and content hash of a strategy resource for the report."""
    digest = hashlib.sha256(
        json.dumps(resources, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if ref is None or str(ref) in ("", "worktree"):
        origin = f"worktree:{STRATEGY_RELATIVE}"
        dirty = _git("status", "--porcelain", "--", STRATEGY_RELATIVE)
        origin += " (uncommitted changes)" if dirty else f" @ {_git('rev-parse', 'HEAD')}"
    elif Path(ref).is_file():
        origin = f"file:{Path(ref).resolve()}"
    else:
        origin = f"git:{ref} -> {_git('rev-parse', '--verify', f'{ref}^{{commit}}')}"
    return {"strategy_origin": origin, "strategy_sha256": digest}


def describe_engine() -> dict[str, Any]:
    """Commit of the engine in use (always the working tree's engine.py)."""
    relative = str(ENGINE_PATH.relative_to(REPO_ROOT))
    commit = _git("log", "-1", "--format=%H", "--", relative)
    dirty = bool(_git("status", "--porcelain", "--", relative))
    return {
        "engine_path": relative,
        "engine_commit": commit,
        "engine_sha256": hashlib.sha256(ENGINE_PATH.read_bytes()).hexdigest(),
        "engine_uncommitted_changes": dirty,
    }

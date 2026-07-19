"""Load a Dexter handler from its portable skill directory for unit tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_handler(skill_name: str) -> ModuleType:
    path = REPO_ROOT / ".agents" / "skills" / "dexter" / "skills" / skill_name / "scripts" / "handler.py"
    spec = importlib.util.spec_from_file_location(f"dexter_test_{skill_name.replace('-', '_')}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load handler for {skill_name}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


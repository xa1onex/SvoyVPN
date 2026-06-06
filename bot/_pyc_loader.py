"""Load implementation from __pycache__ when .py source was lost."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def exec_from_pyc(module_name: str, pyc_path: Path) -> ModuleType:
    pyc_path = pyc_path.resolve()
    if not pyc_path.is_file():
        raise FileNotFoundError(pyc_path)

    existing = sys.modules.get(module_name)
    if existing is not None and getattr(existing, "_loaded_from_pyc", False):
        return existing

    loader = importlib.machinery.SourcelessFileLoader(module_name, str(pyc_path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    if spec is None:
        raise ImportError(f"Cannot load {pyc_path}")

    module = importlib.util.module_from_spec(spec)
    module._loaded_from_pyc = True  # type: ignore[attr-defined]
    sys.modules[module_name] = module
    loader.exec_module(module)
    return module

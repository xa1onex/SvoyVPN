"""Restore lost source from bytecode cache (newer than git)."""
import sys

if not getattr(sys.modules.get(__name__), "_loaded_from_pyc", False):
    from pathlib import Path

    from bot._pyc_loader import exec_from_pyc

    _mod = exec_from_pyc(
        __name__,
        Path(__file__).resolve().parent / "__pycache__" / "happ_text_notice.cpython-310.pyc",
    )
    globals().update({k: v for k, v in _mod.__dict__.items() if k != "__all__"})

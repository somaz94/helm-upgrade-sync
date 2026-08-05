"""Shared module loader for tests under ``tests/python/``.

Adds the package root to ``sys.path`` (idempotent) and imports the named
module via ``importlib.import_module``. Resolution matches the consumer
thin-wrapper's pattern, so test-side imports resolve ``upgrade_core.<name>``
the same way runtime invocations do under either layout.

Introduced to remove the four-way
duplication of the ``_load_module`` helper across test files.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType


def _find_package_root() -> Path:
    """Locate the directory holding the ``upgrade_sync`` / ``upgrade_core`` packages.

    Supports both layouts: embedded (``<repo>/scripts/python/``) and standalone
    (packages sitting at the tool root, next to ``tests/``).
    """
    here = Path(__file__).resolve()
    for anc in here.parents:
        embedded = anc / "scripts" / "python"
        if (embedded / "upgrade_sync").is_dir():
            return embedded
        if (anc / "upgrade_sync").is_dir():
            return anc
    # Preserve the historical guess so the failure surfaces as an ImportError
    # naming the expected path rather than an opaque IndexError here.
    return here.parent.parent.parent / "scripts" / "python"


def _find_tool_root(repo_root: Path) -> Path:
    """Locate the directory holding the entry-point scripts.

    Embedded copies keep them at ``<repo>/scripts/upgrade-sync/``; a standalone
    checkout keeps them at the repo root.
    """
    embedded = repo_root / "scripts" / "upgrade-sync"
    return embedded if (embedded / "sync.py").is_file() else repo_root


_PKG_ROOT = _find_package_root()
REPO_ROOT = _PKG_ROOT.parent.parent if _PKG_ROOT.name == "python" else _PKG_ROOT
# Where sync.py / check-versions.py / manage-backups.py and templates/ live.
TOOL_ROOT = _find_tool_root(REPO_ROOT)


def load(module_name: str) -> ModuleType:
    """Return the imported module after ensuring the package root is on sys.path."""
    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))
    return importlib.import_module(module_name)

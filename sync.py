#!/usr/bin/env python3
"""Thin entry-point wrapper for the upgrade-sync system.

Sits alongside ``check-versions.py`` and the canonical ``templates/``.
The actual logic lives in ``upgrade_sync/`` — this file only resolves the
package root and dispatches to ``cli.main``.

Resolution handles both layouts: a standalone checkout keeps the packages
next to this script, while an embedded copy at ``<repo>/scripts/upgrade-sync/``
finds them under ``<repo>/scripts/python/``. Either way the file works when
invoked from an arbitrary cwd or through a symlink.
"""

from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_package_root() -> None:
    here = Path(__file__).resolve().parent
    # Standalone layout — the packages sit next to this script.
    if (here / "upgrade_sync").is_dir():
        sys.path.insert(0, str(here))
        return
    # Embedded layout — <repo>/scripts/upgrade-sync/ next to <repo>/scripts/python/.
    for anc in [here, *here.parents]:
        if (anc / "scripts" / "python" / "upgrade_sync").is_dir():
            sys.path.insert(0, str(anc / "scripts" / "python"))
            return
    # Last-resort fallback (development env where layout drifted).
    here_pkg = here.parent.parent / "scripts" / "python"
    if here_pkg.is_dir():
        sys.path.insert(0, str(here_pkg))


_bootstrap_package_root()

from upgrade_sync.cli import main  # noqa: E402


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], script_path=__file__))

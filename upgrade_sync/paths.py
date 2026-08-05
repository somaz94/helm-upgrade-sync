"""Target-repository root resolution.

Historically both entry points (``sync.py`` / ``manage-backups.py``) hardcoded
``repo_root = script_dir.parent.parent``, which encodes the assumption that the
tool lives at ``<repo>/scripts/upgrade-sync/``. That holds for an embedded copy
but breaks the moment the tool is installed standalone and pointed at some other
repository.

This module keeps the embedded assumption as one branch of an explicit
precedence chain, so an embedded copy resolves byte-identically to before while
a standalone copy can target any repository.

Precedence:

1. ``--repo-root <dir>`` (explicit operator intent)
2. ``UPGRADE_SYNC_REPO_ROOT`` environment variable
3. Embedded layout — ``script_dir`` is ``<repo>/scripts/upgrade-sync/``
4. ``git rev-parse --show-toplevel`` of the current working directory
5. The current working directory

Steps 3 and 4 are deliberately in that order: an embedded copy invoked from an
unrelated cwd must keep targeting its own repo, which is the pre-existing
behaviour every caller relies on.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ENV_VAR = "UPGRADE_SYNC_REPO_ROOT"

# Directory names that mark the embedded layout: <repo>/scripts/upgrade-sync/.
_EMBEDDED_LEAF = "upgrade-sync"
_EMBEDDED_PARENT = "scripts"


def is_embedded(script_dir: Path) -> bool:
    """Return True when ``script_dir`` is a ``<repo>/scripts/upgrade-sync/`` copy."""
    return script_dir.name == _EMBEDDED_LEAF and script_dir.parent.name == _EMBEDDED_PARENT


def _git_toplevel(start: Path) -> Path | None:
    """Return the git worktree root containing ``start``, or None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    return Path(top) if top else None


def resolve_repo_root(script_dir: Path, explicit: str | None = None) -> Path:
    """Resolve the repository the sync run should operate on.

    ``script_dir`` is the directory holding the entry-point script. See the
    module docstring for the precedence chain.
    """
    if explicit:
        return Path(explicit).expanduser().resolve()

    env_value = os.environ.get(ENV_VAR)
    if env_value:
        return Path(env_value).expanduser().resolve()

    if is_embedded(script_dir):
        return script_dir.parent.parent

    cwd = Path.cwd()
    top = _git_toplevel(cwd)
    return top if top is not None else cwd


def extract_repo_root_flag(args: list[str]) -> tuple[list[str], str | None]:
    """Strip ``--repo-root <dir>`` / ``--repo-root=<dir>`` from ``args``.

    Returns the remaining args and the requested root (None when absent). The
    flag is removed before the legacy positional command parser runs, so the
    byte-for-byte usage contract of the original dispatcher is preserved.
    """
    remaining: list[str] = []
    root: str | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--repo-root":
            if i + 1 < len(args):
                root = args[i + 1]
                i += 2
                continue
            # Missing value — drop the dangling flag and let the caller decide.
            i += 1
            continue
        if arg.startswith("--repo-root="):
            root = arg.split("=", 1)[1]
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return remaining, root

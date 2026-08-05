"""Top-level commands invoked by the CLI dispatcher.

Each ``cmd_*`` function returns an exit code (0 on success, non-zero on
failure / drift). Output formatting mirrors the retired bash ``sync.sh``
verbatim so the package's golden-file tests can lock down byte parity.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .discovery import find_managed_files, find_unmanaged_charts, parse_template_header
from .extract import build_expected


# Width of the ``[<template>]`` field in the per-file status row. The bash
# original printed ``%-16s`` so every row's left edge lines up regardless
# of template-name length. Keep at 16 to preserve byte parity.
_HEADER_FIELD_WIDTH = 16


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_template(upgrade_script: Path) -> str:
    """Return the file's ``# upgrade-template:`` header, exiting if absent.

    Mirrors the bash ``resolve_template`` in the "header" mode (the only
    mode the Python port supports — ``--no-header`` was retired with
    ``--insert-headers`` once every consumer carried the header).
    """
    template = parse_template_header(upgrade_script)
    if template:
        return template
    print(
        f"ERROR: {upgrade_script} has no '# upgrade-template:' header on line 2",
        file=sys.stderr,
    )
    print(
        "       Add the header on line 2: '# upgrade-template: <name>'",
        file=sys.stderr,
    )
    sys.exit(2)


def _rel(repo_root: Path, path: Path) -> str:
    """Repo-relative path string (used for human-readable output)."""
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_check(repo_root: Path, templates_dir: Path) -> int:
    """Diff every managed upgrade.py against its canonical. CI-friendly."""
    drift = 0
    total = 0
    skipped = 0
    for path in find_managed_files(repo_root):
        total += 1

        # Files without a header are silently skipped (not an error) so a
        # repo can partially adopt the sync system without forcing every
        # upgrade.py to be managed at once.
        header = parse_template_header(path)
        if not header:
            skipped += 1
            # NOTE: the SKIP literal is 17 chars wide ("no-header" + 8 spaces)
            # — one wider than the OK/DRIFT 16-char field. This asymmetry is
            # carried over from bash sync.sh's printf format string; keep it
            # for byte parity with the captured golden fixtures.
            print(f"  SKIP  [no-header        ] {_rel(repo_root, path)}")
            continue

        expected = build_expected(path, header, templates_dir)
        actual = path.read_text(encoding="utf-8")
        rel = _rel(repo_root, path)
        if expected == actual:
            print(f"  OK    [{header:<{_HEADER_FIELD_WIDTH}}] {rel}")
        else:
            print(f"  DRIFT [{header:<{_HEADER_FIELD_WIDTH}}] {rel}")
            drift += 1

    print()
    managed = total - skipped
    if drift == 0:
        if skipped > 0:
            print(f"{managed} managed file(s) in sync. {skipped} skipped (no header).")
        else:
            print(f"All {total} managed file(s) are in sync.")
        return 0

    print(
        f"{drift} of {managed} managed file(s) have drift. "
        f"({skipped} skipped, no header)"
    )
    print("To inspect a specific file:")
    print("  sync.py --print-expected <file> | diff - <file>")
    print("To fix: sync.py --apply")
    return 1


def cmd_apply(repo_root: Path, templates_dir: Path, force: bool = False) -> int:
    """Rewrite every managed upgrade.py from its canonical."""
    # Guard: working tree must be clean unless --force.
    if not force and _git_tree_dirty(repo_root):
        print(
            "ERROR: working tree is dirty. Commit or stash before --apply.",
            file=sys.stderr,
        )
        print(
            "       Use 'sync.py --apply --force' to override.",
            file=sys.stderr,
        )
        print(
            f"       (Run 'git -C {repo_root} status' to see changes.)",
            file=sys.stderr,
        )
        return 3

    changed = 0
    total = 0
    skipped = 0
    for path in find_managed_files(repo_root):
        total += 1
        header = parse_template_header(path)
        if not header:
            skipped += 1
            continue

        expected = build_expected(path, header, templates_dir)
        actual = path.read_text(encoding="utf-8")
        if expected != actual:
            path.write_text(expected, encoding="utf-8")
            # Match bash chmod +x — preserve existing perm bits, add exec.
            mode = path.stat().st_mode
            path.chmod(mode | 0o111)
            changed += 1
            print(f"  WROTE [{header:<{_HEADER_FIELD_WIDTH}}] {_rel(repo_root, path)}")

    print()
    managed = total - skipped
    print(
        f"Updated {changed} of {managed} managed file(s). "
        f"({skipped} skipped, no header)"
    )
    if changed > 0:
        print(f"Review the changes with: git -C {repo_root} diff")
    return 0


def cmd_status(repo_root: Path, templates_dir: Path) -> int:
    """Print template assignment + canonicals + unmanaged chart list."""
    total = 0
    counts: dict[str, int] = {}
    for path in find_managed_files(repo_root):
        total += 1
        header = parse_template_header(path) or "(no header)"
        counts[header] = counts.get(header, 0) + 1

    print(f"Managed upgrade.{{sh,py}} files: {total}")
    for name in sorted(counts):
        # Bash prints "%-24s %d" with name + ":" → 24-char left-justified
        # field including the trailing colon. Match that exactly.
        label = f"{name}:"
        print(f"  {label:<24} {counts[name]}")

    print()
    print("Available canonicals:")
    canonicals: set[str] = set()
    for ext in ("*.sh", "*.py"):
        for c in templates_dir.glob(ext):
            if c.is_file():
                canonicals.add(c.stem)
    for name in sorted(canonicals):
        print(f"  {name}")

    print()
    print("Unmanaged chart directories (have Chart.yaml but no upgrade.{sh,py}):")
    unmanaged = find_unmanaged_charts(repo_root)
    if not unmanaged:
        print("  (none)")
    else:
        for rel in unmanaged:
            print(f"  - {rel}")
    return 0


def cmd_print_expected(repo_root: Path, templates_dir: Path, file_arg: str | None) -> int:
    """Print to stdout what ``file_arg`` would look like after sync."""
    if not file_arg:
        print(
            "ERROR: --print-expected requires a valid file path",
            file=sys.stderr,
        )
        return 1
    candidate = Path(file_arg)
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    else:
        candidate = candidate.resolve()
    if not candidate.is_file():
        print(
            "ERROR: --print-expected requires a valid file path",
            file=sys.stderr,
        )
        return 1
    template = _resolve_template(candidate)
    expected = build_expected(candidate, template, templates_dir)
    sys.stdout.write(expected)
    return 0


# ---------------------------------------------------------------------------
# Git-tree helper
# ---------------------------------------------------------------------------


def _git_tree_dirty(repo_root: Path) -> bool:
    """Return True when ``git diff --quiet HEAD`` reports unstaged changes."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--quiet", "HEAD", "--"],
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        # git missing or repo unreadable → treat as clean (bash original
        # also let the apply proceed in that path via ``|| true``).
        return False
    return result.returncode != 0


__all__ = [
    "cmd_check",
    "cmd_apply",
    "cmd_status",
    "cmd_print_expected",
]

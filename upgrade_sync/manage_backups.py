"""Cross-chart backup janitor for upgrade.py-managed snapshots.

Each managed chart dir produces backups under ``<chart>/backup/<TIMESTAMP>/``
(YYYYMMDD_HHMMSS naming, so directory name sorts lexically == time order).
This module collects them across every consumer and exposes four commands:

- ``--list``                    Per-chart table with count + size + oldest + newest.
- ``--total-size``              One-line grand-total.
- ``--cleanup [--keep N]``      Keep last N per chart (default 5), remove rest.
- ``--purge``                   Interactive: type ``PURGE`` to remove everything.

Discovery shares ``upgrade_sync.discovery.find_managed_files`` with sync.py /
check-versions.py / auto-upgrade.py — the bash original walked only
``upgrade.sh`` (legacy from Phase 1) which silently found zero charts after the migration
once every consumer became ``upgrade.py``. Importing the shared discovery
restores the correct walk over ``upgrade.{sh,py}``.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .discovery import find_managed_files
from .paths import extract_repo_root_flag, resolve_repo_root


# Width of the per-chart relative-path column in --list / --cleanup tables.
# Matches the bash ``%-50s`` printf width exactly so multi-line outputs
# align cleanly in fixed-width terminals.
_CHART_FIELD_WIDTH = 50


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def human_size(num_bytes: int) -> str:
    """Render a byte count as ``B`` / ``K`` / ``M`` / ``G``.

    Mirrors the bash ``human_size`` thresholds exactly: 1024 / 1048576 /
    1073741824. M and G use one decimal (``%.1f``); B and K are integer.
    """
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1048576:
        return f"{num_bytes // 1024}K"
    if num_bytes < 1073741824:
        return f"{num_bytes / 1048576:.1f}M"
    return f"{num_bytes / 1073741824:.1f}G"


def _dir_size(path: Path) -> int:
    """Sum ``st_size`` of every regular file under ``path`` (recursive)."""
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file():
                total += entry.stat().st_size
        except OSError:
            # File vanished mid-walk (concurrent rm) — skip.
            continue
    return total


def _backup_snapshots(backup_dir: Path) -> list[Path]:
    """Return YYYYMMDD-prefixed snapshot dirs sorted lexically (== time-asc).

    Bash original used the shell glob ``"$backup_dir"/2*/`` to skip stray
    files; we filter on the directory entry + the ``2`` prefix the same way.
    """
    if not backup_dir.is_dir():
        return []
    return sorted(
        d for d in backup_dir.iterdir()
        if d.is_dir() and d.name.startswith("2")
    )


def chart_backup_stats(chart_dir: Path) -> tuple[int, int, str, str]:
    """Return ``(count, total_bytes, oldest, newest)`` for a chart's backups.

    Empty fields (``0, 0, "", ""``) when the chart has no backup/ directory
    or no snapshots.
    """
    snapshots = _backup_snapshots(chart_dir / "backup")
    if not snapshots:
        return 0, 0, "", ""
    total = sum(_dir_size(s) for s in snapshots)
    return len(snapshots), total, snapshots[0].name, snapshots[-1].name


def chart_prune(chart_dir: Path, keep: int) -> tuple[int, int]:
    """Remove all but the most-recent ``keep`` snapshots. Return ``(removed, freed_bytes)``.

    Bash original used ``ls -dt`` (mtime-desc) + ``tail -n <to_delete>`` to
    pick the oldest. We mirror that with ``stat().st_mtime`` + slice.
    """
    snapshots = _backup_snapshots(chart_dir / "backup")
    if len(snapshots) <= keep:
        return 0, 0
    # Sort by mtime descending; the older tail is what we delete.
    by_mtime_desc = sorted(snapshots, key=lambda p: p.stat().st_mtime, reverse=True)
    to_delete = by_mtime_desc[keep:]
    freed = 0
    for d in to_delete:
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
    return len(to_delete), freed


def chart_purge(chart_dir: Path) -> tuple[int, int]:
    """Remove every backup snapshot + the empty ``backup/`` parent. Return ``(removed, freed_bytes)``."""
    backup_dir = chart_dir / "backup"
    snapshots = _backup_snapshots(backup_dir)
    if not snapshots:
        return 0, 0
    freed = 0
    for d in snapshots:
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
    # Try to remove the now-empty backup/ dir; ignore if other files remain.
    try:
        backup_dir.rmdir()
    except OSError:
        pass
    return len(snapshots), freed


def _rel(repo_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_list(repo_root: Path) -> int:
    """Print a per-chart backup table + grand total."""
    header_fmt = f"  %-{_CHART_FIELD_WIDTH}s %-7s %-8s %-17s %s"
    print(header_fmt % ("CHART", "COUNT", "SIZE", "OLDEST", "NEWEST"))
    print(header_fmt % ("-----", "-----", "----", "------", "------"))

    grand_count = 0
    grand_bytes = 0
    for f in find_managed_files(repo_root):
        chart_dir = f.parent
        count, total_bytes, oldest, newest = chart_backup_stats(chart_dir)
        if count == 0:
            continue
        grand_count += count
        grand_bytes += total_bytes
        rel = _rel(repo_root, chart_dir)
        print(header_fmt % (rel, count, human_size(total_bytes), oldest, newest))

    print()
    print(f"  Total: {grand_count} backup(s) across all charts, {human_size(grand_bytes)}")
    return 0


def cmd_total_size(repo_root: Path) -> int:
    """Print a one-line summary of the grand backup footprint."""
    grand_count = 0
    grand_bytes = 0
    chart_count = 0
    for f in find_managed_files(repo_root):
        count, total_bytes, _, _ = chart_backup_stats(f.parent)
        if count == 0:
            continue
        chart_count += 1
        grand_count += count
        grand_bytes += total_bytes
    print(f"Total: {grand_count} backup(s) in {chart_count} chart(s), {human_size(grand_bytes)}")
    return 0


def cmd_cleanup(repo_root: Path, keep: int) -> int:
    """Prune old backups, keep last ``keep`` per chart."""
    if keep < 0:
        print(f"ERROR: --keep requires a non-negative integer (got: {keep})", file=sys.stderr)
        return 2

    print(f"Pruning backups across all charts (keep last {keep} per chart)...")
    print()
    row_fmt = f"  %-{_CHART_FIELD_WIDTH}s removed=%d, freed=%s"
    total_removed = 0
    total_freed = 0
    for f in find_managed_files(repo_root):
        chart_dir = f.parent
        removed, freed = chart_prune(chart_dir, keep)
        if removed == 0:
            continue
        total_removed += removed
        total_freed += freed
        print(row_fmt % (_rel(repo_root, chart_dir), removed, human_size(freed)))

    print()
    if total_removed == 0:
        print("Nothing to clean up (all charts within retention).")
    else:
        print(f"Removed {total_removed} backup(s) total, freed {human_size(total_freed)}.")
    return 0


def cmd_purge(repo_root: Path, *, input_fn=input) -> int:
    """Interactive — type ``PURGE`` to remove every backup."""
    print("WARNING: This will REMOVE ALL backups under every managed chart's backup/ directory.")
    print("         Existing rollback snapshots will be lost.")
    print()
    confirm = input_fn("Type 'PURGE' to confirm: ")
    if confirm != "PURGE":
        print("Aborted.")
        return 1

    print()
    row_fmt = f"  %-{_CHART_FIELD_WIDTH}s removed=%d, freed=%s"
    total_removed = 0
    total_freed = 0
    for f in find_managed_files(repo_root):
        chart_dir = f.parent
        removed, freed = chart_purge(chart_dir)
        if removed == 0:
            continue
        total_removed += removed
        total_freed += freed
        print(row_fmt % (_rel(repo_root, chart_dir), removed, human_size(freed)))

    print()
    if total_removed == 0:
        print("No backups found.")
    else:
        print(f"Purged {total_removed} backup(s) total, freed {human_size(total_freed)}.")
    return 0


# ---------------------------------------------------------------------------
# CLI dispatcher
# ---------------------------------------------------------------------------


_USAGE = """Usage: manage-backups.py <command> [options]

Manage backup directories created by upgrade.py scripts across all charts.

Commands:
  --list                    List backups per chart (count, size, oldest, newest).
  --total-size              Show total disk usage of all backup/ directories.
  --cleanup [--keep N]      Keep last N backups per chart (default 5).
  --purge                   Remove ALL backups (requires confirmation).
  -h, --help                Show this help.

Options:
  --repo-root <dir>         Repository to operate on. Defaults to the embedded
                            repo when installed at <repo>/scripts/upgrade-sync/,
                            otherwise $UPGRADE_SYNC_REPO_ROOT, otherwise the git
                            root of the current directory.

Examples:
  manage-backups.py --list
  manage-backups.py --cleanup --keep 1        # keep only the latest backup
  manage-backups.py --cleanup --keep 3
  manage-backups.py --purge                    # remove everything (destructive)
  manage-backups.py --total-size
"""


def _print_usage(stream=None) -> None:
    (stream or sys.stdout).write(_USAGE)


def main(argv: list[str] | None = None, script_path: str | None = None) -> int:
    """Parse argv and dispatch a single command."""
    args = list(argv if argv is not None else sys.argv[1:])
    args, repo_root_flag = extract_repo_root_flag(args)

    if script_path is None:
        script_path = str(Path(__file__).resolve())
    script_dir = Path(script_path).resolve().parent
    # An embedded copy still resolves to two directories up; a standalone
    # install falls through to --repo-root / env / git root.
    repo_root = resolve_repo_root(script_dir, repo_root_flag)

    if not args:
        _print_usage()
        return 0

    cmd = args[0]
    if cmd in ("-h", "--help"):
        _print_usage()
        return 0
    if cmd == "--list":
        return cmd_list(repo_root)
    if cmd == "--total-size":
        return cmd_total_size(repo_root)
    if cmd == "--cleanup":
        keep = 5
        i = 1
        while i < len(args):
            if args[i] == "--keep":
                if i + 1 >= len(args):
                    print("ERROR: --keep requires a number", file=sys.stderr)
                    return 2
                try:
                    keep = int(args[i + 1])
                except ValueError:
                    print(
                        f"ERROR: --keep requires a non-negative integer (got: {args[i + 1]})",
                        file=sys.stderr,
                    )
                    return 2
                i += 2
            else:
                print(f"Unknown option: {args[i]}", file=sys.stderr)
                print(file=sys.stderr)
                _print_usage(sys.stderr)
                return 2
        return cmd_cleanup(repo_root, keep)
    if cmd == "--purge":
        return cmd_purge(repo_root)

    # Exit 2 (usage error), so a mistyped subcommand in CI fails instead of passing silently.
    print(f"Unknown command: {cmd}", file=sys.stderr)
    print(file=sys.stderr)
    _print_usage(sys.stderr)
    return 2


__all__ = [
    "cmd_list",
    "cmd_total_size",
    "cmd_cleanup",
    "cmd_purge",
    "chart_backup_stats",
    "chart_prune",
    "chart_purge",
    "human_size",
    "main",
]

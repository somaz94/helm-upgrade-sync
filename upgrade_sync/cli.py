"""CLI dispatcher for the sync system.

Wires argv → one of ``cmd_check`` / ``cmd_apply`` / ``cmd_status`` /
``cmd_print_expected``. The bash original used a single-pass ``case`` with
implicit usage on zero args; this module preserves that contract via a
small manual parser (so we keep matching the bash usage text byte-for-byte
without argparse's auto-generated phrasing).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .commands import cmd_apply, cmd_check, cmd_print_expected, cmd_status
from .paths import extract_repo_root_flag, resolve_repo_root


_USAGE = """Usage: sync.py [--repo-root <dir>] <command>

Commands:
  --check              Diff each managed upgrade.py against its canonical.
                       Exits non-zero if any drift is found. (CI-friendly)
  --apply [--force]    Rewrite each managed upgrade.py from its canonical.
                       Aborts if the working tree is dirty (use --force to skip).
  --status             Print template assignment + drift summary table.
  --print-expected <file>
                       Print what <file> would look like after sync (stdout).
  -h, --help           Show this help.

Options:
  --repo-root <dir>    Repository to operate on. Defaults to the embedded
                       repo when installed at <repo>/scripts/upgrade-sync/,
                       otherwise $UPGRADE_SYNC_REPO_ROOT, otherwise the git
                       root of the current directory.

Examples:
  sync.py --check
  sync.py --status
  sync.py --apply
  sync.py --repo-root ~/infra --check
"""


def _print_usage() -> None:
    """Print the usage banner verbatim (no auto-formatting)."""
    sys.stdout.write(_USAGE)


def main(argv: list[str] | None = None, script_path: str | None = None) -> int:
    """Dispatch a single sync command and return its exit code."""
    args = list(argv if argv is not None else sys.argv[1:])
    args, repo_root_flag = extract_repo_root_flag(args)

    # Resolve the templates dir relative to ``sync.py``'s own location so
    # that the package can be invoked from any cwd (mirrors the bash
    # ``SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)`` pattern).
    if script_path is None:
        script_path = str(Path(__file__).resolve())
    script_dir = Path(script_path).resolve().parent
    templates_dir = script_dir / "templates"
    # The repo under management is NOT necessarily the tool's own ancestor —
    # see ``paths.resolve_repo_root`` for the precedence chain.
    repo_root = resolve_repo_root(script_dir, repo_root_flag)

    if not args:
        _print_usage()
        return 0

    cmd = args[0]
    if cmd in ("-h", "--help"):
        _print_usage()
        return 0
    if cmd == "--check":
        return cmd_check(repo_root, templates_dir)
    if cmd == "--apply":
        force = len(args) >= 2 and args[1] == "--force"
        return cmd_apply(repo_root, templates_dir, force=force)
    if cmd == "--status":
        return cmd_status(repo_root, templates_dir)
    if cmd == "--print-expected":
        file_arg = args[1] if len(args) >= 2 else None
        return cmd_print_expected(repo_root, templates_dir, file_arg)

    # Match the bash sync.sh contract — unknown commands echo the message
    # and fall through to the usage banner, but the script still exits 0.
    # CI yamls relied on this for argv typos; preserve byte parity.
    print(f"Unknown command: {cmd}")
    print()
    _print_usage()
    return 0

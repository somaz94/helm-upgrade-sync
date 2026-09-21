"""Shared helpers for upgrade_core template modules.

Extracted once a third template (``external-with-image-tag``) confirmed that
it, ``external-standard`` and ``ansible-github-release`` share the same
backup-directory layout and exclude-pattern semantics. Public API names
intentionally drop the module-private ``_`` prefix the originating
modules used.

Exported helpers — all stdlib-only:

- :func:`sorted_backups` — newest-first list of ``backup/<YYYYMMDD_HHMMSS>``
  subdirs.
- :func:`cleanup_backups` — verbose prune to ``keep_backups`` retention.
- :func:`auto_prune_backups` — silent prune called at the end of a
  successful upgrade.
- :func:`is_excluded` — substring match against a comma-separated pattern
  string (used by the ``--exclude`` flag in the chart-flavored templates;
  not used by ``ansible_github_release``).
- :func:`parse_upgrade_argv` — the shared ``sys.exit``-style CLI loop
  behind ``external_standard`` / ``ansible_github_release`` /
  ``local_with_templates``'s ``_parse_args``.
- :func:`print_backup_list` / :func:`backup_file_names` — the shared
  ``--list-backups`` frame; each template supplies only its row text.
- :func:`print_run_banner` / :func:`print_upgrade_footer` — the
  DOUBLE_SEP-framed opening and closing blocks of those same three
  templates' ``_main_flow``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable


# Output separators shared across every upgrade template's `_main_flow`
# banner and step boundaries. Kept at this fixed 48-char width to match
# the bash bodies byte-for-byte.
SEPARATOR = "------------------------------------------------"
DOUBLE_SEP = "================================================"

# GitHub Releases API page size used by both ``ansible_github_release``
# and ``external_oci`` when scanning recent releases.
GITHUB_RELEASES_PER_PAGE = 100
GITHUB_RECENT_RELEASES_PER_PAGE = 30

# Backup-directory timestamp + retention defaults shared across every
# template module. The format string and retention value were inlined in
# each template; consolidating them here keeps the bash byte-for-byte
# behavior while exposing a single point of change.
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
DEFAULT_KEEP_BACKUPS = 5


def now_timestamp() -> str:
    """Return ``datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)``.

    Centralizes the ``YYYYMMDD_HHMMSS`` literal used in every template's
    backup-dir naming + chart-pin backup naming (the CR templates).
    """
    return datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)


def read_keep_backups_env() -> int:
    """Return ``int($KEEP_BACKUPS)`` falling back to ``DEFAULT_KEEP_BACKUPS``.

    Mirrors the per-template inline expression
    ``int(os.environ.get("KEEP_BACKUPS") or "5")`` byte-for-byte.
    An empty-string env value is treated as unset (matches the bash
    ``${KEEP_BACKUPS:-5}`` semantic the previous Python copies were
    mirroring).
    """
    raw = os.environ.get("KEEP_BACKUPS") or str(DEFAULT_KEEP_BACKUPS)
    return int(raw)


def sorted_backups(backup_dir: Path) -> list[Path]:
    """Return backup subdirs (matching ``2*``) sorted by name desc.

    Backup dirs use the ``YYYYMMDD_HHMMSS`` timestamp, so name-desc ==
    time-desc.
    """
    if not backup_dir.is_dir():
        return []
    return sorted(
        (p for p in backup_dir.glob("2*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )


def cleanup_backups(backup_dir: Path, keep_backups: int) -> None:
    """Verbose prune called by ``--cleanup-backups``.

    Mirrors the bash ``cleanup_backups`` block byte-for-byte: prints the
    header, totals, removed entries, and the final ``Done.`` line.
    """
    backups = sorted_backups(backup_dir)
    if not backups:
        print("No backups found.")
        return
    total = len(backups)
    print(f"Total backups: {total} (keeping last {keep_backups})")
    if total <= keep_backups:
        print("Nothing to clean up.")
        return
    to_delete = total - keep_backups
    print(f"Removing {to_delete} old backup(s)...")
    # Keep the most recent `keep_backups`, drop the older tail.
    for victim in backups[keep_backups:]:
        shutil.rmtree(victim)
        print(f"  Removed: {victim.name}")
    print("Done.")


def auto_prune_backups(backup_dir: Path, keep_backups: int) -> None:
    """Silent variant called at the end of a successful upgrade.

    Mirrors the bash ``auto_prune_backups`` block: no header, no
    per-victim line; emits the ``Auto-pruned N old backup(s)...`` line
    only when something was actually removed.
    """
    if not backup_dir.is_dir():
        return
    backups = sorted_backups(backup_dir)
    total = len(backups)
    if total <= keep_backups:
        return
    to_delete = total - keep_backups
    for victim in backups[keep_backups:]:
        shutil.rmtree(victim)
    print(f"  Auto-pruned {to_delete} old backup(s) (KEEP_BACKUPS={keep_backups}).")


def is_excluded(filename: str, patterns: str) -> bool:
    """Return True when filename contains any comma-separated pattern.

    Empty ``patterns`` always returns False. Empty individual patterns
    (e.g. trailing commas) are ignored — bash's ``IFS=',' read -ra``
    produced empty strings that the loop would still substring-match,
    but the ``-z`` guard at the top of the bash function shortcuts the
    empty-input case identically.
    """
    if not patterns:
        return False
    for pat in patterns.split(","):
        if pat and pat in filename:
            return True
    return False


# -----------------------------------------------
# CLI argument loop (sys.exit-style templates)
# -----------------------------------------------
#
# ``external_standard``, ``ansible_github_release`` and
# ``local_with_templates`` each carried a byte-identical copy of this
# loop; only the sub-command callbacks and the presence of ``--exclude``
# differ. The CR templates use a different contract — they
# return a ``(mode, ..., exit_code)`` tuple instead of calling
# ``sys.exit`` — and deliberately keep their own ``_parse_argv``.

def parse_upgrade_argv(
    argv: list[str],
    *,
    usage: Callable[[], None],
    list_backups: Callable[[], None],
    rollback: Callable[[], None],
    cleanup_backups: Callable[[], None],
    support_exclude: bool = True,
) -> dict:
    """Parse the shared upgrade CLI flags, exiting on every sub-command.

    Returns ``{"dry_run", "target_version", "exclude_patterns"}`` for the
    main flow. ``--help``, ``--list-backups``, ``--rollback`` and
    ``--cleanup-backups`` invoke their callback and then ``sys.exit(0)``.
    A missing ``--version`` / ``--exclude`` operand and an unknown option both
    exit 1 (the unknown option after printing usage), same as the CR templates.

    ``support_exclude=False`` drops ``--exclude`` from the grammar so it
    falls through to the unknown-option branch
    (``ansible_github_release`` has no values dir).
    """
    dry_run = False
    target_version = ""
    exclude_patterns = ""

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            usage()
            sys.exit(0)
        elif arg == "--list-backups":
            list_backups()
            sys.exit(0)
        elif arg == "--rollback":
            rollback()
            sys.exit(0)
        elif arg == "--cleanup-backups":
            cleanup_backups()
            sys.exit(0)
        elif arg == "--dry-run":
            dry_run = True
            i += 1
        elif arg == "--exclude" and support_exclude:
            exclude_patterns = argv[i + 1] if i + 1 < len(argv) else ""
            if not exclude_patterns:
                print("ERROR: --exclude requires a pattern (e.g., --exclude old-release,test)")
                sys.exit(1)
            i += 2
        elif arg == "--version":
            target_version = argv[i + 1] if i + 1 < len(argv) else ""
            if not target_version:
                print("ERROR: --version requires a version number")
                sys.exit(1)
            i += 2
        else:
            print(f"Unknown option: {arg}")
            print()
            usage()
            sys.exit(1)

    return {
        "dry_run": dry_run,
        "target_version": target_version,
        "exclude_patterns": exclude_patterns,
    }


# -----------------------------------------------
# Backup listing (per-template row descriptions)
# -----------------------------------------------
#
# Every template prints the same "Available backups:" frame and only
# differs in how it labels each row. ``describe`` receives one backup
# directory and returns the text that goes inside the brackets after
# the timestamp.

def print_backup_list(
    backup_dir: Path, describe: Callable[[Path], str]
) -> None:
    """Print the shared backup-listing frame, one ``describe`` row each.

    Emits nothing but the ``  No backups found.`` line when the backup
    dir is empty or absent — the trailing blank line is skipped in that
    case, matching every template's original early ``return``.
    """
    print("Available backups:")
    print()
    backups = sorted_backups(backup_dir)
    if not backups:
        print("  No backups found.")
        return
    for idx, directory in enumerate(backups, start=1):
        print(f"  [{idx}] {directory.name} {describe(directory)}")
    print()


def backup_file_names(directory: Path, *, files_only: bool = False) -> str:
    """Return the comma-joined sorted child names of a backup directory."""
    entries = directory.iterdir()
    if files_only:
        entries = (p for p in entries if p.is_file())
    return ", ".join(sorted(p.name for p in entries))


# -----------------------------------------------
# _main_flow banner + footer
# -----------------------------------------------
#
# ``external_standard`` / ``ansible_github_release`` /
# ``local_with_templates`` open and close their `_main_flow` with the same
# DOUBLE_SEP-framed blocks; only the per-template extra lines differ.
# Both helpers stay byte-for-byte compatible with the bash bodies, so
# any spacing change here has to be made once instead of three times.

def print_run_banner(
    script_name: str,
    *,
    dry_run: bool,
    target_version: str,
    extra_lines: list[str] | None = None,
) -> None:
    """Print the ``_main_flow`` opening banner.

    ``extra_lines`` are emitted verbatim (leading space included) after
    the Target line — the chart-flavored templates pass the ``Exclude:``
    line, ``ansible_github_release`` the ``Major pin:`` line.
    """
    print(DOUBLE_SEP)
    print(f" {script_name}")
    if dry_run:
        print(" Mode: DRY-RUN (no files will be changed)")
    if target_version:
        print(f" Target: v{target_version}")
    for line in extra_lines or []:
        print(line)
    print(DOUBLE_SEP)


def print_upgrade_footer(
    config: dict,
    current_version: str,
    latest_version: str,
    *,
    next_steps: list[str],
    rollback_header: str = " To rollback:",
    extra_sections: list[list[str]] | None = None,
) -> None:
    """Print the ``Upgrade complete!`` closing block.

    ``extra_sections`` are emitted between the Changelog line and
    ``Next steps:``, each followed by a blank line
    (``local_with_templates`` uses one for its "Custom templates
    preserved:" list). ``rollback_header`` lets ``ansible_github_release``
    widen the label without forking the block.
    """
    print()
    print(DOUBLE_SEP)
    print(f" Upgrade complete! ({current_version} -> {latest_version})")
    print()
    print(f" Changelog: {config['CHANGELOG_URL']}")
    print()
    for section in extra_sections or []:
        for line in section:
            print(line)
        print()
    print(" Next steps:")
    for line in next_steps:
        print(line)
    print()
    print(rollback_header)
    print("   ./upgrade.py --rollback")
    print(DOUBLE_SEP)


# -----------------------------------------------
# YAML helpers (top-level string field read/write)
# -----------------------------------------------
#
# Lifted from ``_common_cr`` (where they originated for the CR templates) once
# ``ansible_github_release`` wanted to drop its own near-identical copy.
# The helpers are pure top-level string-value parsing — no CR / kubectl
# semantics — so they live in the domain-neutral ``_common`` module.

def read_yaml_value(path: Path, key: str) -> str:
    """Return the value of a top-level YAML string field, or empty.

    Strips matching surrounding single or double quotes and any trailing
    ``# comment`` suffix. Returns empty string if the key is missing or
    the file is unreadable. Only top-level (column-0) keys match;
    indented sub-keys are ignored.
    """
    if not path.is_file():
        return ""
    pat = re.compile(rf"^{re.escape(key)}:[ \t]*(.*)$")
    for raw in path.read_text().splitlines():
        m = pat.match(raw)
        if not m:
            continue
        val = m.group(1)
        val = re.sub(r"\s+#.*$", "", val).rstrip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        return val
    return ""


def update_yaml_value(path: Path, key: str, new: str) -> None:
    """Replace the value of a top-level YAML string field in place.

    Preserves the original quoting style: double-quoted stays double,
    single-quoted stays single, bare stays bare. Preserves any trailing
    content after the value (inline comments etc.). Only the first
    match is updated. Missing file is a silent no-op (bash parity).
    """
    if not path.is_file():
        return
    text = path.read_text()
    lines: list[str] = []
    patched = False
    key_pat = re.compile(rf"^{re.escape(key)}:[ \t]+(.*)$")
    for line in text.splitlines(keepends=True):
        if patched:
            lines.append(line)
            continue
        stripped_eol = line.rstrip("\n")
        ending = line[len(stripped_eol):]
        m = key_pat.match(stripped_eol)
        if not m:
            lines.append(line)
            continue
        rest = m.group(1)
        dq = re.match(r'^"[^"]*"(.*)$', rest)
        sq = re.match(r"^'[^']*'(.*)$", rest)
        if dq:
            new_rest = f'"{new}"' + dq.group(1)
        elif sq:
            new_rest = f"'{new}'" + sq.group(1)
        else:
            new_rest = new
        prefix_match = re.match(rf"^({re.escape(key)}:[ \t]+)", stripped_eol)
        assert prefix_match is not None
        prefix = prefix_match.group(1)
        lines.append(f"{prefix}{new_rest}{ending}")
        patched = True
    path.write_text("".join(lines))


def prompt_select_backup(backups: list[Path]) -> Path:
    """Prompt ``Select backup number to restore [1]:`` and return the choice.

    Reads stdin, defaults to ``1`` on empty input or EOF (Ctrl+D in an
    interactive shell), validates the choice is a positive integer within
    ``[1, len(backups)]``, and raises :class:`SystemExit` with status 1 on
    any invalid input. Caller is expected to have already verified
    ``backups`` is non-empty.

    Consolidates the identical prompt block used by ``local_cr_version``,
    ``external_oci_cr_version``, ``local_with_templates``,
    ``ansible_github_release``, and
    the chart-flavored :func:`_common_helmfile.do_rollback`. The EOF
    handling (``input`` raises :class:`EOFError`) matches ``ansible_github_release`` / ``local_with_templates`` /
    ``_common_helmfile`` defensive behavior; ``local_cr_version`` / ``external_oci_cr_version`` gain the same
    graceful default as a strict UX improvement.
    """
    try:
        raw = input("Select backup number to restore [1]: ")
    except EOFError:
        raw = ""
    raw = raw.strip() or "1"
    if not raw.isdigit():
        print("Invalid selection.")
        raise SystemExit(1)
    n = int(raw)
    if n < 1 or n > len(backups):
        print("Invalid selection.")
        raise SystemExit(1)
    return backups[n - 1]


# -----------------------------------------------
# GitHub Releases helpers (shared by ``ansible_github_release`` + ``external_oci``)
# -----------------------------------------------

def _github_releases_request(url: str, *, timeout: float = 10.0) -> str:
    """Fetch a GitHub Releases JSON URL and return the response body.

    Honors ``$GITHUB_TOKEN`` for higher rate limits. Returns empty
    string on any network / HTTP error (callers treat empty as "no
    release found" — matches bash ``curl -fsSL ... || true``).
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return ""


def fetch_github_ga_versions(github_repo: str, major_pin: str = "") -> list[str]:
    """Return GA versions from GitHub Releases, newest-first.

    Strips leading 'v', excludes prereleases / drafts, filters to strict
    ``X.Y.Z`` semver, optionally constrained to ``major_pin``. Mirrors
    the bash inline ``python3 -c`` block in ``ansible-github-release.sh``
    — same fields, same filters, same sort key.

    Used by ``ansible_github_release``. Named ``fetch_github_ga_versions``
    (not ``fetch_ga_versions``) to avoid collision with the 3-source
    homonym in ``_common_cr`` (different signature, different backend).
    """
    if not github_repo:
        return []
    url = (
        f"https://api.github.com/repos/{github_repo}/releases"
        f"?per_page={GITHUB_RELEASES_PER_PAGE}"
    )
    payload = _github_releases_request(url)
    if not payload:
        return []
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, list):
        return []

    tags: list[str] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        if r.get("prerelease") or r.get("draft"):
            continue
        tag = r.get("tag_name", "") or ""
        # strip leading 'v'
        tag = re.sub(r"^v", "", tag)
        tags.append(tag)

    ga = [t for t in tags if re.fullmatch(r"\d+\.\d+\.\d+", t)]
    if major_pin:
        ga = [t for t in ga if t.startswith(major_pin + ".")]
    ga.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    return ga


def fetch_latest_release_tag(github_repo: str, tag_prefix: str = "v") -> str:
    """Return the newest release tag matching ``tag_prefix``, or "" on failure.

    Mirrors the OCI bash template's two-branch flow:
      - When ``tag_prefix`` is "" or "v" (single-chart repo): hit
        ``/releases/latest`` and read ``tag_name``.
      - Otherwise (multi-chart repo, e.g. ``somaz94/helm-charts`` with
        ``keycloak-cr-`` prefix): scan ``/releases?per_page=30`` and
        return the first tag whose name starts with ``tag_prefix``.

    Used by ``external_oci``. Empty string on any error or no match.
    """
    if not github_repo:
        return ""
    if not tag_prefix or tag_prefix == "v":
        url = f"https://api.github.com/repos/{github_repo}/releases/latest"
        payload = _github_releases_request(url)
        if not payload:
            return ""
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            return ""
        if not isinstance(data, dict):
            return ""
        return data.get("tag_name", "") or ""

    # Multi-chart prefix scan.
    url = (
        f"https://api.github.com/repos/{github_repo}/releases"
        f"?per_page={GITHUB_RECENT_RELEASES_PER_PAGE}"
    )
    payload = _github_releases_request(url)
    if not payload:
        return ""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, list):
        return ""
    for r in data:
        if not isinstance(r, dict):
            continue
        tag = r.get("tag_name", "") or ""
        if tag.startswith(tag_prefix):
            return tag
    return ""


# =============================================================
# MAJOR-bump confirmation prompt — shared by ``ansible_github_release``
# and both CR templates
# =============================================================

_MAJOR_BANNER = "  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"

# Extra warning lines appended (between the BUMP line and the changelog
# line) by ``local_cr_version`` and ``external_oci_cr_version``.
# Stateful CR consumers (Elasticsearch / Kibana / future operators) carry
# data that is not reversible across a major bump — surface the operator
# backup requirement before the user confirms.
DATA_BACKUP_WARNING: tuple[str, ...] = (
    "  !!",
    "  !! STRONGLY RECOMMENDED: back up application data using",
    "  !! the component's native backup/snapshot mechanism",
    "  !! before proceeding.",
    "  !!",
    "  !! Major bumps commonly include deprecated setting removals",
    "  !! and data format changes that are not reversible.",
)


def prompt_major_bump(
    current_version: str,
    latest_version: str,
    changelog_url: str,
    dry_run: bool,
    *,
    extra_lines: tuple[str, ...] = (),
) -> bool:
    """Print the MAJOR-bump banner + optionally prompt the user.

    Returns ``True`` when the caller should continue:

    - The major component of ``current_version`` and ``latest_version``
      is equal (no MAJOR bump → banner skipped, nothing prompted).
    - ``dry_run`` is set (banner printed for visibility, no prompt).
    - The user typed an answer starting with ``y`` / ``Y`` (e.g. ``y``,
      ``yes``, ``YES``).

    Returns ``False`` when the user declined (any other answer, empty
    input, or ``EOFError`` from a closed stdin). In that case the caller
    should propagate a non-zero exit code; the helper has already
    printed ``Aborted.``.

    ``extra_lines`` are inserted verbatim between the BUMP line and the
    changelog line — pass :data:`DATA_BACKUP_WARNING` for stateful CR
    consumers, leave empty for stateless components.
    """
    current_major = current_version.split(".", 1)[0] if current_version else ""
    latest_major = latest_version.split(".", 1)[0] if latest_version else ""
    if not current_major or not latest_major or current_major == latest_major:
        return True
    print()
    print(_MAJOR_BANNER)
    print(f"  !! MAJOR VERSION BUMP: {current_major}.x -> {latest_major}.x")
    for line in extra_lines:
        print(line)
    print(f"  !! Review breaking changes: {changelog_url}")
    print(_MAJOR_BANNER)
    if dry_run:
        return True
    print()
    try:
        confirm = input("  Continue with major version upgrade? [y/N]: ").strip()
    except EOFError:
        confirm = ""
    if not confirm.lower().startswith("y"):
        print("Aborted.")
        return False
    return True

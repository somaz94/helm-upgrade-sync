"""Unit tests for upgrade_core/_common.py.

Covers the 4 helpers extracted into :mod:`upgrade_core._common`:
sorted_backups, cleanup_backups, auto_prune_backups, is_excluded.

Stdlib unittest only — keeps dep surface at zero.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

cm = load("upgrade_core._common")


def _mkdir_p(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _make_backup_tree(timestamps: list[str]) -> Path:
    tmp = Path(tempfile.mkdtemp())
    bdir = tmp / "backup"
    bdir.mkdir()
    for ts in timestamps:
        _mkdir_p(bdir / ts)
    return bdir


class SortedBackupsTests(unittest.TestCase):
    def test_missing_directory_returns_empty(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.assertEqual(cm.sorted_backups(tmp / "missing"), [])
        shutil.rmtree(tmp)

    def test_empty_directory_returns_empty(self) -> None:
        bdir = _make_backup_tree([])
        try:
            self.assertEqual(cm.sorted_backups(bdir), [])
        finally:
            shutil.rmtree(bdir.parent)

    def test_only_timestamp_dirs_match_glob(self) -> None:
        bdir = _make_backup_tree(["20260101_000000", "20260520_120000"])
        # Add a noise dir that does NOT start with '2' so the glob skips it.
        (bdir / "noise").mkdir()
        try:
            names = [p.name for p in cm.sorted_backups(bdir)]
            self.assertEqual(names, ["20260520_120000", "20260101_000000"])
        finally:
            shutil.rmtree(bdir.parent)

    def test_descending_order_by_name(self) -> None:
        bdir = _make_backup_tree(
            ["20240101_010000", "20260520_120000", "20260101_000000"]
        )
        try:
            names = [p.name for p in cm.sorted_backups(bdir)]
            self.assertEqual(
                names,
                ["20260520_120000", "20260101_000000", "20240101_010000"],
            )
        finally:
            shutil.rmtree(bdir.parent)

    def test_files_under_2_prefix_are_ignored(self) -> None:
        bdir = _make_backup_tree(["20260520_120000"])
        # A file (not a dir) starting with '2' must be filtered out.
        (bdir / "2-stray.txt").write_text("not a dir")
        try:
            names = [p.name for p in cm.sorted_backups(bdir)]
            self.assertEqual(names, ["20260520_120000"])
        finally:
            shutil.rmtree(bdir.parent)


class CleanupBackupsTests(unittest.TestCase):
    def _run(self, bdir: Path, keep: int) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cm.cleanup_backups(bdir, keep)
        return buf.getvalue()

    def test_no_backups_message(self) -> None:
        bdir = _make_backup_tree([])
        try:
            out = self._run(bdir, 5)
            self.assertEqual(out, "No backups found.\n")
        finally:
            shutil.rmtree(bdir.parent)

    def test_total_within_keep_says_nothing_to_clean(self) -> None:
        bdir = _make_backup_tree(["20260101_000000", "20260102_000000"])
        try:
            out = self._run(bdir, 5)
            self.assertIn("Total backups: 2 (keeping last 5)", out)
            self.assertIn("Nothing to clean up.", out)
            # Both dirs survive.
            survivors = sorted(p.name for p in bdir.iterdir())
            self.assertEqual(
                survivors, ["20260101_000000", "20260102_000000"]
            )
        finally:
            shutil.rmtree(bdir.parent)

    def test_prunes_oldest_keeping_top_n(self) -> None:
        ts = [f"2026010{i}_000000" for i in (1, 2, 3, 4, 5, 6)]
        bdir = _make_backup_tree(ts)
        try:
            out = self._run(bdir, 3)
            # Verbose header + 3 victims (oldest 3) + Done.
            self.assertIn("Total backups: 6 (keeping last 3)", out)
            self.assertIn("Removing 3 old backup(s)...", out)
            for victim in ("20260101_000000", "20260102_000000", "20260103_000000"):
                self.assertIn(f"Removed: {victim}", out)
            self.assertIn("Done.", out)
            survivors = sorted(p.name for p in bdir.iterdir())
            self.assertEqual(
                survivors,
                ["20260104_000000", "20260105_000000", "20260106_000000"],
            )
        finally:
            shutil.rmtree(bdir.parent)


class AutoPruneBackupsTests(unittest.TestCase):
    def _run(self, bdir: Path, keep: int) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cm.auto_prune_backups(bdir, keep)
        return buf.getvalue()

    def test_missing_directory_silent_noop(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            out = self._run(tmp / "missing", 5)
            self.assertEqual(out, "")
        finally:
            shutil.rmtree(tmp)

    def test_within_keep_silent_noop(self) -> None:
        bdir = _make_backup_tree(["20260101_000000", "20260102_000000"])
        try:
            out = self._run(bdir, 5)
            self.assertEqual(out, "")
            # No removal.
            self.assertEqual(len(list(bdir.iterdir())), 2)
        finally:
            shutil.rmtree(bdir.parent)

    def test_emits_auto_prune_line_when_pruning(self) -> None:
        ts = [f"2026010{i}_000000" for i in (1, 2, 3, 4, 5)]
        bdir = _make_backup_tree(ts)
        try:
            out = self._run(bdir, 2)
            self.assertIn("Auto-pruned 3 old backup(s) (KEEP_BACKUPS=2).", out)
            survivors = sorted(p.name for p in bdir.iterdir())
            self.assertEqual(
                survivors, ["20260104_000000", "20260105_000000"]
            )
        finally:
            shutil.rmtree(bdir.parent)


class IsExcludedTests(unittest.TestCase):
    def test_empty_patterns_never_excludes(self) -> None:
        self.assertFalse(cm.is_excluded("dev.yaml", ""))

    def test_single_pattern_substring_match(self) -> None:
        self.assertTrue(cm.is_excluded("dev-old-release.yaml", "old-release"))
        self.assertFalse(cm.is_excluded("dev.yaml", "old-release"))

    def test_comma_separated_patterns(self) -> None:
        self.assertTrue(cm.is_excluded("dev-test.yaml", "old-release,test"))
        self.assertTrue(cm.is_excluded("foo-old-release.yaml", "old-release,test"))
        self.assertFalse(cm.is_excluded("dev.yaml", "old-release,test"))

    def test_empty_individual_pattern_does_not_short_circuit(self) -> None:
        # Trailing comma produces an empty token — must not match every file.
        self.assertFalse(cm.is_excluded("dev.yaml", "test,"))
        self.assertTrue(cm.is_excluded("dev-test.yaml", "test,"))


class PromptSelectBackupTests(unittest.TestCase):
    """Tests for prompt_select_backup, shared by ``ansible_github_release``,
    ``local_with_templates``, and both CR templates."""

    def _backups(self, n: int) -> list[Path]:
        return [Path(f"backup/2026010{i}_000000") for i in range(1, n + 1)]

    def _run(self, backups: list[Path], stdin_value: str) -> tuple[Path | None, str]:
        out = io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin_value)
        try:
            with redirect_stdout(out):
                result = cm.prompt_select_backup(backups)
            return result, out.getvalue()
        finally:
            sys.stdin = original_stdin

    def test_default_returns_first_on_empty_input(self) -> None:
        backups = self._backups(3)
        result, _ = self._run(backups, "\n")
        self.assertEqual(result, backups[0])

    def test_default_returns_first_on_eof(self) -> None:
        # Empty stdin → input() raises EOFError → falls through to "1".
        backups = self._backups(3)
        result, _ = self._run(backups, "")
        self.assertEqual(result, backups[0])

    def test_valid_selection_returns_matching_backup(self) -> None:
        backups = self._backups(3)
        result, _ = self._run(backups, "2\n")
        self.assertEqual(result, backups[1])

    def test_non_digit_input_raises_systemexit(self) -> None:
        backups = self._backups(3)
        out = io.StringIO()
        sys.stdin = io.StringIO("abc\n")
        try:
            with redirect_stdout(out), self.assertRaises(SystemExit) as cm_exc:
                cm.prompt_select_backup(backups)
            self.assertEqual(cm_exc.exception.code, 1)
            self.assertIn("Invalid selection.", out.getvalue())
        finally:
            sys.stdin = sys.__stdin__

    def test_out_of_range_low_raises_systemexit(self) -> None:
        backups = self._backups(3)
        sys.stdin = io.StringIO("0\n")
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cm.prompt_select_backup(backups)
        finally:
            sys.stdin = sys.__stdin__

    def test_out_of_range_high_raises_systemexit(self) -> None:
        backups = self._backups(3)
        sys.stdin = io.StringIO("4\n")
        try:
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                cm.prompt_select_backup(backups)
        finally:
            sys.stdin = sys.__stdin__


class PromptMajorBumpTests(unittest.TestCase):
    """``prompt_major_bump`` covers the shared MAJOR-bump banner +
    optional confirmation prompt used by ``ansible_github_release``,
    ``local_cr_version``, and ``external_oci_cr_version``."""

    CHANGELOG_URL = "https://example/changelog"

    def test_no_bump_short_circuits_true(self) -> None:
        """Same major → no banner, no prompt, return True."""
        with redirect_stdout(io.StringIO()) as out:
            ok = cm.prompt_major_bump(
                "1.5.0", "1.6.0", self.CHANGELOG_URL, dry_run=False,
            )
        self.assertTrue(ok)
        self.assertEqual(out.getvalue(), "")

    def test_empty_versions_short_circuit_true(self) -> None:
        # An empty current_version (first run) shouldn't trip the bump
        # banner — it would print a meaningless ``.x -> N.x`` line.
        with redirect_stdout(io.StringIO()) as out:
            ok = cm.prompt_major_bump(
                "", "1.0.0", self.CHANGELOG_URL, dry_run=False,
            )
        self.assertTrue(ok)
        self.assertEqual(out.getvalue(), "")

    def test_dry_run_prints_banner_and_returns_true(self) -> None:
        with redirect_stdout(io.StringIO()) as out:
            ok = cm.prompt_major_bump(
                "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=True,
            )
        self.assertTrue(ok)
        body = out.getvalue()
        self.assertIn("MAJOR VERSION BUMP: 1.x -> 2.x", body)
        self.assertIn(self.CHANGELOG_URL, body)
        # No prompt line emitted under dry_run.
        self.assertNotIn("Continue with major version upgrade?", body)

    def test_user_confirms_y(self) -> None:
        sys.stdin = io.StringIO("y\n")
        try:
            with redirect_stdout(io.StringIO()) as out:
                ok = cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                )
        finally:
            sys.stdin = sys.__stdin__
        self.assertTrue(ok)
        self.assertIn("MAJOR VERSION BUMP: 1.x -> 2.x", out.getvalue())

    def test_user_confirms_yes_case_insensitive(self) -> None:
        sys.stdin = io.StringIO("YES\n")
        try:
            with redirect_stdout(io.StringIO()):
                ok = cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                )
        finally:
            sys.stdin = sys.__stdin__
        self.assertTrue(ok)

    def test_user_declines_n(self) -> None:
        sys.stdin = io.StringIO("n\n")
        try:
            with redirect_stdout(io.StringIO()) as out:
                ok = cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                )
        finally:
            sys.stdin = sys.__stdin__
        self.assertFalse(ok)
        self.assertIn("Aborted.", out.getvalue())

    def test_empty_answer_declines(self) -> None:
        sys.stdin = io.StringIO("\n")
        try:
            with redirect_stdout(io.StringIO()) as out:
                ok = cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                )
        finally:
            sys.stdin = sys.__stdin__
        self.assertFalse(ok)
        self.assertIn("Aborted.", out.getvalue())

    def test_eof_declines(self) -> None:
        # Closed stdin → EOFError caught by the helper → False return.
        sys.stdin = io.StringIO("")
        try:
            with redirect_stdout(io.StringIO()) as out:
                ok = cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                )
        finally:
            sys.stdin = sys.__stdin__
        self.assertFalse(ok)
        self.assertIn("Aborted.", out.getvalue())

    def test_extra_lines_inserted_between_bump_and_changelog(self) -> None:
        """Stateful CR consumers inject the data-backup
        warning between the BUMP line and the changelog line."""
        sys.stdin = io.StringIO("y\n")
        try:
            with redirect_stdout(io.StringIO()) as out:
                cm.prompt_major_bump(
                    "1.5.0", "2.0.0", self.CHANGELOG_URL, dry_run=False,
                    extra_lines=cm.DATA_BACKUP_WARNING,
                )
        finally:
            sys.stdin = sys.__stdin__
        body = out.getvalue()
        # All 7 warning lines present, in order.
        for line in cm.DATA_BACKUP_WARNING:
            self.assertIn(line, body)
        bump_idx = body.index("MAJOR VERSION BUMP")
        warn_idx = body.index("STRONGLY RECOMMENDED")
        changelog_idx = body.index(self.CHANGELOG_URL)
        self.assertLess(bump_idx, warn_idx)
        self.assertLess(warn_idx, changelog_idx)


if __name__ == "__main__":
    unittest.main()

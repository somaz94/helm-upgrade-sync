"""Unit tests for upgrade_sync.manage_backups.

Every assertion builds a self-contained mini repo under
``tempfile.TemporaryDirectory`` so the suite never depends on the host
repo's live backup state (which changes whenever ``upgrade.py`` runs).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from _loader import REPO_ROOT, TOOL_ROOT, load


mb = load("upgrade_sync.manage_backups")


SCRIPT = TOOL_ROOT / "manage-backups.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "manage-backups"


CONSUMER_HEADER = (
    "#!/usr/bin/env python3\n"
    "# upgrade-template: external-standard\n"
    "\n"
    "# stub for tests\n"
)


def _seed_consumer(root: Path, rel_dir: str) -> Path:
    """Place a minimal upgrade.py under ``rel_dir``."""
    chart_dir = root / rel_dir
    chart_dir.mkdir(parents=True, exist_ok=True)
    upgrade = chart_dir / "upgrade.py"
    upgrade.write_text(CONSUMER_HEADER, encoding="utf-8")
    return chart_dir


def _seed_backup(chart_dir: Path, name: str, file_bytes: int = 1024) -> Path:
    """Create ``chart_dir/backup/<name>/`` with a single file of ``file_bytes``."""
    snap = chart_dir / "backup" / name
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "values.yaml.bak").write_bytes(b"x" * file_bytes)
    return snap


# ---------------------------------------------------------------------------
# human_size
# ---------------------------------------------------------------------------


class HumanSizeTests(unittest.TestCase):
    def test_bytes(self) -> None:
        self.assertEqual(mb.human_size(0), "0B")
        self.assertEqual(mb.human_size(1023), "1023B")

    def test_kilobytes(self) -> None:
        self.assertEqual(mb.human_size(1024), "1K")
        self.assertEqual(mb.human_size(1048575), "1023K")

    def test_megabytes(self) -> None:
        self.assertEqual(mb.human_size(1048576), "1.0M")
        self.assertEqual(mb.human_size(2 * 1048576 + 100 * 1024), "2.1M")

    def test_gigabytes(self) -> None:
        self.assertEqual(mb.human_size(1073741824), "1.0G")
        self.assertEqual(mb.human_size(int(2.5 * 1073741824)), "2.5G")


# ---------------------------------------------------------------------------
# chart_backup_stats
# ---------------------------------------------------------------------------


class ChartBackupStatsTests(unittest.TestCase):
    def test_empty_when_no_backup_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            self.assertEqual(mb.chart_backup_stats(chart), (0, 0, "", ""))

    def test_counts_and_sums_snapshot_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            _seed_backup(chart, "20260101_010101", file_bytes=2048)
            _seed_backup(chart, "20260514_120000", file_bytes=1024)
            count, total, oldest, newest = mb.chart_backup_stats(chart)
            self.assertEqual(count, 2)
            self.assertEqual(total, 3072)
            # Lex sort == time order because of YYYYMMDD_HHMMSS naming.
            self.assertEqual(oldest, "20260101_010101")
            self.assertEqual(newest, "20260514_120000")

    def test_ignores_non_2_prefix_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            _seed_backup(chart, "20260101_010101")
            # Stray dir without "2" prefix should be skipped.
            (chart / "backup" / "stray").mkdir()
            (chart / "backup" / "stray" / "f.bak").write_bytes(b"yyyy")
            count, _, _, _ = mb.chart_backup_stats(chart)
            self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# chart_prune
# ---------------------------------------------------------------------------


class ChartPruneTests(unittest.TestCase):
    def test_noop_when_within_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            _seed_backup(chart, "20260101_010101")
            _seed_backup(chart, "20260102_020202")
            removed, freed = mb.chart_prune(chart, keep=5)
            self.assertEqual((removed, freed), (0, 0))
            # Both snapshots intact.
            self.assertEqual(len(list((chart / "backup").iterdir())), 2)

    def test_keeps_n_most_recent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            old1 = _seed_backup(chart, "20260101_010101", file_bytes=1024)
            old2 = _seed_backup(chart, "20260102_020202", file_bytes=1024)
            new = _seed_backup(chart, "20260514_120000", file_bytes=1024)
            # Re-stamp mtimes so chart_prune's mtime-desc sort sees the
            # expected order regardless of fs creation jitter.
            old_mtime = time.time() - 7 * 86400
            mid_mtime = time.time() - 86400
            new_mtime = time.time()
            os.utime(old1, (old_mtime, old_mtime))
            os.utime(old2, (mid_mtime, mid_mtime))
            os.utime(new, (new_mtime, new_mtime))
            removed, freed = mb.chart_prune(chart, keep=1)
            self.assertEqual(removed, 2)
            self.assertGreater(freed, 0)
            remaining = sorted(p.name for p in (chart / "backup").iterdir())
            self.assertEqual(remaining, ["20260514_120000"])

    def test_keep_zero_removes_all_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            _seed_backup(chart, "20260101_010101")
            _seed_backup(chart, "20260102_020202")
            removed, _ = mb.chart_prune(chart, keep=0)
            self.assertEqual(removed, 2)
            self.assertEqual(list((chart / "backup").iterdir()), [])


# ---------------------------------------------------------------------------
# chart_purge
# ---------------------------------------------------------------------------


class ChartPurgeTests(unittest.TestCase):
    def test_removes_snapshots_and_parent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            _seed_backup(chart, "20260101_010101", file_bytes=1024)
            _seed_backup(chart, "20260102_020202", file_bytes=2048)
            removed, freed = mb.chart_purge(chart)
            self.assertEqual(removed, 2)
            self.assertEqual(freed, 3072)
            # backup/ dir removed too (no other entries).
            self.assertFalse((chart / "backup").exists())

    def test_noop_when_no_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            chart = _seed_consumer(Path(tmp), "comp-a")
            removed, freed = mb.chart_purge(chart)
            self.assertEqual((removed, freed), (0, 0))


# ---------------------------------------------------------------------------
# cmd_list / cmd_total_size — captured stdout assertions
# ---------------------------------------------------------------------------


class CmdListTests(unittest.TestCase):
    def test_header_and_per_chart_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = _seed_consumer(root, "comp-a")
            _seed_backup(chart, "20260101_010101", file_bytes=1024)
            _seed_backup(chart, "20260514_120000", file_bytes=2048)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_list(root)
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("CHART", out)
            self.assertIn("COUNT", out)
            self.assertIn("comp-a", out)
            self.assertIn("Total: 2 backup(s) across all charts, 3K", out)

    def test_charts_with_no_backups_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_consumer(root, "comp-a")  # no backups
            chart_b = _seed_consumer(root, "comp-b")
            _seed_backup(chart_b, "20260514_120000")
            buf = io.StringIO()
            with redirect_stdout(buf):
                mb.cmd_list(root)
            out = buf.getvalue()
            self.assertNotIn("comp-a", out.splitlines()[2:][0] if len(out.splitlines()) > 2 else "")
            self.assertIn("comp-b", out)


class CmdTotalSizeTests(unittest.TestCase):
    def test_one_line_grand_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart_a = _seed_consumer(root, "comp-a")
            chart_b = _seed_consumer(root, "comp-b")
            _seed_backup(chart_a, "20260101_010101", file_bytes=512)
            _seed_backup(chart_b, "20260102_020202", file_bytes=1024)
            _seed_backup(chart_b, "20260514_120000", file_bytes=512)
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_total_size(root)
            self.assertEqual(rc, 0)
            self.assertEqual(
                buf.getvalue().strip(),
                "Total: 3 backup(s) in 2 chart(s), 2K",
            )

    def test_empty_repo_zero_total(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _seed_consumer(root, "comp-a")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_total_size(root)
            self.assertEqual(rc, 0)
            self.assertEqual(
                buf.getvalue().strip(),
                "Total: 0 backup(s) in 0 chart(s), 0B",
            )


# ---------------------------------------------------------------------------
# cmd_cleanup / cmd_purge
# ---------------------------------------------------------------------------


class CmdCleanupTests(unittest.TestCase):
    def test_within_retention_says_nothing_to_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = _seed_consumer(root, "comp-a")
            _seed_backup(chart, "20260514_120000")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_cleanup(root, keep=5)
            self.assertEqual(rc, 0)
            self.assertIn("Nothing to clean up", buf.getvalue())

    def test_keep_one_removes_older(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = _seed_consumer(root, "comp-a")
            old = _seed_backup(chart, "20260101_010101")
            new = _seed_backup(chart, "20260514_120000")
            old_mtime = time.time() - 7 * 86400
            os.utime(old, (old_mtime, old_mtime))
            os.utime(new, (time.time(), time.time()))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_cleanup(root, keep=1)
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            self.assertIn("removed=1", out)
            self.assertIn("Removed 1 backup(s) total", out)

    def test_negative_keep_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            buf_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
                rc = mb.cmd_cleanup(Path(tmp), keep=-1)
            self.assertEqual(rc, 2)
            self.assertIn("non-negative integer", buf_err.getvalue())


class CmdPurgeTests(unittest.TestCase):
    def test_purge_confirmation_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = _seed_consumer(root, "comp-a")
            _seed_backup(chart, "20260514_120000")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_purge(root, input_fn=lambda _: "no")
            self.assertEqual(rc, 1)
            self.assertIn("Aborted.", buf.getvalue())
            # Snapshot untouched.
            self.assertTrue((chart / "backup" / "20260514_120000").exists())

    def test_purge_executes_when_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chart = _seed_consumer(root, "comp-a")
            _seed_backup(chart, "20260514_120000")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.cmd_purge(root, input_fn=lambda _: "PURGE")
            self.assertEqual(rc, 0)
            self.assertIn("Purged 1 backup(s) total", buf.getvalue())
            self.assertFalse((chart / "backup").exists())


# ---------------------------------------------------------------------------
# CLI dispatcher
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def _make_repo(self) -> tuple[Path, str]:
        tmp = tempfile.mkdtemp()
        root = Path(tmp)
        (root / "scripts" / "upgrade-sync").mkdir(parents=True)
        sync_py = root / "scripts" / "upgrade-sync" / "manage-backups.py"
        sync_py.write_text("# stub", encoding="utf-8")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        return root, str(sync_py)

    def test_zero_args_prints_usage(self) -> None:
        _, sp = self._make_repo()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mb.main([], script_path=sp)
        self.assertEqual(rc, 0)
        self.assertIn("Usage: manage-backups.py", buf.getvalue())

    def test_help_flags(self) -> None:
        _, sp = self._make_repo()
        for flag in ("-h", "--help"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mb.main([flag], script_path=sp)
            self.assertEqual(rc, 0, flag)
            self.assertIn("--cleanup", buf.getvalue())

    def test_unknown_command_is_usage_error_on_stderr(self) -> None:
        _, sp = self._make_repo()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mb.main(["--bogus"], script_path=sp)
        self.assertEqual(rc, 2)
        self.assertIn("Unknown command: --bogus", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_total_size_on_empty_repo(self) -> None:
        root, sp = self._make_repo()
        _seed_consumer(root, "comp-a")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mb.main(["--total-size"], script_path=sp)
        self.assertEqual(rc, 0)
        self.assertIn("Total: 0 backup(s) in 0 chart(s)", buf.getvalue())

    def test_cleanup_keep_parsed(self) -> None:
        root, sp = self._make_repo()
        _seed_consumer(root, "comp-a")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = mb.main(["--cleanup", "--keep", "3"], script_path=sp)
        self.assertEqual(rc, 0)
        self.assertIn("Pruning backups across all charts (keep last 3 per chart)", buf.getvalue())

    def test_cleanup_keep_missing_value_rc2(self) -> None:
        _, sp = self._make_repo()
        buf_err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
            rc = mb.main(["--cleanup", "--keep"], script_path=sp)
        self.assertEqual(rc, 2)
        self.assertIn("--keep requires a number", buf_err.getvalue())

    def test_cleanup_keep_non_integer_rc2(self) -> None:
        _, sp = self._make_repo()
        buf_err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(buf_err):
            rc = mb.main(["--cleanup", "--keep", "abc"], script_path=sp)
        self.assertEqual(rc, 2)

    def test_cleanup_unknown_option_is_usage_error_on_stderr(self) -> None:
        _, sp = self._make_repo()
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = mb.main(["--cleanup", "--kep", "3"], script_path=sp)
        self.assertEqual(rc, 2)
        self.assertIn("Unknown option: --kep", err.getvalue())
        self.assertEqual(out.getvalue(), "")


# ---------------------------------------------------------------------------
# Golden-file byte parity (help text — the only static command output).
# ---------------------------------------------------------------------------


class HelpGoldenFileTests(unittest.TestCase):
    def setUp(self) -> None:
        if not SCRIPT.exists():
            self.skipTest("manage-backups.py not yet present in working tree")
        if not (FIXTURES / "help.txt").is_file():
            self.skipTest("help fixture missing — capture it before running")

    def _run(self, args: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            check=False,
            capture_output=True,
            cwd=str(REPO_ROOT),
            text=True,
        )
        return result.returncode, result.stdout + result.stderr

    def test_help_short_flag_byte_parity(self) -> None:
        rc, out = self._run(["-h"])
        out += f"rc={rc}\n"
        expected = (FIXTURES / "help.txt").read_text(encoding="utf-8")
        self.assertEqual(out, expected)

    def test_help_long_flag_byte_parity(self) -> None:
        rc, out = self._run(["--help"])
        out += f"rc={rc}\n"
        expected = (FIXTURES / "help.txt").read_text(encoding="utf-8")
        self.assertEqual(out, expected)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for upgrade_sync.commands + cli.

Every case builds a synthetic mini repo under ``tempfile.TemporaryDirectory``
and exercises one command against it — drift, clean tree, dirty tree, missing
header. Nothing here reads the repository the tool is checked out into, so the
suite gives the same result wherever it runs.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

from _loader import load


commands = load("upgrade_sync.commands")
cli = load("upgrade_sync.cli")


FENCE = "# " + ("=" * 60)
PY_SHEBANG = "#!/usr/bin/env python3"
PY_HEADER = "# upgrade-template: demo"


# ---------------------------------------------------------------------------
# Test fixture helpers
# ---------------------------------------------------------------------------


def _build_mini_repo(root: Path) -> Path:
    """Lay out a single-template mini repo under ``root``."""
    templates = root / "scripts" / "upgrade-sync" / "templates"
    templates.mkdir(parents=True)
    (templates / "demo.py").write_text(
        f"{PY_SHEBANG}\n{FENCE}\nx\n{FENCE}\ny\n{FENCE}\ncanonical body line 1\ncanonical body line 2\n",
        encoding="utf-8",
    )
    return templates


def _seed_consumer(root: Path, rel: str, *, header: bool, body: str | None = None) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    head = f"{PY_SHEBANG}\n"
    if header:
        head += f"{PY_HEADER}\n"
    head += "\n"
    config = f"{FENCE}\nCONFIG_LINE\n{FENCE}\nKEY = 'value'\n{FENCE}\n"
    tail = body if body is not None else "canonical body line 1\ncanonical body line 2\n"
    p.write_text(head + config + tail, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# cmd_check tests
# ---------------------------------------------------------------------------


class CmdCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.templates = _build_mini_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_clean_tree_returns_0(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_check(self.root, self.templates)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("OK    [demo            ]", out)
        self.assertIn("All 1 managed file(s) are in sync.", out)

    def test_drift_returns_1(self) -> None:
        _seed_consumer(
            self.root,
            "comp/upgrade.py",
            header=True,
            body="DRIFTED BODY\n",
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_check(self.root, self.templates)
        out = buf.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("DRIFT [demo            ]", out)
        self.assertIn("1 of 1 managed file(s) have drift.", out)
        self.assertIn("To fix: sync.py --apply", out)

    def test_missing_header_is_skipped_not_an_error(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_check(self.root, self.templates)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("SKIP  [no-header        ]", out)
        self.assertIn("0 managed file(s) in sync. 1 skipped (no header).", out)


# ---------------------------------------------------------------------------
# cmd_apply tests
# ---------------------------------------------------------------------------


class CmdApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.templates = _build_mini_repo(self.root)
        # ``cmd_apply`` calls git diff for dirty-tree guard. We patch it
        # globally per-test rather than initializing a real repo.
        self._patcher = mock.patch.object(commands, "_git_tree_dirty", return_value=False)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self._tmp.cleanup()

    def test_clean_tree_no_drift_reports_zero_updates(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_apply(self.root, self.templates)
        self.assertEqual(rc, 0)
        self.assertIn("Updated 0 of 1 managed file(s)", buf.getvalue())

    def test_drift_is_rewritten_and_chmod_x(self) -> None:
        p = _seed_consumer(
            self.root, "comp/upgrade.py", header=True, body="OLD\n"
        )
        # Strip exec bits so we can detect chmod +x having run.
        os.chmod(p, 0o644)
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_apply(self.root, self.templates)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("WROTE [demo            ] comp/upgrade.py", out)
        self.assertIn("Updated 1 of 1 managed file(s)", out)
        self.assertIn("canonical body line 1", p.read_text(encoding="utf-8"))
        mode = stat.S_IMODE(p.stat().st_mode)
        self.assertTrue(mode & 0o111, f"exec bit missing: {oct(mode)}")

    def test_dirty_tree_aborts_with_rc3(self) -> None:
        self._patcher.stop()
        try:
            with mock.patch.object(commands, "_git_tree_dirty", return_value=True):
                _seed_consumer(self.root, "comp/upgrade.py", header=True)
                buf_out = io.StringIO()
                buf_err = io.StringIO()
                with redirect_stdout(buf_out), redirect_stderr(buf_err):
                    rc = commands.cmd_apply(self.root, self.templates)
                self.assertEqual(rc, 3)
                self.assertIn("working tree is dirty", buf_err.getvalue())
        finally:
            self._patcher = mock.patch.object(commands, "_git_tree_dirty", return_value=False)
            self._patcher.start()

    def test_force_overrides_dirty_tree(self) -> None:
        self._patcher.stop()
        try:
            with mock.patch.object(commands, "_git_tree_dirty", return_value=True):
                p = _seed_consumer(
                    self.root, "comp/upgrade.py", header=True, body="OLD\n"
                )
                buf = io.StringIO()
                with redirect_stdout(buf):
                    rc = commands.cmd_apply(self.root, self.templates, force=True)
                self.assertEqual(rc, 0)
                self.assertIn("canonical body line 1", p.read_text(encoding="utf-8"))
        finally:
            self._patcher = mock.patch.object(commands, "_git_tree_dirty", return_value=False)
            self._patcher.start()


# ---------------------------------------------------------------------------
# cmd_status / cmd_print_expected tests
# ---------------------------------------------------------------------------


class CmdStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.templates = _build_mini_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_lists_managed_canonicals_and_unmanaged(self) -> None:
        _seed_consumer(self.root, "comp-a/upgrade.py", header=True)
        # An unmanaged Chart.yaml (no upgrade.{sh,py} sibling).
        (self.root / "comp-b").mkdir()
        (self.root / "comp-b" / "Chart.yaml").write_text("", encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_status(self.root, self.templates)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("Managed upgrade.{sh,py} files: 1", out)
        self.assertIn("demo:", out)
        self.assertIn("Available canonicals:", out)
        self.assertIn("  demo", out)
        self.assertIn("Unmanaged chart directories", out)
        self.assertIn("- comp-b", out)

    def test_unmanaged_empty_shows_none(self) -> None:
        _seed_consumer(self.root, "comp-a/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_status(self.root, self.templates)
        self.assertEqual(rc, 0)
        self.assertIn("(none)", buf.getvalue())


class CmdPrintExpectedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.templates = _build_mini_repo(self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_emits_expected_content(self) -> None:
        p = _seed_consumer(self.root, "comp/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = commands.cmd_print_expected(self.root, self.templates, str(p))
        self.assertEqual(rc, 0)
        self.assertIn("canonical body line 1", buf.getvalue())
        self.assertTrue(buf.getvalue().startswith("#!/usr/bin/env python3"))

    def test_missing_file_returns_1(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(io.StringIO()):
            rc = commands.cmd_print_expected(self.root, self.templates, None)
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# CLI dispatcher tests (argv → cmd_*)
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.templates = _build_mini_repo(self.root)
        # Make sync.py "appear" to live at the mini repo's
        # scripts/upgrade-sync/sync.py path so cli resolves templates_dir
        # and repo_root via its own heuristic.
        sync_py = self.root / "scripts" / "upgrade-sync" / "sync.py"
        sync_py.write_text("# stub", encoding="utf-8")
        self.sync_py = str(sync_py)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_zero_args_prints_usage_rc0(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main([], script_path=self.sync_py)
        self.assertEqual(rc, 0)
        self.assertIn("Usage: sync.py [--repo-root <dir>] <command>", buf.getvalue())

    def test_help_short_flag(self) -> None:
        for flag in ("-h", "--help"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([flag], script_path=self.sync_py)
            self.assertEqual(rc, 0, flag)
            self.assertIn("--check", buf.getvalue())

    def test_unknown_command_is_usage_error_on_stderr(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--bogus"], script_path=self.sync_py)
        self.assertEqual(rc, 2)
        self.assertIn("Unknown command: --bogus", err.getvalue())
        self.assertIn("Usage", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_check_dispatches(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--check"], script_path=self.sync_py)
        self.assertEqual(rc, 0)
        self.assertIn("All 1 managed file(s) are in sync.", buf.getvalue())

    def test_apply_force_flag(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=True, body="OLD\n")
        # No git repo → _git_tree_dirty returns False, so --force is redundant
        # but exercising the flag path here is the point.
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--apply", "--force"], script_path=self.sync_py)
        self.assertEqual(rc, 0)

    def test_status_dispatches(self) -> None:
        _seed_consumer(self.root, "comp/upgrade.py", header=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--status"], script_path=self.sync_py)
        self.assertEqual(rc, 0)
        self.assertIn("Managed upgrade.{sh,py} files: 1", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

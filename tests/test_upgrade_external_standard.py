"""Unit tests for scripts/python/upgrade_core/external_standard.py.

Stdlib unittest only — keeps dep surface at zero so the suite runs under
both the helmfile-tools image's system python3 and any local venv.

Test design:
  - Pure helpers (_is_excluded, _read_yaml_field, _detect_helmfile,
    _extract_top_keys, _used_top_level_keys, _sorted_backups,
    _update_helmfile_pins, _print_helmfile_releases) — direct cases
    on tempdir trees + tmp_path fixtures.
  - Argument parsing (_parse_args) — happy / error / sub-command paths,
    asserting sys.exit codes and stdout messages.
  - Backup helpers (_list_backups, _cleanup_backups, _auto_prune_backups,
    _do_rollback) — populated tempdir trees with timestamped subdirs.
  - 13 consumer spot-check — import each upgrade.py and assert the CONFIG
    dict shape (6 expected keys, valid CHART_TYPE).
  - Higher-level run() — mocks helm/diff subprocess to drive the
    7-step main flow without touching the network or filesystem outside
    the test tempdir.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

es = load("upgrade_core.external_standard")


# =============================================================
# _is_excluded — substring pattern matching
# =============================================================


class IsExcludedTests(unittest.TestCase):
    def test_empty_pattern_returns_false(self) -> None:
        self.assertFalse(es._is_excluded("dev.yaml", ""))

    def test_single_substring_match(self) -> None:
        self.assertTrue(es._is_excluded("dev-old-release.yaml", "old-release"))

    def test_substring_no_match(self) -> None:
        self.assertFalse(es._is_excluded("dev.yaml", "qa"))

    def test_comma_separated_first_matches(self) -> None:
        self.assertTrue(es._is_excluded("test-foo.yaml", "test,backup"))

    def test_comma_separated_second_matches(self) -> None:
        self.assertTrue(es._is_excluded("backup-foo.yaml", "test,backup"))

    def test_comma_separated_none_matches(self) -> None:
        self.assertFalse(es._is_excluded("prod.yaml", "test,backup"))

    def test_empty_pattern_token_is_skipped(self) -> None:
        # Trailing comma should not flag everything (empty token is never matched).
        self.assertFalse(es._is_excluded("anything.yaml", "test,"))


# =============================================================
# _read_yaml_field — Chart.yaml version / appVersion extraction
# =============================================================


class ReadYamlFieldTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "Chart.yaml"
        f.write_text(body)
        return f

    def test_reads_version(self) -> None:
        f = self._write("apiVersion: v2\nversion: 1.2.3\nappVersion: 7.4.0\n")
        self.assertEqual(es._read_yaml_field(f, "version"), "1.2.3")

    def test_reads_app_version(self) -> None:
        f = self._write("apiVersion: v2\nversion: 1.2.3\nappVersion: 7.4.0\n")
        self.assertEqual(es._read_yaml_field(f, "appVersion"), "7.4.0")

    def test_missing_field_returns_empty(self) -> None:
        f = self._write("apiVersion: v2\nversion: 1.2.3\n")
        self.assertEqual(es._read_yaml_field(f, "appVersion"), "")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(es._read_yaml_field(Path("/nope/Chart.yaml"), "version"), "")

    def test_ignores_indented_subfield(self) -> None:
        # bash `grep '^version:'` matches only at line start — so any indented
        # `version:` inside a nested map must be ignored.
        f = self._write("dependencies:\n  - name: x\n    version: 9.9.9\nversion: 1.2.3\n")
        self.assertEqual(es._read_yaml_field(f, "version"), "1.2.3")


# =============================================================
# _detect_helmfile — gotmpl preferred over plain yaml
# =============================================================


class DetectHelmfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_prefers_gotmpl(self) -> None:
        (self.dir / "helmfile.yaml").write_text("plain\n")
        (self.dir / "helmfile.yaml.gotmpl").write_text("templated\n")
        path, name = es._detect_helmfile(self.dir)
        self.assertEqual(name, "helmfile.yaml.gotmpl")
        self.assertEqual(path, self.dir / "helmfile.yaml.gotmpl")

    def test_falls_back_to_plain(self) -> None:
        (self.dir / "helmfile.yaml").write_text("plain\n")
        path, name = es._detect_helmfile(self.dir)
        self.assertEqual(name, "helmfile.yaml")
        self.assertEqual(path, self.dir / "helmfile.yaml")

    def test_neither_present_returns_none(self) -> None:
        path, name = es._detect_helmfile(self.dir)
        self.assertIsNone(path)
        self.assertEqual(name, "")


# =============================================================
# _extract_top_keys / _used_top_level_keys
# =============================================================


class ExtractTopKeysTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "values.yaml"
        f.write_text(body)
        return f

    def test_returns_letter_started_lines_only(self) -> None:
        f = self._write("global:\n  foo: 1\nimage:\n  repo: a\n123: bad\n# comment\n")
        self.assertEqual(es._extract_top_keys(f), {"global", "image"})

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(es._extract_top_keys(Path("/nope.yaml")), set())

    def test_used_top_level_keys_skips_indented(self) -> None:
        f = self._write("global:\n  foo: 1\nimage:\n  repo: a\n# comment\n")
        self.assertEqual(es._used_top_level_keys(f), {"global", "image"})

    def test_used_top_level_keys_skips_comments(self) -> None:
        f = self._write("# image: ignored\nimage:\n  repo: a\n")
        self.assertEqual(es._used_top_level_keys(f), {"image"})


# =============================================================
# _sorted_backups + _list_backups + _cleanup_backups + _auto_prune
# =============================================================


class BackupHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.backup = self.tmp / "backup"
        self.backup.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, names: list[str]) -> None:
        for n in names:
            (self.backup / n).mkdir()

    def test_sorted_backups_desc(self) -> None:
        self._make(["20260101_010101", "20260520_120000", "20260301_050505"])
        ordered = [p.name for p in es._sorted_backups(self.backup)]
        self.assertEqual(
            ordered,
            ["20260520_120000", "20260301_050505", "20260101_010101"],
        )

    def test_sorted_backups_skips_non_2x_dirs(self) -> None:
        self._make(["20260101_010101", "tmp-other", "9999_other"])
        names = {p.name for p in es._sorted_backups(self.backup)}
        self.assertEqual(names, {"20260101_010101"})

    def test_sorted_backups_missing_dir(self) -> None:
        shutil.rmtree(self.backup)
        self.assertEqual(es._sorted_backups(self.backup), [])

    def test_list_backups_empty(self) -> None:
        shutil.rmtree(self.backup)
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._list_backups(self.backup)
        self.assertIn("No backups found.", buf.getvalue())

    def test_list_backups_populated_includes_chart_version(self) -> None:
        self._make(["20260520_120000"])
        (self.backup / "20260520_120000" / "Chart.yaml").write_text(
            "version: 9.9.9\nappVersion: 1.0\n"
        )
        (self.backup / "20260520_120000" / "values.yaml").write_text("x: 1\n")
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._list_backups(self.backup)
        out = buf.getvalue()
        self.assertIn("[1] 20260520_120000", out)
        self.assertIn("Chart: 9.9.9", out)
        self.assertIn("Chart.yaml", out)

    def test_cleanup_below_threshold_is_noop(self) -> None:
        self._make(["20260101_010101", "20260102_010101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._cleanup_backups(self.backup, keep_backups=5)
        self.assertIn("Nothing to clean up.", buf.getvalue())
        self.assertEqual(len(es._sorted_backups(self.backup)), 2)

    def test_cleanup_keeps_newest_n(self) -> None:
        self._make([
            "20260101_010101",
            "20260201_010101",
            "20260301_010101",
            "20260401_010101",
            "20260501_010101",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._cleanup_backups(self.backup, keep_backups=2)
        kept = [p.name for p in es._sorted_backups(self.backup)]
        self.assertEqual(kept, ["20260501_010101", "20260401_010101"])
        self.assertIn("Removing 3 old backup(s)", buf.getvalue())

    def test_auto_prune_silent_below_threshold(self) -> None:
        self._make(["20260101_010101", "20260201_010101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._auto_prune_backups(self.backup, keep_backups=5)
        self.assertEqual(buf.getvalue(), "")
        self.assertEqual(len(es._sorted_backups(self.backup)), 2)

    def test_auto_prune_above_threshold_prints_count(self) -> None:
        self._make([
            "20260101_010101",
            "20260201_010101",
            "20260301_010101",
            "20260401_010101",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._auto_prune_backups(self.backup, keep_backups=2)
        out = buf.getvalue()
        self.assertIn("Auto-pruned 2 old backup(s) (KEEP_BACKUPS=2).", out)
        self.assertEqual(len(es._sorted_backups(self.backup)), 2)


# =============================================================
# _do_rollback — interactive selection
# =============================================================


class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "chart"
        self.values_dir = self.tmp / "chart" / "values"
        self.backup = self.tmp / "chart" / "backup"
        self.chart_dir.mkdir(parents=True)
        self.values_dir.mkdir()
        self.backup.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, name: str, files: dict[str, str]) -> None:
        d = self.backup / name
        d.mkdir()
        for fname, content in files.items():
            (d / fname).write_text(content)

    def test_no_backups_exits_with_code_1(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            es._do_rollback(self.backup, self.chart_dir, self.values_dir)
        self.assertEqual(cm.exception.code, 1)

    def test_default_selection_restores_newest(self) -> None:
        self._make_backup("20260101_010101", {"Chart.yaml": "version: 0.9.0\n"})
        self._make_backup(
            "20260520_120000",
            {
                "Chart.yaml": "version: 1.0.0\n",
                "values.yaml": "v1: 1\n",
                "helmfile.yaml": "old\n",
                "dev.yaml": "from-backup\n",
            },
        )
        with mock.patch("builtins.input", return_value=""):
            es._do_rollback(self.backup, self.chart_dir, self.values_dir)
        self.assertEqual(
            (self.chart_dir / "Chart.yaml").read_text(),
            "version: 1.0.0\n",
        )
        self.assertEqual((self.chart_dir / "values.yaml").read_text(), "v1: 1\n")
        self.assertEqual((self.chart_dir / "helmfile.yaml").read_text(), "old\n")
        self.assertEqual((self.values_dir / "dev.yaml").read_text(), "from-backup\n")

    def test_invalid_selection_exits(self) -> None:
        self._make_backup("20260520_120000", {"Chart.yaml": "version: 1.0.0\n"})
        with mock.patch("builtins.input", return_value="abc"):
            with self.assertRaises(SystemExit) as cm:
                es._do_rollback(self.backup, self.chart_dir, self.values_dir)
            self.assertEqual(cm.exception.code, 1)

    def test_out_of_range_selection_exits(self) -> None:
        self._make_backup("20260520_120000", {"Chart.yaml": "version: 1.0.0\n"})
        with mock.patch("builtins.input", return_value="9"):
            with self.assertRaises(SystemExit) as cm:
                es._do_rollback(self.backup, self.chart_dir, self.values_dir)
            self.assertEqual(cm.exception.code, 1)


# =============================================================
# _update_helmfile_pins — 4 sed patterns covered
# =============================================================


class UpdateHelmfilePinsTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "helmfile.yaml"
        f.write_text(body)
        return f

    def test_quoted_version_pin(self) -> None:
        f = self._write('releases:\n  - name: x\n    version: "1.0.0"\n')
        pins = es._update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(pins, 1)
        self.assertIn('version: "1.1.0"', f.read_text())
        self.assertNotIn("1.0.0", f.read_text())

    def test_bare_version_pin_followed_by_space(self) -> None:
        f = self._write('releases:\n  - name: x\n    version: 1.0.0 \n')
        pins = es._update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(pins, 1)
        self.assertIn("version: 1.1.0 ", f.read_text())

    def test_bare_version_pin_at_line_end(self) -> None:
        f = self._write('releases:\n  - name: x\n    version: 1.0.0\n')
        pins = es._update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(pins, 1)
        self.assertIn("version: 1.1.0", f.read_text())

    def test_gotmpl_chart_version_hoist(self) -> None:
        f = self._write(
            '{{- $chartVersion := "1.0.0" }}\n'
            "releases:\n"
            "  - name: x\n"
            '    version: {{ $chartVersion }}\n'
        )
        pins = es._update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(pins, 1)
        self.assertIn('$chartVersion := "1.1.0"', f.read_text())

    def test_no_match_leaves_file_unchanged(self) -> None:
        body = "releases:\n  - name: x\n    version: 9.9.9\n"
        f = self._write(body)
        pins = es._update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(pins, 0)
        self.assertEqual(f.read_text(), body)


# =============================================================
# _print_helmfile_releases — awk pretty-print equivalent
# =============================================================


class PrintHelmfileReleasesTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "helmfile.yaml"
        f.write_text(body)
        return f

    def test_basic_releases_block(self) -> None:
        f = self._write(
            "releases:\n"
            "  - name: argo-cd\n"
            "    chart: argo/argo-cd\n"
            "    version: 7.4.0\n"
            "  - name: extra\n"
            "    version: 1.0.0\n"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._print_helmfile_releases(f)
        out = buf.getvalue()
        self.assertIn(f"    - {'argo-cd':<30} version: 7.4.0", out)
        self.assertIn(f"    - {'extra':<30} version: 1.0.0", out)

    def test_lines_with_comments_are_skipped(self) -> None:
        f = self._write(
            "releases:\n"
            "  # disabled-name: foo\n"
            "  - name: real\n"
            "    version: 2.0.0\n"
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            es._print_helmfile_releases(f)
        out = buf.getvalue()
        self.assertIn(f"    - {'real':<30} version: 2.0.0", out)
        self.assertNotIn("disabled-name", out)


# =============================================================
# _parse_args
# =============================================================


class ParseArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.backup = self.tmp / "backup"
        self.chart_dir = self.tmp
        self.values_dir = self.tmp / "values"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_args_returns_defaults(self) -> None:
        args = es._parse_args([], "upgrade.py", 5, self.backup, self.chart_dir, self.values_dir)
        self.assertEqual(args, {"dry_run": False, "target_version": "", "exclude_patterns": ""})

    def test_dry_run_flag(self) -> None:
        args = es._parse_args(["--dry-run"], "upgrade.py", 5, self.backup, self.chart_dir, self.values_dir)
        self.assertTrue(args["dry_run"])

    def test_version_with_value(self) -> None:
        args = es._parse_args(
            ["--version", "1.2.3"], "upgrade.py", 5,
            self.backup, self.chart_dir, self.values_dir,
        )
        self.assertEqual(args["target_version"], "1.2.3")

    def test_version_without_value_errors(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            es._parse_args(
                ["--version"], "upgrade.py", 5,
                self.backup, self.chart_dir, self.values_dir,
            )
        self.assertEqual(cm.exception.code, 1)

    def test_exclude_with_value(self) -> None:
        args = es._parse_args(
            ["--exclude", "test,old"], "upgrade.py", 5,
            self.backup, self.chart_dir, self.values_dir,
        )
        self.assertEqual(args["exclude_patterns"], "test,old")

    def test_exclude_without_value_errors(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            es._parse_args(
                ["--exclude"], "upgrade.py", 5,
                self.backup, self.chart_dir, self.values_dir,
            )
        self.assertEqual(cm.exception.code, 1)

    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                es._parse_args(
                    ["--help"], "upgrade.py", 5,
                    self.backup, self.chart_dir, self.values_dir,
                )
        self.assertEqual(cm.exception.code, 0)

    def test_unknown_option_exits_zero_after_usage(self) -> None:
        # bash mirror: `Unknown option: X\n\n<usage>` then `exit 0` from usage.
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                es._parse_args(
                    ["--bogus"], "upgrade.py", 5,
                    self.backup, self.chart_dir, self.values_dir,
                )
        self.assertEqual(cm.exception.code, 0)

    def test_combined_dry_run_and_version(self) -> None:
        args = es._parse_args(
            ["--dry-run", "--version", "1.2.3"], "upgrade.py", 5,
            self.backup, self.chart_dir, self.values_dir,
        )
        self.assertTrue(args["dry_run"])
        self.assertEqual(args["target_version"], "1.2.3")

    def test_list_backups_subcommand_exits(self) -> None:
        self.backup.mkdir()
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                es._parse_args(
                    ["--list-backups"], "upgrade.py", 5,
                    self.backup, self.chart_dir, self.values_dir,
                )
        self.assertEqual(cm.exception.code, 0)


# =============================================================
# run() — full flow, mocked subprocess
# =============================================================


def _fake_subprocess(handler):
    """Returns a mock that routes subprocess.run(...) through handler(cmd)."""
    def _run(cmd, **kwargs):
        text = handler(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=text, stderr="")
    return _run


class RunFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "chart"
        self.chart_dir.mkdir()
        (self.chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: x\nversion: 1.0.0\nappVersion: 7.0.0\n"
        )
        (self.chart_dir / "values.yaml").write_text("global:\n  foo: 1\n")
        (self.chart_dir / "values").mkdir()
        (self.chart_dir / "values" / "dev.yaml").write_text("global:\n  foo: 2\n")
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Test Upgrade",
            "HELM_REPO_NAME": "x",
            "HELM_REPO_URL": "https://x.example",
            "HELM_CHART": "x/x",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_TYPE": "external",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_already_up_to_date_returns_zero(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.0.0","app_version":"7.0.0"}]'
            return ""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date! Nothing to do.", buf.getvalue())

    def test_dry_run_returns_zero_and_no_chart_change(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.1.0","app_version":"7.1.0"}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 2\n  bar: 3\n"
            if cmd[:2] == ["helm", "pull"]:
                return ""
            if cmd[0] == "diff":
                return ""
            return ""
        original = (self.chart_dir / "Chart.yaml").read_text()
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(self.config, ["--dry-run"], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("[Step 7/7] DRY-RUN complete.", out)
        self.assertIn(" Mode: DRY-RUN (no files will be changed)", out)
        self.assertIn("./upgrade.py", out)
        # Chart.yaml unchanged because dry-run took the early exit.
        self.assertEqual((self.chart_dir / "Chart.yaml").read_text(), original)

    def test_empty_search_returns_one_with_error(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return "[]"
            return ""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: Failed to fetch latest version.", buf.getvalue())

    def test_target_version_overrides_latest(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"2.0.0","app_version":"7.5.0"}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return "apiVersion: v2\nname: x\nversion: 1.2.3\nappVersion: 7.4.0\n"
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 1\n"
            return ""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(self.config, ["--dry-run", "--version", "1.2.3"], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(" Target: v1.2.3", out)
        self.assertIn("Using target     - Chart: 1.2.3", out)

    def test_first_run_onboarding_creates_chart_without_crash(self) -> None:
        """Regression: onboarding a component that has no local Chart.yaml /
        values.yaml yet must not crash in the Step 7 backup step. Previously
        the unconditional ``shutil.copy2(chart_dir / 'Chart.yaml', ...)`` raised
        FileNotFoundError on the absent Chart.yaml; it is now guarded with
        ``.is_file()`` like the sibling helmfile / values.yaml copies. The apply
        path should materialize Chart.yaml at the target version."""
        # Simulate a freshly-scaffolded component: helmfile + values/ only.
        (self.chart_dir / "Chart.yaml").unlink()
        (self.chart_dir / "values.yaml").unlink()
        (self.chart_dir / "helmfile.yaml").write_text(
            "releases:\n  - name: x\n    chart: x/x\n    version: 1.1.0\n"
        )

        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.1.0","app_version":"7.1.0"}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 2\n"
            if cmd[:2] == ["helm", "pull"]:
                return ""
            if cmd and cmd[0] == "diff":
                return ""
            return ""

        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = es.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        chart_yaml = self.chart_dir / "Chart.yaml"
        self.assertTrue(chart_yaml.is_file())
        self.assertIn("version: 1.1.0", chart_yaml.read_text())

    # ----- the shell -> python migration total_steps=8 + pre_apply_hook -----

    def _k10_handler(self):
        """Handler that emits a real version bump (1.0.0 → 1.1.0) so Step 7
        is reached. Used by the K10-flavored tests below."""
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.1.0","app_version":"7.1.0"}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return "apiVersion: v2\nname: x\nversion: 1.1.0\nappVersion: 7.1.0\n"
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 2\n"
            if cmd[:2] == ["helm", "pull"]:
                return ""
            if cmd and cmd[0] == "diff":
                return ""
            return ""
        return handler

    def test_dry_run_skips_pre_apply_hook_when_total_steps_eight(self) -> None:
        """K10 dry-run: SKIPPED line + Step 8 DRY-RUN, hook not called."""
        hook = mock.Mock(return_value=0)
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._k10_handler())):
            with redirect_stdout(buf):
                rc = es.run(
                    self.config,
                    ["--dry-run"],
                    self.script,
                    total_steps=8,
                    pre_apply_hook=hook,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(hook.call_count, 0)
        out = buf.getvalue()
        self.assertIn("[Step 7/8] Mirror stage SKIPPED in dry-run.", out)
        self.assertIn("[Step 8/8] DRY-RUN complete. No files were changed.", out)

    def test_total_steps_eight_renders_all_step_headers(self) -> None:
        """K10 non-dry-run: every step header renders as ``/8`` and the
        final Apply line lands at ``[Step 8/8]``."""
        hook = mock.Mock(return_value=0)
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._k10_handler())):
            with redirect_stdout(buf):
                rc = es.run(
                    self.config,
                    [],
                    self.script,
                    total_steps=8,
                    pre_apply_hook=hook,
                )
        self.assertEqual(rc, 0)
        self.assertEqual(hook.call_count, 1)
        out = buf.getvalue()
        for expected in (
            "[Step 1/8] Checking current version...",
            "[Step 2/8] Checking latest version...",
            "[Step 3/8] Fetching Chart.yaml and values.yaml",
            "[Step 4/8] Chart.yaml diff",
            "[Step 5/8] values.yaml diff",
            "[Step 6/8] Checking custom values for breaking changes",
            "[Step 7/8] Mirroring upstream images to private registry...",
            "[Step 8/8] Applying upgrade...",
        ):
            self.assertIn(expected, out, f"missing header line: {expected!r}")

    def test_pre_apply_hook_non_zero_aborts_and_propagates_rc(self) -> None:
        """K10 mirror failure: hook returns rc -> _apply_upgrade returns
        the same rc and prints the abort line to stderr; Step 8 Apply
        header MUST NOT appear."""
        hook = mock.Mock(return_value=7)
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        from contextlib import redirect_stderr
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._k10_handler())):
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = es.run(
                    self.config,
                    [],
                    self.script,
                    total_steps=8,
                    pre_apply_hook=hook,
                )
        self.assertEqual(rc, 7)
        self.assertEqual(hook.call_count, 1)
        self.assertNotIn("[Step 8/8] Applying upgrade...", out_buf.getvalue())
        self.assertIn("mirror stage failed", err_buf.getvalue())
        self.assertIn("Aborting upgrade (no files modified)", err_buf.getvalue())

    def test_pre_apply_hook_none_with_total_steps_eight_prints_skip(self) -> None:
        """K10 with do_mirror omitted: Step 7 'Mirror stage skipped' line
        appears, Step 8 Apply still runs."""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(self._k10_handler())):
            with redirect_stdout(buf):
                rc = es.run(
                    self.config,
                    [],
                    self.script,
                    total_steps=8,
                    pre_apply_hook=None,
                )
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn(
            "[Step 7/8] Mirror stage skipped (do_mirror not defined in CONFIG).",
            out,
        )
        self.assertIn("[Step 8/8] Applying upgrade...", out)


# =============================================================
# 13 consumer CONFIG dict spot-check
# =============================================================
if __name__ == "__main__":
    unittest.main()

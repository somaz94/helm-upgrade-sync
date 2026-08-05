"""Unit tests for upgrade_core/_common_helmfile.py.

Covers the 11 helmfile-flavored helpers extracted in the shell -> python migration
The sibling modules now import these via alias, and ``external-oci``
relies on the extraction to keep its hook overrides thin.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ch = load("upgrade_core._common_helmfile")


# =============================================================
# read_yaml_field
# =============================================================


class ReadYamlFieldTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "Chart.yaml"
        f.write_text(body)
        return f

    def test_finds_top_level_version(self) -> None:
        f = self._write("apiVersion: v2\nname: x\nversion: 1.2.3\nappVersion: 7.0.0\n")
        self.assertEqual(ch.read_yaml_field(f, "version"), "1.2.3")
        self.assertEqual(ch.read_yaml_field(f, "appVersion"), "7.0.0")

    def test_missing_key_returns_empty(self) -> None:
        f = self._write("apiVersion: v2\nname: x\n")
        self.assertEqual(ch.read_yaml_field(f, "version"), "")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(ch.read_yaml_field(Path("/nonexistent.yaml"), "version"), "")


# =============================================================
# detect_helmfile
# =============================================================


class DetectHelmfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prefers_gotmpl(self) -> None:
        (self.tmp / "helmfile.yaml").write_text("")
        (self.tmp / "helmfile.yaml.gotmpl").write_text("")
        path, name = ch.detect_helmfile(self.tmp)
        self.assertEqual(name, "helmfile.yaml.gotmpl")

    def test_plain_yaml_when_no_gotmpl(self) -> None:
        (self.tmp / "helmfile.yaml").write_text("")
        path, name = ch.detect_helmfile(self.tmp)
        self.assertEqual(name, "helmfile.yaml")

    def test_returns_none_when_absent(self) -> None:
        path, name = ch.detect_helmfile(self.tmp)
        self.assertIsNone(path)
        self.assertEqual(name, "")


# =============================================================
# print_helmfile_releases
# =============================================================


class PrintHelmfileReleasesTests(unittest.TestCase):
    def test_multiline_release_block(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "helmfile.yaml"
            f.write_text(
                "repositories: []\n"
                "releases:\n"
                "  - name: foo\n"
                "    chart: oci://example/foo\n"
                "    version: 1.0.0\n"
                "  - name: bar\n"
                "    chart: oci://example/bar\n"
                "    version: 2.5.0\n"
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                ch.print_helmfile_releases(f)
            out = buf.getvalue()
            self.assertIn("foo", out)
            self.assertIn("1.0.0", out)
            self.assertIn("bar", out)
            self.assertIn("2.5.0", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================
# extract_top_keys + used_top_level_keys
# =============================================================


class TopKeysTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "values.yaml"
        f.write_text(body)
        return f

    def test_extract_skips_comments_and_indents(self) -> None:
        f = self._write("# comment\nglobal:\n  foo: 1\nimage:\n  tag: v1\n")
        keys = ch.extract_top_keys(f)
        self.assertIn("global", keys)
        self.assertIn("image", keys)
        self.assertNotIn("foo", keys)
        self.assertNotIn("tag", keys)

    def test_used_top_level_keys_filters_indents(self) -> None:
        f = self._write("# header\nglobal:\n  foo: 1\nimage: x\n")
        used = ch.used_top_level_keys(f)
        self.assertIn("global", used)
        self.assertIn("image", used)
        self.assertNotIn("foo", used)


# =============================================================
# update_helmfile_pins (baseline, no scope)
# =============================================================


class UpdateHelmfilePinsTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "helmfile.yaml"
        f.write_text(body)
        return f

    def test_replaces_quoted_version(self) -> None:
        f = self._write('releases:\n  - name: x\n    version: "1.0.0"\n')
        n = ch.update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 1)
        self.assertIn('version: "1.1.0"', f.read_text())

    def test_replaces_bare_version(self) -> None:
        f = self._write("releases:\n  - name: x\n    version: 1.0.0\n")
        n = ch.update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 1)
        self.assertIn("version: 1.1.0\n", f.read_text())

    def test_replaces_gotmpl_chartversion_hoist(self) -> None:
        f = self._write('{{- $chartVersion := "1.0.0" }}\n')
        n = ch.update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 1)
        self.assertIn('$chartVersion := "1.1.0"', f.read_text())

    def test_no_match_returns_zero(self) -> None:
        f = self._write('releases:\n  - name: x\n    version: "9.9.9"\n')
        n = ch.update_helmfile_pins(f, "1.0.0", "1.1.0")
        self.assertEqual(n, 0)
        self.assertIn('"9.9.9"', f.read_text())


# =============================================================
# Subprocess wrappers — light smoke (no actual helm/diff binary needed
# for these tests; we just verify the function exists + returns a
# CompletedProcess).
# =============================================================


class SubprocessWrapperTests(unittest.TestCase):
    def test_run_subprocess_returns_completed_process(self) -> None:
        # `true` exits 0 with empty stdout on POSIX.
        proc = ch.run_subprocess(["true"])
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_run_subprocess_tolerates_failure(self) -> None:
        # `false` exits 1 — wrapper uses check=False so no exception.
        proc = ch.run_subprocess(["false"])
        self.assertEqual(proc.returncode, 1)

    def test_diff_returns_empty_when_files_match(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            a = tmp / "a"
            b = tmp / "b"
            a.write_text("hello\n")
            b.write_text("hello\n")
            self.assertEqual(ch.diff(a, b), "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =============================================================
# list_backups (chart-flavored)
# =============================================================


class ListBackupsTests(unittest.TestCase):
    def test_lists_chart_version_from_backup(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            backup_dir = tmp / "backup"
            backup_dir.mkdir()
            b = backup_dir / "20260520_120000"
            b.mkdir()
            (b / "Chart.yaml").write_text("version: 1.2.3\nname: x\n")
            buf = io.StringIO()
            with redirect_stdout(buf):
                ch.list_backups(backup_dir)
            out = buf.getvalue()
            self.assertIn("[1] 20260520_120000", out)
            self.assertIn("Chart: 1.2.3", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_backups_message(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        try:
            backup_dir = tmp / "backup"
            backup_dir.mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                ch.list_backups(backup_dir)
            self.assertIn("No backups found.", buf.getvalue())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

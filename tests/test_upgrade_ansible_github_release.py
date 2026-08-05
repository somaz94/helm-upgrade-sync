"""Unit tests for upgrade_core/ansible_github_release.py.

Stdlib unittest only — keeps dep surface at zero so the suite runs under
both the helmfile-tools image's system python3 and any local venv.

Test design (~30 cases):
  - YAML helpers (_read_yaml_value, _update_yaml_value) — quoted / unquoted
    / inline-comment variants.
  - Backup helpers (_sorted_backups, _list_backups, _cleanup_backups,
    _auto_prune_backups, _do_rollback) — tempdir trees with timestamped
    subdirs.
  - GitHub Releases fetch (_fetch_github_ga_versions, _fetch_latest_version) —
    mocked urlopen returning canned JSON; covers release / prerelease /
    draft / non-semver / MAJOR_PIN filtering.
  - Argument parsing (_parse_args) — happy + error + sub-command paths.
  - node-exporter consumer spot-check — CONFIG dict shape + value types.
  - Higher-level run() — mocked urlopen to drive the 5-step main flow.
"""

from __future__ import annotations

import importlib.util
import io
import json
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ag = load("upgrade_core.ansible_github_release")


# =============================================================
# _read_yaml_value — quoted / unquoted / inline-comment
# =============================================================


class ReadYamlValueTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "all.yml"
        f.write_text(body, encoding="utf-8")
        return f

    def test_double_quoted(self) -> None:
        f = self._write('node_exporter_version: "1.8.2"\n')
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "1.8.2")

    def test_single_quoted(self) -> None:
        f = self._write("node_exporter_version: '1.8.2'\n")
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "1.8.2")

    def test_bare(self) -> None:
        f = self._write("node_exporter_version: 1.8.2\n")
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "1.8.2")

    def test_with_inline_comment(self) -> None:
        f = self._write("node_exporter_version: \"1.8.2\"  # bump 2026-05\n")
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "1.8.2")

    def test_missing_key_returns_empty(self) -> None:
        f = self._write("foo: bar\n")
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "")

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(
            ag._read_yaml_value(Path("/nope/all.yml"), "node_exporter_version"),
            "",
        )

    def test_indented_subkey_is_ignored(self) -> None:
        # bash awk matches only line-start, not indented children.
        f = self._write(
            "deps:\n  - name: x\n    node_exporter_version: 9.9.9\n"
            "node_exporter_version: 1.8.2\n"
        )
        self.assertEqual(ag._read_yaml_value(f, "node_exporter_version"), "1.8.2")


# =============================================================
# _update_yaml_value — 3 quote branches
# =============================================================


class UpdateYamlValueTests(unittest.TestCase):
    def _write(self, body: str) -> Path:
        tmp = Path(tempfile.mkdtemp())
        f = tmp / "all.yml"
        f.write_text(body, encoding="utf-8")
        return f

    def test_double_quoted_replacement_preserves_quotes(self) -> None:
        f = self._write('node_exporter_version: "1.8.2"\n')
        ag._update_yaml_value(f, "node_exporter_version", "1.9.0")
        self.assertEqual(f.read_text(), 'node_exporter_version: "1.9.0"\n')

    def test_single_quoted_replacement_preserves_quotes(self) -> None:
        f = self._write("node_exporter_version: '1.8.2'\n")
        ag._update_yaml_value(f, "node_exporter_version", "1.9.0")
        self.assertEqual(f.read_text(), "node_exporter_version: '1.9.0'\n")

    def test_bare_replacement_stays_bare(self) -> None:
        f = self._write("node_exporter_version: 1.8.2\n")
        ag._update_yaml_value(f, "node_exporter_version", "1.9.0")
        self.assertEqual(f.read_text(), "node_exporter_version: 1.9.0\n")

    def test_only_first_match_replaced(self) -> None:
        # bash uses `done = 1; next` after the first hit.
        body = (
            'node_exporter_version: "1.8.2"\n'
            "other: 2\n"
            'node_exporter_version: "9.9.9"\n'
        )
        f = self._write(body)
        ag._update_yaml_value(f, "node_exporter_version", "1.9.0")
        out = f.read_text()
        self.assertIn('node_exporter_version: "1.9.0"', out)
        # Second occurrence intentionally left alone.
        self.assertIn('node_exporter_version: "9.9.9"', out)

    def test_missing_file_is_noop(self) -> None:
        nope = Path("/nope/all.yml")
        ag._update_yaml_value(nope, "node_exporter_version", "1.9.0")
        # No exception, no file created.
        self.assertFalse(nope.exists())


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
        names = [p.name for p in ag._sorted_backups(self.backup)]
        self.assertEqual(
            names,
            ["20260520_120000", "20260301_050505", "20260101_010101"],
        )

    def test_list_backups_empty(self) -> None:
        shutil.rmtree(self.backup)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._list_backups(self.backup, "ansible/group_vars/all.yml", "node_exporter_version")
        self.assertIn("No backups found.", buf.getvalue())

    def test_list_backups_reads_version_key(self) -> None:
        self._make(["20260520_120000"])
        (self.backup / "20260520_120000" / "all.yml").write_text(
            'node_exporter_version: "1.8.2"\n'
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._list_backups(
                self.backup, "ansible/group_vars/all.yml", "node_exporter_version",
            )
        out = buf.getvalue()
        self.assertIn("[1] 20260520_120000", out)
        self.assertIn("version: 1.8.2", out)

    def test_cleanup_below_threshold_is_noop(self) -> None:
        self._make(["20260101_010101", "20260102_010101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._cleanup_backups(self.backup, keep_backups=5)
        self.assertIn("Nothing to clean up.", buf.getvalue())
        self.assertEqual(len(ag._sorted_backups(self.backup)), 2)

    def test_cleanup_keeps_newest_n(self) -> None:
        self._make([
            "20260101_010101", "20260201_010101", "20260301_010101",
            "20260401_010101", "20260501_010101",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._cleanup_backups(self.backup, keep_backups=2)
        kept = [p.name for p in ag._sorted_backups(self.backup)]
        self.assertEqual(kept, ["20260501_010101", "20260401_010101"])

    def test_auto_prune_silent_below_threshold(self) -> None:
        self._make(["20260101_010101", "20260201_010101"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._auto_prune_backups(self.backup, keep_backups=5)
        self.assertEqual(buf.getvalue(), "")

    def test_auto_prune_above_threshold_prints_count(self) -> None:
        self._make([
            "20260101_010101", "20260201_010101",
            "20260301_010101", "20260401_010101",
        ])
        buf = io.StringIO()
        with redirect_stdout(buf):
            ag._auto_prune_backups(self.backup, keep_backups=2)
        self.assertIn("Auto-pruned 2 old backup(s) (KEEP_BACKUPS=2).", buf.getvalue())


# =============================================================
# _do_rollback — interactive restore
# =============================================================


class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp
        self.backup = self.chart_dir / "backup"
        self.backup.mkdir()
        (self.chart_dir / "ansible" / "group_vars").mkdir(parents=True)
        (self.chart_dir / "ansible" / "group_vars" / "all.yml").write_text(
            'node_exporter_version: "1.7.0"\n'
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_backup(self, name: str, body: str) -> None:
        d = self.backup / name
        d.mkdir()
        (d / "all.yml").write_text(body)

    def test_no_backups_exits_with_code_1(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            ag._do_rollback(
                self.backup, self.chart_dir,
                "ansible/group_vars/all.yml", "node_exporter_version",
                "ansible", "inventory.ini", "upgrade.yml",
            )
        self.assertEqual(cm.exception.code, 1)

    def test_default_selection_restores_newest(self) -> None:
        self._make_backup("20260101_010101", 'node_exporter_version: "1.5.0"\n')
        self._make_backup("20260520_120000", 'node_exporter_version: "1.6.0"\n')
        with mock.patch("builtins.input", return_value=""):
            ag._do_rollback(
                self.backup, self.chart_dir,
                "ansible/group_vars/all.yml", "node_exporter_version",
                "ansible", "inventory.ini", "upgrade.yml",
            )
        restored = (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text()
        self.assertEqual(restored, 'node_exporter_version: "1.6.0"\n')


# =============================================================
# _fetch_github_ga_versions — mocked urlopen
# =============================================================


def _fake_response(payload):
    raw = json.dumps(payload).encode("utf-8")
    obj = mock.MagicMock()
    obj.read.return_value = raw
    obj.__enter__.return_value = obj
    obj.__exit__.return_value = False
    return obj


class FetchGithubGaVersionsTests(unittest.TestCase):
    def test_returns_sorted_ga_only(self) -> None:
        payload = [
            {"tag_name": "v1.8.2", "prerelease": False, "draft": False},
            {"tag_name": "v1.9.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.8.0-rc.1", "prerelease": True, "draft": False},
            {"tag_name": "v1.10.0-pre", "prerelease": False, "draft": True},
            {"tag_name": "v1.7.0", "prerelease": False, "draft": False},
        ]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            versions = ag._fetch_github_ga_versions("prometheus/node_exporter", "")
        self.assertEqual(versions, ["1.9.0", "1.8.2", "1.7.0"])

    def test_major_pin_filter(self) -> None:
        payload = [
            {"tag_name": "v2.0.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.9.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.8.2", "prerelease": False, "draft": False},
        ]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            versions = ag._fetch_github_ga_versions("prometheus/node_exporter", "1")
        self.assertEqual(versions, ["1.9.0", "1.8.2"])

    def test_non_semver_tags_filtered(self) -> None:
        payload = [
            {"tag_name": "release-2024-01", "prerelease": False, "draft": False},
            {"tag_name": "v1.8.2", "prerelease": False, "draft": False},
        ]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            versions = ag._fetch_github_ga_versions("prometheus/node_exporter", "")
        self.assertEqual(versions, ["1.8.2"])

    def test_empty_repo_returns_empty(self) -> None:
        self.assertEqual(ag._fetch_github_ga_versions("", ""), [])

    def test_network_error_returns_empty(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=ConnectionError("net down"),
        ):
            self.assertEqual(
                ag._fetch_github_ga_versions("prometheus/node_exporter", ""), [],
            )

    def test_invalid_json_returns_empty(self) -> None:
        obj = mock.MagicMock()
        obj.read.return_value = b"<<not json>>"
        obj.__enter__.return_value = obj
        obj.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=obj):
            self.assertEqual(
                ag._fetch_github_ga_versions("prometheus/node_exporter", ""), [],
            )

    def test_fetch_latest_version_wrapper(self) -> None:
        payload = [
            {"tag_name": "v1.9.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.8.2", "prerelease": False, "draft": False},
        ]
        with mock.patch("urllib.request.urlopen", return_value=_fake_response(payload)):
            self.assertEqual(
                ag._fetch_latest_version("prometheus/node_exporter", ""), "1.9.0",
            )


# =============================================================
# _parse_args
# =============================================================


class ParseArgsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.kwargs = {
            "prog": "upgrade.py",
            "keep_backups": 5,
            "backup_dir": self.tmp / "backup",
            "chart_dir": self.tmp,
            "version_file": "ansible/group_vars/all.yml",
            "version_key": "node_exporter_version",
            "ansible_dir": "ansible",
            "ansible_inventory": "inventory.ini",
            "ansible_upgrade_playbook": "upgrade.yml",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_args(self) -> None:
        out = ag._parse_args([], **self.kwargs)
        self.assertEqual(out, {"dry_run": False, "target_version": ""})

    def test_dry_run(self) -> None:
        out = ag._parse_args(["--dry-run"], **self.kwargs)
        self.assertTrue(out["dry_run"])

    def test_version_happy(self) -> None:
        out = ag._parse_args(["--version", "1.9.0"], **self.kwargs)
        self.assertEqual(out["target_version"], "1.9.0")

    def test_version_missing_errors(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            ag._parse_args(["--version"], **self.kwargs)
        self.assertEqual(cm.exception.code, 1)

    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                ag._parse_args(["--help"], **self.kwargs)
        self.assertEqual(cm.exception.code, 0)

    def test_unknown_option_exits_zero_after_usage(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            with redirect_stdout(io.StringIO()):
                ag._parse_args(["--bogus"], **self.kwargs)
        self.assertEqual(cm.exception.code, 0)


# =============================================================
# Consumer spot-check (node-exporter)
# =============================================================
# =============================================================
# run() — full flow, mocked urlopen
# =============================================================


class RunFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "chart"
        self.chart_dir.mkdir()
        (self.chart_dir / "ansible" / "group_vars").mkdir(parents=True)
        (self.chart_dir / "ansible" / "group_vars" / "all.yml").write_text(
            'node_exporter_version: "1.7.0"\n'
        )
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Node Exporter Test",
            "COMPONENT_NAME": "node_exporter",
            "GITHUB_REPO": "prometheus/node_exporter",
            "VERSION_FILE": "ansible/group_vars/all.yml",
            "VERSION_KEY": "node_exporter_version",
            "ANSIBLE_DIR": "ansible",
            "ANSIBLE_INVENTORY": "inventory.ini",
            "ANSIBLE_UPGRADE_PLAYBOOK": "upgrade.yml",
            "CHANGELOG_URL": "https://github.com/prometheus/node_exporter/releases",
            "MAJOR_PIN": "",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mock_response(self, payload):
        return _fake_response(payload)

    def test_already_up_to_date(self) -> None:
        payload = [{"tag_name": "v1.7.0", "prerelease": False, "draft": False}]
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with redirect_stdout(buf):
                rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date! Nothing to do.", buf.getvalue())

    def test_dry_run_does_not_modify_yaml(self) -> None:
        payload = [{"tag_name": "v1.9.0", "prerelease": False, "draft": False}]
        original = (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text()
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with redirect_stdout(buf):
                rc = ag.run(self.config, ["--dry-run"], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("[Step 4/5] DRY-RUN complete.", out)
        self.assertIn("./upgrade.py", out)
        self.assertEqual(
            (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text(),
            original,
        )

    def test_apply_writes_new_version_and_backup(self) -> None:
        payload = [{"tag_name": "v1.9.0", "prerelease": False, "draft": False}]
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with redirect_stdout(buf):
                rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        # YAML was rewritten preserving quotes
        self.assertEqual(
            (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text(),
            'node_exporter_version: "1.9.0"\n',
        )
        # Backup directory created with the original value
        backups = list((self.chart_dir / "backup").glob("2*/all.yml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), 'node_exporter_version: "1.7.0"\n')

    def test_major_bump_dry_run_skips_confirm(self) -> None:
        payload = [{"tag_name": "v2.0.0", "prerelease": False, "draft": False}]
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with redirect_stdout(buf):
                rc = ag.run(self.config, ["--dry-run"], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("MAJOR VERSION BUMP: 1.x -> 2.x", out)
        # No input prompt under dry-run.
        self.assertIn("[Step 4/5] DRY-RUN complete.", out)

    def test_major_bump_apply_y_confirms(self) -> None:
        payload = [{"tag_name": "v2.0.0", "prerelease": False, "draft": False}]
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with mock.patch("builtins.input", return_value="y"):
                with redirect_stdout(buf):
                    rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        self.assertEqual(
            (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text(),
            'node_exporter_version: "2.0.0"\n',
        )

    def test_major_bump_apply_n_aborts(self) -> None:
        payload = [{"tag_name": "v2.0.0", "prerelease": False, "draft": False}]
        original = (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text()
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen", return_value=self._mock_response(payload)):
            with mock.patch("builtins.input", return_value="n"):
                with redirect_stdout(buf):
                    rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("Aborted.", buf.getvalue())
        # YAML unchanged.
        self.assertEqual(
            (self.chart_dir / "ansible" / "group_vars" / "all.yml").read_text(),
            original,
        )

    def test_target_version_overrides_latest(self) -> None:
        # urlopen not even called when --version is given.
        buf = io.StringIO()
        with mock.patch("urllib.request.urlopen") as mocked:
            with redirect_stdout(buf):
                rc = ag.run(self.config, ["--dry-run", "--version", "1.8.0"], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("Using explicit target: 1.8.0", buf.getvalue())
        mocked.assert_not_called()

    def test_missing_version_file_errors(self) -> None:
        (self.chart_dir / "ansible" / "group_vars" / "all.yml").unlink()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: version file not found", buf.getvalue())

    def test_fetch_returns_empty_errors(self) -> None:
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=ConnectionError("net"),
        ):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = ag.run(self.config, [], self.script)
        self.assertEqual(rc, 1)
        self.assertIn("ERROR: failed to fetch latest version from GitHub.", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

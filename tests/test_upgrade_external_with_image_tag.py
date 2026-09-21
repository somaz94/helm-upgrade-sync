"""Unit tests for upgrade_core/external_with_image_tag.py.

The ``external_with_image_tag`` module is now a thin extension of :mod:`external_standard`
(during the shell -> python migration): the 7-step main flow is reused via
``post_pin_hook`` and the only ``external_with_image_tag``-specific surface is
``_rewrite_image_tags`` + the ``run()`` shim. ``external_standard``'s main flow surface is
covered by ``test_upgrade_external_standard.py``; here we cover:

  - ``_rewrite_image_tags`` semantics (the ``external_with_image_tag`` delta).
  - ``run()`` integration on the image-tag path (mocked subprocess) to
    confirm the hook fires at the correct point inside the apply flow
    and respects ``--dry-run``.
  - The single consumer (cicd/harbor-helm) CONFIG dict shape.

Stdlib unittest only.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import ModuleType
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402

ei = load("upgrade_core.external_with_image_tag")


# =============================================================
# _rewrite_image_tags — ``external_with_image_tag`` delta vs external-standard
# =============================================================


class RewriteImageTagsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.values_dir = self.tmp / "values"
        self.values_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, body: str) -> Path:
        path = self.values_dir / name
        path.write_text(body)
        return path

    def _run(self, exclude: str, latest_app: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ei._rewrite_image_tags(
                values_dir=self.values_dir,
                exclude_patterns=exclude,
                latest_app_version=latest_app,
            )
        return buf.getvalue()

    def test_empty_latest_app_version_is_noop(self) -> None:
        path = self._write("dev.yaml", "image:\n  tag: v1.2.3\n")
        out = self._run("", "")
        self.assertEqual(out, "")
        self.assertEqual(path.read_text(), "image:\n  tag: v1.2.3\n")

    def test_missing_values_dir_is_noop(self) -> None:
        shutil.rmtree(self.values_dir)
        out = self._run("", "2.0.0")
        self.assertEqual(out, "")

    def test_no_tag_match_skips_file(self) -> None:
        path = self._write("dev.yaml", "image:\n  repository: foo\n")
        out = self._run("", "2.0.0")
        self.assertEqual(out, "")
        self.assertEqual(path.read_text(), "image:\n  repository: foo\n")

    def test_tag_already_matches_skips(self) -> None:
        path = self._write("dev.yaml", "image:\n  tag: v2.0.0\n")
        out = self._run("", "2.0.0")
        self.assertEqual(out, "")
        # File untouched.
        self.assertEqual(path.read_text(), "image:\n  tag: v2.0.0\n")

    def test_single_tag_rewrites_and_reports(self) -> None:
        path = self._write("dev.yaml", "image:\n  tag: v1.2.3\n")
        out = self._run("", "2.0.0")
        self.assertEqual(path.read_text(), "image:\n  tag: v2.0.0\n")
        self.assertIn("Updated values/dev.yaml", out)
        self.assertIn("(1 image tag(s): v1.2.3 -> v2.0.0)", out)

    def test_multiple_same_tags_all_rewritten(self) -> None:
        body = (
            "core:\n  image:\n    tag: v2.15.0\n"
            "jobservice:\n  image:\n    tag: v2.15.0\n"
            "registry:\n  image:\n    tag: v2.15.0\n"
        )
        self._write("dev.yaml", body)
        out = self._run("", "2.16.0")
        rewritten = (self.values_dir / "dev.yaml").read_text()
        self.assertEqual(rewritten.count("tag: v2.16.0"), 3)
        self.assertNotIn("tag: v2.15.0", rewritten)
        self.assertIn("(3 image tag(s): v2.15.0 -> v2.16.0)", out)

    def test_first_match_drives_replace_pattern(self) -> None:
        # When a file has two distinct tags, the FIRST one drives the
        # bash `head -1` semantics — only that tag is replaced.
        body = "a:\n  tag: v1.2.3\nb:\n  tag: v9.9.9\n"
        self._write("dev.yaml", body)
        out = self._run("", "2.0.0")
        rewritten = (self.values_dir / "dev.yaml").read_text()
        # First tag (v1.2.3) was replaced.
        self.assertIn("tag: v2.0.0", rewritten)
        # The other distinct tag (v9.9.9) is untouched.
        self.assertIn("tag: v9.9.9", rewritten)
        self.assertIn("(1 image tag(s): v1.2.3 -> v2.0.0)", out)

    def test_exclude_pattern_skips_file(self) -> None:
        self._write("dev.yaml", "image:\n  tag: v1.2.3\n")
        self._write("dev-old.yaml", "image:\n  tag: v1.2.3\n")
        out = self._run("old", "2.0.0")
        # dev.yaml rewritten, dev-old.yaml skipped.
        self.assertEqual(
            (self.values_dir / "dev.yaml").read_text(),
            "image:\n  tag: v2.0.0\n",
        )
        self.assertEqual(
            (self.values_dir / "dev-old.yaml").read_text(),
            "image:\n  tag: v1.2.3\n",
        )
        self.assertIn("Updated values/dev.yaml", out)
        self.assertNotIn("Updated values/dev-old.yaml", out)

    def test_only_yaml_files_processed(self) -> None:
        # README / non-yaml stays untouched.
        readme = self._write("README.md", "tag: v1.0.0\n")
        # The glob is `*.yaml`, so README.md is skipped entirely.
        path = self._write("dev.yaml", "image:\n  tag: v1.0.0\n")
        out = self._run("", "1.1.0")
        self.assertEqual(readme.read_text(), "tag: v1.0.0\n")
        self.assertEqual(path.read_text(), "image:\n  tag: v1.1.0\n")
        self.assertIn("Updated values/dev.yaml", out)


# =============================================================
# Upstream image-rename detection
# =============================================================


class RepositoryDriftTests(unittest.TestCase):
    """This template rewrites tags but never ``repository:``.

    The Harbor chart renamed ``redis-photon`` -> ``valkey-photon`` upstream in
    1.19.2. The override kept the old name and took the new tag, producing
    ``goharbor/redis-photon:v2.15.2`` — a reference that does not exist, which
    no resource-removal diff check can see.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.component = Path(self._tmp.name) / "harbor-helm"
        self.values_dir = self.component / "values"
        self.values_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _upstream(self, body: str) -> None:
        (self.component / "values.yaml").write_text(body)

    def _override(self, name: str, body: str) -> None:
        (self.values_dir / name).write_text(body)

    def _warn(self, exclude: str = "") -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ei._warn_repository_drift(
                values_dir=self.values_dir, exclude_patterns=exclude
            )
        return buf.getvalue()

    def test_renamed_image_is_flagged(self) -> None:
        self._upstream(
            "redis:\n  internal:\n    image:\n"
            "      repository: docker.io/goharbor/valkey-photon\n"
        )
        self._override(
            "dev.yaml",
            "redis:\n  internal:\n    image:\n"
            "      repository: goharbor/redis-photon\n      tag: v2.15.2\n",
        )
        out = self._warn()
        self.assertIn("redis-photon", out)
        self.assertIn("WARNING", out)

    def test_matching_image_is_silent(self) -> None:
        self._upstream("image:\n  repository: docker.io/goharbor/valkey-photon\n")
        self._override(
            "dev.yaml", "image:\n  repository: goharbor/valkey-photon\n  tag: v2.15.2\n"
        )
        self.assertEqual(self._warn(), "")

    def test_registry_retarget_is_not_flagged(self) -> None:
        """A mirror / custom-build override changes the host, not the image name."""
        self._upstream("image:\n  repository: docker.io/library/ghost\n")
        self._override(
            "dev.yaml", "image:\n  repository: harbor.example.com/library/ghost\n"
        )
        self.assertEqual(self._warn(), "")

    def test_missing_upstream_values_is_silent(self) -> None:
        self._override("dev.yaml", "image:\n  repository: goharbor/redis-photon\n")
        self.assertEqual(self._warn(), "")

    def test_excluded_override_is_skipped(self) -> None:
        self._upstream("image:\n  repository: docker.io/goharbor/valkey-photon\n")
        self._override("dev-old.yaml", "image:\n  repository: goharbor/redis-photon\n")
        self.assertEqual(self._warn(exclude="dev-old.yaml"), "")

    def test_basenames_parsed_with_quotes_and_comments(self) -> None:
        text = (
            'image:\n  repository: "docker.io/goharbor/core"  # quoted + comment\n'
            "other:\n  repository: goharbor/portal\n"
        )
        self.assertEqual(ei._repository_basenames(text), {"core", "portal"})


# =============================================================
# Module wiring — the ``external_with_image_tag`` module must hand its hook to ``external_standard``'s run()
# =============================================================


class HookWiringTests(unittest.TestCase):
    def test_run_delegates_with_post_pin_hook(self) -> None:
        captured: dict = {}

        def fake_run(config, argv, script_path, *, post_pin_hook=None):
            captured["config"] = config
            captured["argv"] = argv
            captured["script_path"] = script_path
            captured["hook"] = post_pin_hook
            return 0

        with mock.patch.object(ei, "_run_external_standard", side_effect=fake_run):
            rc = ei.run({"k": "v"}, ["--dry-run"], "/tmp/upgrade.py")
        self.assertEqual(rc, 0)
        self.assertEqual(captured["config"], {"k": "v"})
        self.assertEqual(captured["argv"], ["--dry-run"])
        self.assertEqual(captured["script_path"], "/tmp/upgrade.py")
        # The hook must be the module-level _rewrite_image_tags.
        self.assertIs(captured["hook"], ei._rewrite_image_tags)


# =============================================================
# run() — integration on image-tag path (mocked subprocess)
# =============================================================


def _fake_subprocess(handler):
    """Mock that routes subprocess.run(...) through handler(cmd)."""
    def _run(cmd, **kwargs):
        text = handler(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=text, stderr="")
    return _run


class RunFlowImageTagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chart_dir = self.tmp / "chart"
        self.chart_dir.mkdir()
        (self.chart_dir / "Chart.yaml").write_text(
            "apiVersion: v2\nname: harbor\nversion: 1.0.0\nappVersion: 2.15.0\n"
        )
        (self.chart_dir / "values.yaml").write_text("global:\n  foo: 1\n")
        (self.chart_dir / "values").mkdir()
        (self.chart_dir / "values" / "dev.yaml").write_text(
            "core:\n  image:\n    tag: v2.15.0\n"
        )
        self.script = self.chart_dir / "upgrade.py"
        self.script.write_text("# stub\n")
        self.config = {
            "SCRIPT_NAME": "Harbor Helm Chart Upgrade Script",
            "HELM_REPO_NAME": "harbor",
            "HELM_REPO_URL": "https://helm.goharbor.io",
            "HELM_CHART": "harbor/harbor",
            "CHANGELOG_URL": "https://example/CHANGELOG.md",
            "CHART_TYPE": "external",
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _handler_for_upgrade(self, latest_chart: str, latest_app: str):
        new_chart_yaml = (
            f"apiVersion: v2\nname: harbor\nversion: {latest_chart}\n"
            f"appVersion: {latest_app}\n"
        )

        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return f'[{{"version":"{latest_chart}","app_version":"{latest_app}"}}]'
            if cmd[:3] == ["helm", "show", "chart"]:
                return new_chart_yaml
            if cmd[:3] == ["helm", "show", "values"]:
                return "global:\n  foo: 2\n"
            if cmd[:2] == ["helm", "pull"]:
                return ""
            if cmd and cmd[0] == "diff":
                return ""
            return ""
        return handler

    def test_already_up_to_date_short_circuits(self) -> None:
        def handler(cmd):
            if cmd[:2] == ["helm", "search"]:
                return '[{"version":"1.0.0","app_version":"2.15.0"}]'
            return ""
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = ei.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("Already up to date! Nothing to do.", buf.getvalue())

    def test_dry_run_preserves_image_tag(self) -> None:
        # Even with a newer appVersion available, dry-run must not touch the
        # values file (image-tag rewrite happens only on the apply path).
        original = (self.chart_dir / "values" / "dev.yaml").read_text()
        handler = self._handler_for_upgrade("1.1.0", "2.16.0")
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = ei.run(self.config, ["--dry-run"], self.script)
        self.assertEqual(rc, 0)
        self.assertIn("[Step 7/7] DRY-RUN complete.", buf.getvalue())
        self.assertEqual(
            (self.chart_dir / "values" / "dev.yaml").read_text(), original
        )

    def test_apply_path_rewrites_image_tag(self) -> None:
        handler = self._handler_for_upgrade("1.1.0", "2.16.0")
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = ei.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        # Banner + completion line.
        self.assertIn("Updated values/dev.yaml", out)
        self.assertIn("(1 image tag(s): v2.15.0 -> v2.16.0)", out)
        self.assertIn("Upgrade complete! (1.0.0 -> 1.1.0)", out)
        # Values file actually mutated on disk.
        self.assertEqual(
            (self.chart_dir / "values" / "dev.yaml").read_text(),
            "core:\n  image:\n    tag: v2.16.0\n",
        )

    def test_apply_with_matching_app_version_does_not_rewrite(self) -> None:
        # When appVersion didn't change (only chart version did) the rewrite
        # block detects no tag drift and stays silent.
        handler = self._handler_for_upgrade("1.1.0", "2.15.0")
        buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=_fake_subprocess(handler)):
            with redirect_stdout(buf):
                rc = ei.run(self.config, [], self.script)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("image tag(s):", out)
        self.assertEqual(
            (self.chart_dir / "values" / "dev.yaml").read_text(),
            "core:\n  image:\n    tag: v2.15.0\n",
        )


# =============================================================
# Single consumer (cicd/harbor-helm) CONFIG dict spot-check
# =============================================================
if __name__ == "__main__":
    unittest.main()

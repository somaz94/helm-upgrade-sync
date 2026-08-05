"""Unit tests for upgrade_sync.paths — repo-root resolution.

The precedence chain is the one piece of behaviour that differs between an
embedded copy (``<repo>/scripts/upgrade-sync/``) and a standalone checkout, so
every branch is pinned here. The embedded branch must win over the cwd-derived
branch: an embedded copy invoked from an unrelated repository has always
targeted its own repo, and callers depend on that.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import load  # noqa: E402


paths = load("upgrade_sync.paths")


def _make_embedded(root: Path) -> Path:
    """Create ``root/scripts/upgrade-sync/`` and return that directory."""
    d = root / "scripts" / "upgrade-sync"
    d.mkdir(parents=True)
    return d


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


class IsEmbeddedTests(unittest.TestCase):
    def test_true_for_scripts_upgrade_sync(self) -> None:
        self.assertTrue(paths.is_embedded(Path("/x/repo/scripts/upgrade-sync")))

    def test_false_for_standalone_root(self) -> None:
        self.assertFalse(paths.is_embedded(Path("/x/helm-upgrade-sync")))

    def test_false_when_parent_is_not_scripts(self) -> None:
        self.assertFalse(paths.is_embedded(Path("/x/tools/upgrade-sync")))


class ExtractRepoRootFlagTests(unittest.TestCase):
    def test_absent_returns_none(self) -> None:
        args, root = paths.extract_repo_root_flag(["--check"])
        self.assertEqual(args, ["--check"])
        self.assertIsNone(root)

    def test_space_separated_form(self) -> None:
        args, root = paths.extract_repo_root_flag(["--repo-root", "/tmp/x", "--check"])
        self.assertEqual(args, ["--check"])
        self.assertEqual(root, "/tmp/x")

    def test_equals_form(self) -> None:
        args, root = paths.extract_repo_root_flag(["--repo-root=/tmp/x", "--status"])
        self.assertEqual(args, ["--status"])
        self.assertEqual(root, "/tmp/x")

    def test_flag_after_command_still_stripped(self) -> None:
        args, root = paths.extract_repo_root_flag(
            ["--apply", "--repo-root", "/tmp/x", "--force"]
        )
        self.assertEqual(args, ["--apply", "--force"])
        self.assertEqual(root, "/tmp/x")

    def test_dangling_flag_is_dropped(self) -> None:
        # A trailing --repo-root with no value must not swallow the command or
        # crash the parser; the caller falls through to the default chain.
        args, root = paths.extract_repo_root_flag(["--check", "--repo-root"])
        self.assertEqual(args, ["--check"])
        self.assertIsNone(root)


class ResolveRepoRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = os.environ.get(paths.ENV_VAR)
        os.environ.pop(paths.ENV_VAR, None)
        self._saved_cwd = Path.cwd()

    def tearDown(self) -> None:
        os.chdir(self._saved_cwd)
        os.environ.pop(paths.ENV_VAR, None)
        if self._saved_env is not None:
            os.environ[paths.ENV_VAR] = self._saved_env

    def test_explicit_flag_wins_over_everything(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            target.mkdir()
            embedded = _make_embedded(Path(td) / "other")
            os.environ[paths.ENV_VAR] = str(Path(td))
            got = paths.resolve_repo_root(embedded, str(target))
            self.assertEqual(got, target.resolve())

    def test_env_var_wins_over_embedded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "target"
            target.mkdir()
            embedded = _make_embedded(Path(td) / "repo")
            os.environ[paths.ENV_VAR] = str(target)
            self.assertEqual(paths.resolve_repo_root(embedded), target.resolve())

    def test_embedded_layout_resolves_two_up(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            embedded = _make_embedded(repo)
            self.assertEqual(paths.resolve_repo_root(embedded), repo)

    def test_embedded_wins_over_cwd_git_root(self) -> None:
        """The regression guard: an embedded copy run from another repo's cwd."""
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            embedded = _make_embedded(repo)
            unrelated = Path(td) / "unrelated"
            unrelated.mkdir()
            _git_init(unrelated)
            os.chdir(unrelated)
            self.assertEqual(paths.resolve_repo_root(embedded), repo)

    def test_standalone_falls_back_to_cwd_git_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tool = Path(td) / "helm-upgrade-sync"
            tool.mkdir()
            target = Path(td) / "target"
            (target / "nested" / "deep").mkdir(parents=True)
            _git_init(target)
            os.chdir(target / "nested" / "deep")
            got = paths.resolve_repo_root(tool)
            self.assertEqual(got.resolve(), target.resolve())

    def test_standalone_outside_git_falls_back_to_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tool = Path(td) / "helm-upgrade-sync"
            tool.mkdir()
            plain = Path(td) / "plain"
            plain.mkdir()
            os.chdir(plain)
            got = paths.resolve_repo_root(tool)
            # Outside any worktree git exits non-zero, so cwd is the answer.
            # Compare resolved forms — macOS /var is a symlink to /private/var.
            self.assertEqual(got.resolve(), plain.resolve())

    def test_tilde_in_explicit_path_is_expanded(self) -> None:
        got = paths.resolve_repo_root(Path("/x/tool"), "~")
        self.assertEqual(got, Path.home().resolve())


if __name__ == "__main__":
    unittest.main()

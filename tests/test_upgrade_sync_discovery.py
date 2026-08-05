"""Unit tests for upgrade_sync.discovery.

Covers find_managed_files / find_unmanaged_charts / parse_template_header.
All tests build a self-contained mini-repo under ``tempfile.TemporaryDirectory``
so the assertions never depend on the host repo's current consumer mix.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _loader import load


discovery = load("upgrade_sync.discovery")


def _seed_file(path: Path, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class FindManagedFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_finds_upgrade_sh_and_upgrade_py(self) -> None:
        _seed_file(self.repo / "comp-a" / "upgrade.py", "#!/usr/bin/env python3\n")
        _seed_file(self.repo / "comp-b" / "upgrade.sh", "#!/usr/bin/env bash\n")
        result = discovery.find_managed_files(self.repo)
        rels = [str(p.relative_to(self.repo)) for p in result]
        self.assertEqual(rels, ["comp-a/upgrade.py", "comp-b/upgrade.sh"])

    def test_skips_backup_deprecated_optional_sync_fixture(self) -> None:
        _seed_file(self.repo / "comp-a" / "upgrade.py", "")
        _seed_file(self.repo / "comp-a" / "backup" / "upgrade.py", "")
        _seed_file(self.repo / "_deprecated" / "old" / "upgrade.py", "")
        _seed_file(self.repo / "_optional" / "future" / "upgrade.py", "")
        _seed_file(self.repo / "scripts" / "upgrade-sync" / "upgrade.py", "")
        _seed_file(self.repo / "tests" / "python" / "fixtures" / "upgrade.py", "")
        result = discovery.find_managed_files(self.repo)
        rels = [str(p.relative_to(self.repo)) for p in result]
        self.assertEqual(rels, ["comp-a/upgrade.py"])

    def test_byte_sort_matches_bash_find_sort(self) -> None:
        """Path tuple-sort puts 'foo' before 'foo-bar', but bash find|sort
        puts 'foo-bar' first (0x2d < 0x2f). discovery must match bash."""
        _seed_file(self.repo / "security" / "keycloak" / "upgrade.py", "")
        _seed_file(self.repo / "security" / "keycloak-operator" / "upgrade.py", "")
        result = discovery.find_managed_files(self.repo)
        rels = [str(p.relative_to(self.repo)) for p in result]
        self.assertEqual(
            rels,
            [
                "security/keycloak-operator/upgrade.py",
                "security/keycloak/upgrade.py",
            ],
        )

    def test_empty_repo_returns_empty(self) -> None:
        self.assertEqual(discovery.find_managed_files(self.repo), [])


class FindUnmanagedChartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_chart_without_upgrade_is_unmanaged(self) -> None:
        _seed_file(self.repo / "comp-a" / "Chart.yaml", "name: a\n")
        result = discovery.find_unmanaged_charts(self.repo)
        self.assertEqual(result, ["comp-a"])

    def test_chart_with_upgrade_py_is_managed(self) -> None:
        _seed_file(self.repo / "comp-a" / "Chart.yaml", "name: a\n")
        _seed_file(self.repo / "comp-a" / "upgrade.py", "")
        self.assertEqual(discovery.find_unmanaged_charts(self.repo), [])

    def test_chart_with_upgrade_sh_is_managed(self) -> None:
        _seed_file(self.repo / "comp-a" / "Chart.yaml", "name: a\n")
        _seed_file(self.repo / "comp-a" / "upgrade.sh", "")
        self.assertEqual(discovery.find_unmanaged_charts(self.repo), [])

    def test_skips_templates_subdir(self) -> None:
        _seed_file(self.repo / "chart" / "templates" / "Chart.yaml", "name: x\n")
        self.assertEqual(discovery.find_unmanaged_charts(self.repo), [])

    def test_skips_backup_deprecated_optional(self) -> None:
        _seed_file(self.repo / "comp" / "backup" / "Chart.yaml", "name: x\n")
        _seed_file(self.repo / "_deprecated" / "x" / "Chart.yaml", "name: x\n")
        _seed_file(self.repo / "_optional" / "y" / "Chart.yaml", "name: y\n")
        self.assertEqual(discovery.find_unmanaged_charts(self.repo), [])

    def test_results_deduplicated_and_sorted(self) -> None:
        # Two charts under the same parent — both unmanaged.
        _seed_file(self.repo / "comp-b" / "Chart.yaml", "")
        _seed_file(self.repo / "comp-a" / "Chart.yaml", "")
        result = discovery.find_unmanaged_charts(self.repo)
        self.assertEqual(result, ["comp-a", "comp-b"])


class ParseTemplateHeaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_reads_line_2_header(self) -> None:
        f = self.repo / "upgrade.py"
        f.write_text(
            "#!/usr/bin/env python3\n# upgrade-template: external-standard\n# body\n",
            encoding="utf-8",
        )
        self.assertEqual(
            discovery.parse_template_header(f),
            "external-standard",
        )

    def test_returns_empty_when_header_absent(self) -> None:
        f = self.repo / "upgrade.py"
        f.write_text("#!/usr/bin/env python3\n# not-a-template\n", encoding="utf-8")
        self.assertEqual(discovery.parse_template_header(f), "")

    def test_returns_empty_on_unreadable(self) -> None:
        self.assertEqual(
            discovery.parse_template_header(self.repo / "missing.py"),
            "",
        )

    def test_strips_trailing_newline_only(self) -> None:
        f = self.repo / "upgrade.py"
        f.write_text(
            "#!/usr/bin/env python3\n# upgrade-template: foo  \n", encoding="utf-8"
        )
        # Trailing whitespace inside the value is preserved by the bash
        # ``sed`` original — keep that contract.
        self.assertEqual(discovery.parse_template_header(f), "foo  ")


if __name__ == "__main__":
    unittest.main()

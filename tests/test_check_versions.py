"""Unit tests for check-versions.py.

Stdlib unittest only — keeps dep surface at zero so the suite runs under
both the helmfile-tools image's system python3 and any local venv.

Test design:
  - Pure helpers (parse_template_header, matches_only, parse_args) —
    direct argument-based cases. The orchestrator now defers every
    larger responsibility to sister test modules:

      * ``test_upgrade_sync_config_parse.py``  (config_parse.py)
      * ``test_upgrade_sync_fetchers.py``      (fetchers.py)
      * ``test_upgrade_sync_table.py``         (table.py)
      * ``test_upgrade_sync_yaml_helpers.py``  (yaml_helpers.py)

Fixtures live under tests/python/fixtures/ and are re-used in the MR
validation step for byte-for-byte parity against the bash version.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import ModuleType


sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loader import TOOL_ROOT  # noqa: E402

SCRIPT_PATH = TOOL_ROOT / "check-versions.py"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _load_module() -> ModuleType:
    """Import check-versions.py despite the hyphen in its name.

    Same importlib pattern as the other tests — sys.modules registration
    before exec_module() so python 3.14+ dataclass introspection works.
    """
    spec = importlib.util.spec_from_file_location("check_versions", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cv = _load_module()


class TestParseTemplateHeader(unittest.TestCase):

    def test_returns_template_name(self) -> None:
        # Re-use the auto-upgrade fixtures — same header shape.
        path = FIXTURES_DIR / "auto-upgrade-upgrade-oci-cr.sh"
        self.assertEqual(
            cv.parse_template_header(path), "external-oci-cr-version"
        )

    def test_returns_empty_when_missing(self) -> None:
        path = FIXTURES_DIR / "auto-upgrade-upgrade-no-header.sh"
        self.assertEqual(cv.parse_template_header(path), "")

    def test_returns_empty_when_file_missing(self) -> None:
        self.assertEqual(
            cv.parse_template_header(FIXTURES_DIR / "does-not-exist.sh"), ""
        )


class TestMatchesOnly(unittest.TestCase):

    def test_empty_patterns_match_all(self) -> None:
        self.assertTrue(cv.matches_only("cicd/argo-cd/upgrade.sh", []))

    def test_substring_match(self) -> None:
        self.assertTrue(
            cv.matches_only("cicd/argo-cd/upgrade.sh", ["argo-cd"])
        )

    def test_no_match_returns_false(self) -> None:
        self.assertFalse(
            cv.matches_only("cicd/argo-cd/upgrade.sh", ["valkey"])
        )

    def test_any_pattern_matches(self) -> None:
        self.assertTrue(
            cv.matches_only(
                "cicd/argo-cd/upgrade.sh", ["does-not-exist", "argo"]
            )
        )


class TestArgParsing(unittest.TestCase):

    def test_repeatable_only(self) -> None:
        ns = cv.parse_args(["check-versions.py", "--only", "a", "--only", "b"])
        self.assertEqual(ns.only, ["a", "b"])

    def test_no_update_flag(self) -> None:
        ns = cv.parse_args(["check-versions.py", "--no-update"])
        self.assertTrue(ns.no_update)

    def test_updates_only_flag(self) -> None:
        ns = cv.parse_args(["check-versions.py", "--updates-only"])
        self.assertTrue(ns.updates_only)

    def test_defaults(self) -> None:
        ns = cv.parse_args(["check-versions.py"])
        self.assertEqual(ns.only, [])
        self.assertFalse(ns.no_update)
        self.assertFalse(ns.updates_only)


if __name__ == "__main__":
    unittest.main()

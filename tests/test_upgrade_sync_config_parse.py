"""Unit tests for upgrade_sync/config_parse.py.

CONFIG-block parsing (extracted from ``check-versions.py``) — see the
module docstring for the input grammar. Tests use the same fixtures as
``test_check_versions.py`` so the byte-for-byte parity contract with
the original bash CONFIG block is preserved.

Stdlib unittest only.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# Make ``upgrade_sync`` importable when the test is launched directly
# (``python3 -m unittest tests/python/test_upgrade_sync_config_parse.py``)
# without going through the repo Makefile.
_pkg_root = REPO_ROOT / "scripts" / "python"
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from upgrade_sync.config_parse import (  # noqa: E402
    ConfigVars,
    _clean_value,
    parse_config_block,
)


class TestCleanValue(unittest.TestCase):

    def test_strips_double_quotes(self) -> None:
        self.assertEqual(_clean_value('"values/dev.yaml"'), "values/dev.yaml")

    def test_strips_single_quotes(self) -> None:
        self.assertEqual(_clean_value("'foo'"), "foo")

    def test_strips_trailing_inline_comment(self) -> None:
        self.assertEqual(_clean_value('"v" # default'), "v")

    def test_resolves_shell_param_default_colon_dash(self) -> None:
        # ``${VAR:-default}`` → ``default`` (bash eval result when VAR
        # is unset).
        self.assertEqual(_clean_value('"${GITHUB_TAG_PREFIX:-v}"'), "v")

    def test_resolves_shell_param_default_dash(self) -> None:
        # ``${VAR-default}`` (no colon) → ``default``. Same in our parser.
        self.assertEqual(_clean_value('"${FOO-bar}"'), "bar")

    def test_bare_value_passthrough(self) -> None:
        self.assertEqual(_clean_value("9"), "9")


class TestParseConfigBlock(unittest.TestCase):

    # ``check-versions.py`` (and now ``parse_config_block``) expects the
    # canonical ``# === ... ===`` fence triple around the CONFIG block —
    # ``auto-upgrade.py``'s ``parse_config_var`` is more forgiving and
    # scans the whole file. Hence a dedicated fixture.
    FIXTURE = FIXTURES_DIR / "check-versions-upgrade-with-fences.sh"

    def test_extracts_known_keys(self) -> None:
        cfg = parse_config_block(self.FIXTURE)
        self.assertEqual(cfg.values_file, "values/dev.yaml")
        self.assertEqual(cfg.version_key, "version")
        self.assertEqual(cfg.helm_repo_name, "elastic")
        self.assertEqual(cfg.helm_repo_url, "oci://docker.elastic.co/helm")

    def test_resolves_shell_param_default_in_value(self) -> None:
        # ``GITHUB_TAG_PREFIX="${GITHUB_TAG_PREFIX:-v}"`` → ``"v"``.
        cfg = parse_config_block(self.FIXTURE)
        self.assertEqual(cfg.github_tag_prefix, "v")

    def test_extracts_chart_source_fields(self) -> None:
        cfg = parse_config_block(self.FIXTURE)
        self.assertEqual(cfg.chart_source_type, "github-releases")
        self.assertEqual(cfg.chart_source_repo, "somaz94/helm-charts")
        self.assertEqual(cfg.chart_name, "elasticsearch-eck")

    def test_file_without_fences_returns_defaults(self) -> None:
        path = FIXTURES_DIR / "auto-upgrade-upgrade-no-header.sh"
        cfg = parse_config_block(path)
        self.assertEqual(cfg.script_name, "")
        self.assertEqual(cfg.values_file, "")

    # the shell -> python migration migrated canonical templates from ``.sh``
    # to ``.py`` — the CONFIG block now comes in Python dict form. ``local_cr_version``
    # wired ``parse_config_block`` to handle both shapes so check-versions.py
    # keeps working across the mixed-mode period and after the migration.
    PY_FIXTURE = FIXTURES_DIR / "check-versions-upgrade-with-fences.py"

    def test_python_dict_extracts_known_keys(self) -> None:
        cfg = parse_config_block(self.PY_FIXTURE)
        self.assertEqual(cfg.values_file, "values/dev.yaml")
        self.assertEqual(cfg.version_key, "version")
        self.assertEqual(cfg.version_source, "elastic-artifacts")
        self.assertEqual(cfg.major_pin, "9")

    def test_python_dict_extracts_chart_source_fields(self) -> None:
        cfg = parse_config_block(self.PY_FIXTURE)
        self.assertEqual(cfg.chart_source_type, "github-releases")
        self.assertEqual(cfg.chart_source_repo, "somaz94/helm-charts")
        self.assertEqual(cfg.chart_name, "elasticsearch-eck")

    def test_python_dict_quoted_string_with_spaces(self) -> None:
        # ``"SCRIPT_NAME": "Elasticsearch ... Script",`` should clean
        # to the full quoted string with internal spaces preserved.
        cfg = parse_config_block(self.PY_FIXTURE)
        self.assertEqual(
            cfg.script_name,
            "Elasticsearch (ECK CR, OCI chart) Stack Version Upgrade Script",
        )


class TestArgocdPinBase(unittest.TestCase):
    """argocd-pin template adds a BASE key selecting the wrapped base flow."""

    def test_extracts_base_key(self) -> None:
        body = (
            "#!/usr/bin/env python3\n"
            "# upgrade-template: argocd-pin\n"
            "# ==========================================\n"
            'CONFIG = {\n'
            '    "SCRIPT_NAME": "x",\n'
            '    "BASE": "oci",\n'
            '    "HELM_CHART": "oci://ghcr.io/somaz94/charts/ghost",\n'
            "}\n"
            "# ==========================================\n"
            "# ==========================================\n"
        )
        tmp = Path(tempfile.mkdtemp()) / "upgrade.py"
        tmp.write_text(body)
        cfg = parse_config_block(tmp)
        self.assertEqual(cfg.base, "oci")


class TestConfigVarsDefaults(unittest.TestCase):

    def test_empty_construction(self) -> None:
        # All fields default to "" so per-template branching can rely on
        # truthiness without KeyError handling.
        cfg = ConfigVars()
        self.assertEqual(cfg.script_name, "")
        self.assertEqual(cfg.helm_repo_name, "")
        self.assertEqual(cfg.chart_source_type, "")
        self.assertEqual(cfg.base, "")

    def test_frozen_instance(self) -> None:
        # ``frozen=True`` — assignment must raise.
        cfg = ConfigVars(script_name="x")
        with self.assertRaises(Exception):
            cfg.script_name = "y"  # type: ignore[misc]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

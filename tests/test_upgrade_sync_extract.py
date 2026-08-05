"""Unit tests for upgrade_sync.extract.

Covers extract_config_block / extract_body / canonical_path / build_expected.
``build_expected`` is exercised against a synthetic mini canonical + target
pair so each branch (.sh vs .py) is asserted in isolation.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from _loader import load


extract = load("upgrade_sync.extract")


FENCE = "# " + ("=" * 60)


PY_TEMPLATE_HEADER = "#!/usr/bin/env python3\n"
SH_TEMPLATE_HEADER = "#!/bin/bash\nset -euo pipefail\n"


def _seed(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class ExtractConfigBlockTests(unittest.TestCase):
    def test_captures_lines_between_three_fences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "u.py"
            f.write_text(
                f"#!/usr/bin/env python3\n"
                f"# upgrade-template: x\n"
                f"\n"
                f"{FENCE}\n"
                f"# Configuration\n"
                f"{FENCE}\n"
                f'CONFIG = {{\n    "K": "V",\n}}\n'
                f"{FENCE}\n"
                f"# body line 1\n"
                f"# body line 2\n",
                encoding="utf-8",
            )
            block = extract.extract_config_block(f)
        # Expect from first fence through third fence inclusive.
        expected = (
            f"{FENCE}\n# Configuration\n{FENCE}\n"
            f'CONFIG = {{\n    "K": "V",\n}}\n'
            f"{FENCE}\n"
        )
        self.assertEqual(block, expected)

    def test_empty_when_no_fences(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "u.py"
            f.write_text("just text\nno fences\n", encoding="utf-8")
            self.assertEqual(extract.extract_config_block(f), "")


class ExtractBodyTests(unittest.TestCase):
    def test_skips_first_three_fences_and_returns_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "u.py"
            f.write_text(
                f"{FENCE}\nconfig 1\n{FENCE}\nconfig 2\n{FENCE}\nbody line 1\nbody line 2\n",
                encoding="utf-8",
            )
            body = extract.extract_body(f)
        self.assertEqual(body, "body line 1\nbody line 2\n")

    def test_empty_when_no_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "u.py"
            f.write_text(f"{FENCE}\nx\n{FENCE}\ny\n{FENCE}\n", encoding="utf-8")
            self.assertEqual(extract.extract_body(f), "")


class CanonicalPathTests(unittest.TestCase):
    def test_returns_existing_py_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp)
            target = templates / "foo.py"
            target.write_text("", encoding="utf-8")
            self.assertEqual(extract.canonical_path(templates, "foo", "py"), target)

    def test_exits_2_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp)
            with self.assertRaises(SystemExit) as ctx:
                extract.canonical_path(templates, "ghost", "py")
            self.assertEqual(ctx.exception.code, 2)

    def test_exits_2_with_mixed_mode_hint_when_only_sh_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp)
            (templates / "halfway.sh").write_text("", encoding="utf-8")
            with self.assertRaises(SystemExit) as ctx:
                extract.canonical_path(templates, "halfway", "py")
            self.assertEqual(ctx.exception.code, 2)


class BuildExpectedTests(unittest.TestCase):
    def test_py_target_uses_python_shebang(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp) / "templates"
            templates.mkdir()
            _seed(
                templates / "demo.py",
                f"{PY_TEMPLATE_HEADER}{FENCE}\nignored\n{FENCE}\nignored\n{FENCE}\n"
                f"body line A\nbody line B\n",
            )
            target = Path(tmp) / "u.py"
            _seed(
                target,
                "#!/usr/bin/env python3\n# upgrade-template: demo\n\n"
                f"{FENCE}\n# Config\n{FENCE}\nCONFIG = {{}}\n{FENCE}\n"
                "old body to be replaced\n",
            )
            out = extract.build_expected(target, "demo", templates)
        self.assertTrue(out.startswith("#!/usr/bin/env python3\n# upgrade-template: demo\n\n"))
        self.assertIn(f"{FENCE}\n# Config\n", out)
        self.assertIn("body line A\nbody line B\n", out)
        # Must not contain "old body" — that comes from target's body region
        # which build_expected discards in favor of the canonical's body.
        self.assertNotIn("old body to be replaced", out)

    def test_sh_target_uses_bash_shebang_plus_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp) / "templates"
            templates.mkdir()
            _seed(
                templates / "legacy.sh",
                f"{SH_TEMPLATE_HEADER}{FENCE}\nx\n{FENCE}\ny\n{FENCE}\nbody\n",
            )
            target = Path(tmp) / "u.sh"
            _seed(
                target,
                "#!/bin/bash\n# upgrade-template: legacy\nset -euo pipefail\n\n"
                f"{FENCE}\n# C\n{FENCE}\nKEY=v\n{FENCE}\nold\n",
            )
            out = extract.build_expected(target, "legacy", templates)
        self.assertTrue(out.startswith("#!/bin/bash\n# upgrade-template: legacy\nset -euo pipefail\n\n"))
        self.assertIn("body\n", out)
        self.assertNotIn("old\n", out)

    def test_output_ends_with_single_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            templates = Path(tmp) / "templates"
            templates.mkdir()
            _seed(
                templates / "demo.py",
                f"{PY_TEMPLATE_HEADER}{FENCE}\nx\n{FENCE}\ny\n{FENCE}\nbody\n",
            )
            target = Path(tmp) / "u.py"
            _seed(
                target,
                f"#!/usr/bin/env python3\n# upgrade-template: demo\n\n{FENCE}\nC\n{FENCE}\nK=v\n{FENCE}\nold\n",
            )
            out = extract.build_expected(target, "demo", templates)
        self.assertTrue(out.endswith("\n"))
        self.assertFalse(out.endswith("\n\n"))


if __name__ == "__main__":
    unittest.main()

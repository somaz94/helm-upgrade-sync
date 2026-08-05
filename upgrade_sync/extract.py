"""CONFIG / body extraction + ``build_expected`` for the sync system.

Ports the bash ``extract_config_block`` / ``extract_body`` / ``canonical_path``
/ ``build_expected`` helpers. The CONFIG block of a managed file spans the
first three ``# ===...===`` fences (inclusive); everything strictly after
the third fence is the canonical-owned body.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Three ``# ==========...`` fences delimit the CONFIG block (10+ '=' chars).
# Matches the bash ``/^# ={10,}$/`` pattern verbatim.
_FENCE_RE = re.compile(r"^# ={10,}$")


def canonical_path(templates_dir: Path, name: str, ext: str = "sh") -> Path:
    """Resolve a template name + extension to its canonical file path.

    Exits process with code 2 (mirroring bash ``canonical_path``) when the
    requested flavor is missing. Falls back from .py → .sh only when the
    requested .py canonical does not exist and the .sh canonical does —
    matches the mixed-mode safety check.
    """
    path = templates_dir / f"{name}.{ext}"
    if path.is_file():
        return path

    # When the caller asked for .py but only .sh exists, fail loudly —
    # this protects mixed-mode rollouts from silently feeding bash body
    # to a .py consumer.
    if ext == "py" and (templates_dir / f"{name}.sh").is_file():
        print(
            f"ERROR: canonical .py template '{name}' not found at {path}",
            file=sys.stderr,
        )
        print(
            f"       (a .sh canonical exists — '{name}' has not been migrated yet.)",
            file=sys.stderr,
        )
        sys.exit(2)

    print(
        f"ERROR: canonical template '{name}' not found at {path}",
        file=sys.stderr,
    )
    sys.exit(2)


def extract_config_block(path: Path) -> str:
    """Return lines from the first ``# ===…===`` fence through the third.

    Inclusive on both ends. Mirrors bash awk:
        /^# ={10,}$/ { c++; print; if (c == 3) exit; next }
        c >= 1 { print }
    """
    out: list[str] = []
    fences = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if _FENCE_RE.match(line):
                fences += 1
                out.append(line)
                if fences == 3:
                    break
                continue
            if fences >= 1:
                out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def extract_body(path: Path) -> str:
    """Return every line strictly after the third ``# ===…===`` fence.

    Mirrors bash awk:
        /^# ={10,}$/ { c++; next }
        c >= 3 { print }
    """
    out: list[str] = []
    fences = 0
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if _FENCE_RE.match(line):
                fences += 1
                continue
            if fences >= 3:
                out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def build_expected(target: Path, template: str, templates_dir: Path) -> str:
    """Build the expected file content for ``target`` under ``template``.

    Format (``.sh`` target — legacy, kept for forward-compat):
        #!/bin/bash
        # upgrade-template: <name>
        set -euo pipefail
        <blank>
        <CONFIG block from target>
        <body from canonical .sh>

    Format (``.py`` target — every consumer):
        #!/usr/bin/env python3
        # upgrade-template: <name>
        <blank>
        <CONFIG block from target>
        <body from canonical .py>
    """
    ext = target.suffix.lstrip(".")
    canonical = canonical_path(templates_dir, template, ext)

    header: list[str] = []
    if ext == "py":
        header.append("#!/usr/bin/env python3")
        header.append(f"# upgrade-template: {template}")
        header.append("")
    else:
        header.append("#!/bin/bash")
        header.append(f"# upgrade-template: {template}")
        header.append("set -euo pipefail")
        header.append("")

    config = extract_config_block(target)
    body = extract_body(canonical)
    # ``extract_*`` helpers already terminate with "\n" when non-empty —
    # join the three regions and let the caller print() append the final
    # trailing newline if needed. The bash original printf'd each region
    # the same way, so the byte parity is preserved.
    return "\n".join(header) + "\n" + config + body

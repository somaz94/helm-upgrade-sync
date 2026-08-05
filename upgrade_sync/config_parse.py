"""CONFIG-block parsing for the managed upgrade.py templates.

Extracted from ``check-versions.py`` so the parser
can be unit-tested and reused by CI orchestrators
without pulling in the orchestrator's full dependency surface.

The CONFIG block is the canonical metadata header carried by every
managed ``upgrade.py`` (and the legacy ``upgrade.sh``). Three ``# ===``
fences delimit it:

    # ==========================================
    # CONFIG (template-specific)
    # ==========================================
    "VERSION_SOURCE": "github-releases",
    "VERSION_SOURCE_ARG": "owner/repo",
    ...
    # ==========================================

Both bash form (``KEY="VALUE"``) and Python dict form
(``"KEY": "VALUE",``) are accepted — the shell -> python migration moved every
canonical template + every consumer to ``.py``, so the dict form is
dominant, but the bash form is still seen in ``_optional/`` and
``_deprecated/`` trees which the discovery walker may still visit.

No ``eval`` — pure regex extraction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class ConfigVars:
    """CONFIG block scalars from a managed upgrade script.

    Mirrors the bash ``dump_config_vars`` output — every potentially
    relevant variable across all template types is collected with
    empty-string defaults so the per-template switch downstream can
    branch cleanly.
    """

    script_name: str = ""
    helm_repo_name: str = ""
    helm_repo_url: str = ""
    helm_chart: str = ""
    chart_type: str = ""
    chart_git_repo: str = ""
    chart_git_path: str = ""
    version_source: str = ""
    version_source_arg: str = ""
    values_file: str = ""
    version_key: str = ""
    major_pin: str = ""
    container_image: str = ""
    github_repo: str = ""
    github_tag_prefix: str = ""
    version_file: str = ""
    chart_source_type: str = ""
    chart_source_repo: str = ""
    chart_name: str = ""
    # argocd-pin template: selects the wrapped base flow (helm repo vs OCI).
    base: str = ""


# Mapping from CONFIG block variable name (uppercase) to ConfigVars field
# name (lowercase). Filtered to just the keys we care about so a stray
# variable in upgrade.sh / upgrade.py doesn't accidentally populate an
# attribute.
_CONFIG_KEYS: dict[str, str] = {f.name.upper(): f.name for f in fields(ConfigVars)}


# CONFIG block boundary — bash's ``extract_config_block`` walked from
# the first ``# ===…===`` separator through the third (inclusive). Three
# ``=``-runs wrap the CONFIG section in every canonical upgrade script.
_CONFIG_BLOCK_FENCE = re.compile(r"^# ={10,}$")

# CONFIG-line shape for bash templates: ``KEY="VALUE"`` / ``KEY='VALUE'``
# / ``KEY=VALUE``. Captures the key + the raw value text up to the first
# ``#`` (comment) or the end of line. Trailing whitespace + surrounding
# quotes are stripped in ``_clean_value()`` below.
_CONFIG_LINE = re.compile(r'^([A-Z_][A-Z0-9_]*)=(.*)$')

# CONFIG-line shape for Python dict templates: ``"KEY": "VALUE",`` (with
# optional whitespace + trailing comma + trailing ``# comment``). Used
# by K6+ canonical templates (``*.py``) which carry CONFIG as a Python
# dict literal instead of bash variable assignments. Trailing comma and
# surrounding quotes are stripped in ``_clean_value()``.
_CONFIG_LINE_PY = re.compile(r'^\s*"([A-Z_][A-Z0-9_]*)"\s*:\s*(.*?)\s*,?\s*$')

# Shell parameter expansion: ``${VAR:-default}`` or ``${VAR-default}``.
# The bash eval resolved this to ``<default>`` when ``VAR`` was unset
# (the standard case in our CONFIG blocks — e.g.
# ``GITHUB_TAG_PREFIX="${GITHUB_TAG_PREFIX:-v}"``). The python parser
# doesn't run bash, so we substitute the default explicitly.
_SHELL_PARAM_DEFAULT = re.compile(r'^\$\{([A-Z_][A-Z0-9_]*):?-([^}]*)\}$')


def _clean_value(raw: str) -> str:
    """Strip surrounding quotes + trailing whitespace/comment from a
    CONFIG value, then resolve ``${VAR:-default}`` to ``<default>`` (the
    bash-eval result when ``VAR`` is unset, which is always the case in
    bash CONFIG blocks).

    Handles both bash form (``KEY="value"``) and Python dict form
    (``"KEY": "value",``) — the trailing comma in Python dict lines is
    stripped before quote handling so ``"value",`` cleans to ``value``.
    """
    # Drop trailing inline comment (` # ...`) — same as bash's awk strip.
    if " #" in raw:
        raw = raw[: raw.index(" #")]
    raw = raw.strip()
    # Strip trailing comma (Python dict line shape — bash has no
    # trailing comma so this is a no-op for ``.sh`` consumers).
    if raw.endswith(","):
        raw = raw[:-1].rstrip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ('"', "'"):
        raw = raw[1:-1]
    m = _SHELL_PARAM_DEFAULT.match(raw)
    if m:
        return m.group(2)
    return raw


def parse_config_block(upgrade_script: Path) -> ConfigVars:
    """Read the CONFIG block (first ``# ===`` through third) and pull
    the scalar assignments into a :class:`ConfigVars` instance.

    Handles both bash (``KEY="VALUE"``) and Python dict
    (``"KEY": "VALUE",``) line shapes — the K6+ canonical templates
    switched from ``.sh`` to ``.py`` over that migration, so the
    CONFIG block now comes in two flavors. Bash form is tried first; on
    miss the Python dict form is tried. Unknown keys are silently
    ignored (matches the bash version's ``set +u`` tolerance).

    No ``eval`` — pure regex extraction.
    """
    fence_count = 0
    values: dict[str, str] = {}
    try:
        with upgrade_script.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.rstrip("\n")
                if _CONFIG_BLOCK_FENCE.match(stripped):
                    fence_count += 1
                    if fence_count >= 3:
                        break
                    continue
                if fence_count < 1:
                    continue
                m = _CONFIG_LINE.match(stripped)
                if m is None:
                    m = _CONFIG_LINE_PY.match(stripped)
                if not m:
                    continue
                key = m.group(1)
                field_name = _CONFIG_KEYS.get(key)
                if field_name is None:
                    continue
                values[field_name] = _clean_value(m.group(2))
    except OSError:
        pass
    return ConfigVars(**values)

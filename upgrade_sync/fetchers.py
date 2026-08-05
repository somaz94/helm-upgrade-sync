"""Upstream version fetchers + container-image existence probe.

Extracted from ``scripts/upgrade-sync/check-versions.py`` so the network
calls can be unit-tested in isolation and reused by future tools.

Public API:

- :func:`fetch_latest_helm_repo` — ``helm search repo`` (already
  registered) → newest version.
- :func:`fetch_latest_git_tags` — ``git ls-remote --tags --sort=-v:refname``
  → newest semver-triplet tag, ``v`` prefix dropped.
- :func:`fetch_ga_versions_source` — multi-backend GA list (elastic /
  github-releases / docker-hub-tags), descending order, optional
  ``major_pin`` filter.
- :func:`fetch_latest_version_source` — first entry of the above.
- :func:`fetch_latest_chart_version_gh` — OCI chart-pin release tag shape
  ``<chart-name>-X.Y.Z`` → newest version.
- :func:`verify_image_exists` — Docker Registry v2 manifest HEAD probe
  with anonymous bearer-token retry.
- :func:`find_latest_available_source` — walk the descending GA list,
  return the newest version with a published image (capped at
  :data:`IMAGE_PROBE_MAX_ATTEMPTS`).

All fetchers fail soft — network / parse errors return ``""`` /
``[]`` / ``False`` rather than raising. Mirrors the bash ``curl -sSfL``
contract that callers downstream expect.

Stdlib only.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request


# Container-image probe cap so a regression at upstream (e.g. all the
# top-N tags missing the image) doesn't make the script hang for minutes.
IMAGE_PROBE_MAX_ATTEMPTS = 15

# HTTP timeout (seconds) for upstream metadata fetch. Mirrors bash
# ``curl --max-time`` implicit behaviour (~10s on most installs).
HTTP_TIMEOUT = 10.0

# Semver triplet regex — ``<major>.<minor>.<patch>``, used to filter the
# GA subset of tag lists across all version-source backends.
SEMVER_TRIPLET = re.compile(r"^\d+\.\d+\.\d+$")

# Tag-shape filter for git-tags: optional ``v`` prefix + semver triplet.
_GIT_TAG_SEMVER = re.compile(r"^v?(\d+\.\d+\.\d+)$")

# User-Agent for upstream metadata calls. GitHub API requires a User-Agent
# and rate-limits the generic urllib default more aggressively. Match the
# bash script's effective curl identity so per-IP/UA buckets match.
_HTTP_USER_AGENT = "helm-upgrade-sync/1.0"


# =============================================================
# Generic HTTP + sort helpers (module-private)
# =============================================================

def _http_get_json(url: str) -> object | None:
    """GET ``url``, parse body as JSON. Returns ``None`` on any failure.

    No bearer auth; the bash version's ``curl -sSfL`` likewise was
    anonymous. A stable User-Agent is sent so GitHub's anonymous
    rate-limit bucket is keyed consistently with the bash version's
    curl traffic.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _HTTP_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _semver_sort_desc(versions: list[str]) -> list[str]:
    """Descending numeric sort by (major, minor, patch) triplet."""
    def key(v: str) -> tuple[int, int, int]:
        try:
            return tuple(int(p) for p in v.split("."))  # type: ignore[return-value]
        except ValueError:
            return (-1, -1, -1)
    return sorted(versions, key=key, reverse=True)


# =============================================================
# Helm + git fetchers
# =============================================================

def fetch_latest_helm_repo(chart: str) -> str:
    """``helm search repo <chart> --output json`` → first version, or ``""``."""
    if not chart:
        return ""
    result = subprocess.run(
        ["helm", "search", "repo", chart, "--output", "json"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return ""
    try:
        rows = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ""
    if not rows:
        return ""
    return str(rows[0].get("version", ""))


def fetch_latest_git_tags(repo: str) -> str:
    """``git ls-remote --tags --refs --sort='-v:refname' <repo>`` → newest
    semver triplet (with ``v`` prefix dropped), or ``""``.
    """
    if not repo:
        return ""
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", "--sort=-v:refname", repo],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return ""
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        ref = parts[1].removeprefix("refs/tags/")
        m = _GIT_TAG_SEMVER.match(ref)
        if m:
            return m.group(1)
    return ""


# =============================================================
# Multi-backend GA version source
# =============================================================

def fetch_ga_versions_source(
    source: str, major_pin: str, source_arg: str = "", tag_prefix: str = "",
) -> list[str]:
    """Return the descending GA-version list for a version-source backend.

    Three backends supported (parity with bash):

      ``elastic-artifacts`` → https://artifacts-api.elastic.co/v1/versions
      ``github-releases``   → GitHub Releases API (per_page=100, skip pre/draft)
      ``docker-hub-tags``   → Docker Hub tags API (page_size=100, ordering=last_updated)
    """
    versions: list[str] = []
    if source == "elastic-artifacts":
        data = _http_get_json("https://artifacts-api.elastic.co/v1/versions")
        if not isinstance(data, dict):
            return []
        raw = data.get("versions", []) or []
        versions = [v for v in raw if isinstance(v, str) and SEMVER_TRIPLET.match(v)]
    elif source == "github-releases":
        if not source_arg:
            return []
        url = f"https://api.github.com/repos/{source_arg}/releases?per_page=100"
        data = _http_get_json(url)
        if not isinstance(data, list):
            return []
        tags = [
            r.get("tag_name", "") for r in data
            if isinstance(r, dict) and not r.get("prerelease") and not r.get("draft")
        ]
        # Tag prefix policy: when a non-``v`` prefix is set, strip it
        # explicitly; when unset or ``v``, strip a leading ``v`` defensively.
        prefix = tag_prefix.strip()
        if prefix and prefix != "v":
            stripped = [t[len(prefix):] for t in tags if t.startswith(prefix)]
        else:
            stripped = [re.sub(r"^v", "", t) for t in tags]
        versions = [t for t in stripped if SEMVER_TRIPLET.match(t)]
    elif source == "docker-hub-tags":
        if not source_arg:
            return []
        url = (
            f"https://hub.docker.com/v2/repositories/{source_arg}"
            "/tags?page_size=100&ordering=last_updated"
        )
        data = _http_get_json(url)
        if not isinstance(data, dict):
            return []
        raw = [t.get("name", "") for t in data.get("results", []) or []]
        stripped = [re.sub(r"^v", "", t) for t in raw]
        versions = [t for t in stripped if SEMVER_TRIPLET.match(t)]
    else:
        return []

    major = major_pin.strip()
    if major:
        versions = [v for v in versions if v.startswith(major + ".")]
    return _semver_sort_desc(versions)


def fetch_latest_version_source(
    source: str, major_pin: str, source_arg: str = "", tag_prefix: str = "",
) -> str:
    """Top of the descending GA list, or ``""``."""
    versions = fetch_ga_versions_source(source, major_pin, source_arg, tag_prefix)
    return versions[0] if versions else ""


def fetch_latest_chart_version_gh(repo: str, name: str) -> str:
    """OCI chart-pin release tag shape: ``<chart-name>-X.Y.Z`` (bash's
    ``fetch_latest_chart_version_gh``).
    """
    if not repo or not name:
        return ""
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    data = _http_get_json(url)
    if not isinstance(data, list):
        return ""
    prefix = f"{name}-"
    versions: list[str] = []
    for r in data:
        if not isinstance(r, dict):
            continue
        if r.get("prerelease") or r.get("draft"):
            continue
        tag = r.get("tag_name", "") or ""
        if not tag.startswith(prefix):
            continue
        candidate = tag[len(prefix):]
        if SEMVER_TRIPLET.match(candidate):
            versions.append(candidate)
    versions = _semver_sort_desc(versions)
    return versions[0] if versions else ""


# =============================================================
# Container image existence probe (Docker Registry HTTP API v2 + bearer)
# =============================================================

_WWW_AUTH_REALM = re.compile(r'realm="([^"]*)"')
_WWW_AUTH_SERVICE = re.compile(r'service="([^"]*)"')
_WWW_AUTH_SCOPE = re.compile(r'scope="([^"]*)"')


def _registry_token(www_auth: str) -> str:
    """Parse a ``WWW-Authenticate: Bearer realm=...,service=...,scope=...``
    header and exchange it for an anonymous read token.
    """
    realm_m = _WWW_AUTH_REALM.search(www_auth)
    service_m = _WWW_AUTH_SERVICE.search(www_auth)
    scope_m = _WWW_AUTH_SCOPE.search(www_auth)
    if not realm_m:
        return ""
    realm = realm_m.group(1)
    params = {}
    if service_m:
        params["service"] = service_m.group(1)
    if scope_m:
        params["scope"] = scope_m.group(1)
    url = realm
    if params:
        url = f"{realm}?{urllib.parse.urlencode(params)}"
    body = _http_get_json(url)
    if not isinstance(body, dict):
        return ""
    return str(body.get("token", ""))


def verify_image_exists(image: str, tag: str) -> bool:
    """``True`` when ``<image>:<tag>`` resolves to a manifest in its registry.

    Two-pass flow mirrors the bash helper:

      1. HEAD the manifest URL anonymously.
      2. If the registry returns 401 with ``WWW-Authenticate: Bearer ...``,
         fetch the token and retry with ``Authorization: Bearer <token>``.

    Empty image or tag short-circuits to ``True`` (mirrors bash).
    """
    if not image or not tag:
        return True
    registry, _, repo = image.partition("/")
    manifest_url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    accept = "application/vnd.docker.distribution.manifest.v2+json"

    # Pass 1 — anonymous HEAD (matches bash ``curl -I``). Most registries
    # answer HEAD with the same auth challenge / status as GET but skip the
    # manifest body, saving bandwidth.
    req = urllib.request.Request(
        manifest_url, method="HEAD", headers={"Accept": accept},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return False
        www_auth = e.headers.get("WWW-Authenticate", "") if e.headers else ""
        token = _registry_token(www_auth)
        if not token:
            return False
        req2 = urllib.request.Request(
            manifest_url,
            method="HEAD",
            headers={
                "Accept": accept,
                "Authorization": f"Bearer {token}",
            },
        )
        try:
            with urllib.request.urlopen(req2, timeout=HTTP_TIMEOUT) as resp:
                return 200 <= resp.status < 300
        except (urllib.error.URLError, TimeoutError, OSError):
            return False
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def find_latest_available_source(
    source: str, major_pin: str, image: str,
    source_arg: str = "", tag_prefix: str = "",
) -> str:
    """Walk the descending GA list, returning the newest version that has
    a published image. Caps at :data:`IMAGE_PROBE_MAX_ATTEMPTS` so a
    regression at upstream doesn't make the script hang.
    """
    versions = fetch_ga_versions_source(source, major_pin, source_arg, tag_prefix)
    for i, v in enumerate(versions, start=1):
        if i > IMAGE_PROBE_MAX_ATTEMPTS:
            break
        if verify_image_exists(image, v):
            return v
    return ""

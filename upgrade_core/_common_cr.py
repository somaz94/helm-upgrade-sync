"""Shared helpers for Custom Resource (CR) version upgrade templates.

Used by:
  - ``local_cr_version`` — local CR wrapper chart, Chart.yaml + values
    upgrade with optional ``MIRROR_CHART_VERSION`` mirror.
  - ``external_oci_cr_version`` — external OCI chart consumer, Stack
    version + OCI chart pin two-track upgrade.

Extracted on the same two-consumer threshold that produced
``_common_helmfile.py``. Both consumers use the same:

  - Multi-source version fetching (elastic-artifacts / github-releases /
    docker-hub-tags) with image-availability fallback.
  - Pre-flight CR phase/health probe + dependency-CR version constraint.
  - Container image verification with Docker Registry v2 + bearer token
    flow.
  - 7-step kubectl webhook-rollback sequence (scale operator down →
    delete validating webhook → recover helm release from failed state →
    helmfile apply → recreate webhook → scale up → wait for CR Ready).

Public API (no ``_`` prefix) — these helpers are meant for cross-module
use. Module-private helpers carry the ``_`` prefix.

Stdlib only. All ``subprocess.run`` calls use ``check=False`` and
``capture_output=True`` so the surrounding template can degrade to
best-effort behavior when ``kubectl`` / ``helm`` / network are absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Re-exported for backward compatibility with ``local_cr_version`` / ``external_oci_cr_version`` import sites.
# The helpers were moved to ``_common`` in Phase 3 so non-CR templates
# (``ansible_github_release``) could drop their near-identical copies
# without taking a CR-domain dependency.
from ._common import read_yaml_value, update_yaml_value  # noqa: F401


# =============================================================
# Constants
# =============================================================

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")

# Default timeouts (seconds) for kubectl waits.
DEFAULT_CR_READY_TIMEOUT = 300
DEFAULT_OPERATOR_READY_TIMEOUT = 120

# Default cap on the image-availability fallback search.
IMAGE_PROBE_MAX_ATTEMPTS = 15

# HTTP timeout for all outbound REST calls.
HTTP_TIMEOUT = 10.0


# =============================================================
# HTTP + version fetching
# =============================================================

def http_get(url: str, *, timeout: float = HTTP_TIMEOUT) -> bytes:
    """Wrap :func:`urllib.request.urlopen` with a uniform error policy.

    Returns the raw response body on success, ``b""`` on any error
    (network / 4xx / 5xx). Mirrors bash ``curl -sSfL`` contract.
    Adds ``Authorization: Bearer $GITHUB_TOKEN`` when querying the
    GitHub Releases API and the env var is set.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "upgrade.py"})
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and "api.github.com" in url:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        # urllib.error.HTTPError is a subclass of URLError, so it's covered.
        return b""


def http_get_json(url: str, *, timeout: float = HTTP_TIMEOUT):
    """Return the parsed JSON body of ``url``, or ``None`` on any failure.

    Collapses the ``http_get`` -> empty-body guard -> ``json.loads`` ->
    ``JSONDecodeError`` guard chain that each version-source backend in
    :func:`fetch_ga_versions` repeated verbatim. Callers that need
    ``http_get`` itself as a test seam (``external_oci_cr_version``'s
    chart-pin fetcher patches it per-module) keep calling ``http_get``
    directly.
    """
    body = http_get(url, timeout=timeout)
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _semver_tags(tags: list[str]) -> list[str]:
    """Strip a leading ``v`` from each tag, keeping only semver-shaped ones."""
    stripped = [re.sub(r"^v", "", t) for t in tags]
    return [t for t in stripped if SEMVER_RE.match(t)]


def fetch_ga_versions(
    source: str, source_arg: str, major_pin: str
) -> list[str]:
    """Return GA versions sorted newest-first, respecting ``major_pin``.

    Three backends mirror the bash ``case "$VERSION_SOURCE"`` chain:

    - ``elastic-artifacts`` — Elastic Stack version feed. ``source_arg``
      is ignored.
    - ``github-releases`` — GitHub Releases API for ``<owner>/<repo>``.
      Strips ``v`` prefix, drops drafts and pre-releases.
    - ``docker-hub-tags`` — Docker Hub tags API for
      ``<namespace>/<repository>``. Strips ``v`` prefix.

    Unknown source → empty list.
    """
    if source == "elastic-artifacts":
        data = http_get_json("https://artifacts-api.elastic.co/v1/versions")
        if data is None:
            return []
        versions = [v for v in data.get("versions", []) if SEMVER_RE.match(v)]
    elif source == "github-releases":
        if not source_arg:
            return []
        data = http_get_json(
            f"https://api.github.com/repos/{source_arg}/releases?per_page=100"
        )
        if data is None:
            return []
        versions = _semver_tags([
            r.get("tag_name", "")
            for r in data
            if not r.get("prerelease") and not r.get("draft")
        ])
    elif source == "docker-hub-tags":
        if not source_arg:
            return []
        data = http_get_json(
            f"https://hub.docker.com/v2/repositories/{source_arg}/"
            "tags?page_size=100&ordering=last_updated"
        )
        if data is None:
            return []
        versions = _semver_tags([t.get("name", "") for t in data.get("results", [])])
    else:
        return []

    if major_pin:
        versions = [v for v in versions if v.startswith(f"{major_pin}.")]
    versions.sort(key=lambda v: tuple(int(p) for p in v.split(".")), reverse=True)
    return versions


def fetch_latest_version(
    source: str, source_arg: str, major_pin: str
) -> str:
    """First entry of :func:`fetch_ga_versions` or empty string."""
    versions = fetch_ga_versions(source, source_arg, major_pin)
    return versions[0] if versions else ""


def find_latest_available_version(
    source: str, source_arg: str, major_pin: str, container_image: str
) -> str:
    """Walk GA versions newest-first, return first with a published image.

    Limit :data:`IMAGE_PROBE_MAX_ATTEMPTS` probes, log each result to
    stderr. Returns empty string if no version within the cap has a
    published image.
    """
    attempt = 0
    for ver in fetch_ga_versions(source, source_arg, major_pin):
        attempt += 1
        if attempt > IMAGE_PROBE_MAX_ATTEMPTS:
            print(
                f"    (stopped after {IMAGE_PROBE_MAX_ATTEMPTS} attempts)",
                file=sys.stderr,
            )
            return ""
        if verify_image_exists(container_image, ver):
            print(f"    {ver}: available", file=sys.stderr)
            return ver
        print(f"    {ver}: not found", file=sys.stderr)
    return ""


def semver_compare(a: str, b: str) -> int:
    """Return -1 / 0 / 1 for semver tuple compare.

    Both inputs are expected to match :data:`SEMVER_RE`.
    """
    ta = tuple(int(p) for p in a.split("."))
    tb = tuple(int(p) for p in b.split("."))
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


# =============================================================
# kubectl helpers
# =============================================================

def kubectl_available() -> bool:
    """Return True if ``kubectl`` is on PATH."""
    return shutil.which("kubectl") is not None


def kubectl_jsonpath(ns: str, kind: str, name: str, path: str) -> str:
    """Return ``kubectl get <kind> <name> -n <ns> -o jsonpath=<path>`` stdout.

    ``path`` is the JSONPath body (e.g. ``.status.phase``) — the
    surrounding ``{...}`` braces are added by this helper. Always
    ``check=False`` + ``capture_output=True``. Empty string on any
    error.
    """
    rc = subprocess.run(
        [
            "kubectl", "-n", ns, "get", kind, name,
            "-o", f"jsonpath={{{path}}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return rc.stdout.strip()


def read_helmfile_namespace(helmfile_path: Path | None) -> str:
    """Read the first ``namespace:`` value from a helmfile.

    Used by both ``local_cr_version`` and ``external_oci_cr_version`` CR helpers. Looks for an indented
    ``namespace:`` (release-level) — the bash original used
    ``awk '/namespace:/ {print $2; exit}'`` which is more lenient but
    we tighten to the indented form here to skip any mid-line match
    inside YAML strings. Returns empty string when helmfile is missing.
    """
    if helmfile_path is None or not helmfile_path.is_file():
        return ""
    for raw in helmfile_path.read_text().splitlines():
        m = re.match(r"\s*namespace:\s+(\S+)", raw)
        if m:
            return m.group(1).strip("\"'")
    return ""


def check_cluster_health(
    component_label: str, helmfile_path: Path | None
) -> bool:
    """Run the pre-flight CR phase/health probe.

    Returns False only when the CR is in a hard-abort state (transient
    reconcile, ``Invalid`` spec, or ``red`` health). Missing kubectl,
    missing namespace, missing CR, or empty phase = passthrough (True).
    """
    if not kubectl_available():
        print("  Skipped (kubectl not available).")
        return True
    if not component_label:
        return True
    ns = read_helmfile_namespace(helmfile_path)
    if not ns:
        print("  Skipped (namespace not readable from helmfile).")
        return True
    # First check CR existence — first install case.
    rc = subprocess.run(
        ["kubectl", "-n", ns, "get", component_label, component_label],
        capture_output=True,
        check=False,
    )
    if rc.returncode != 0:
        print(f"  CR not found in ns/{ns} (first install?). Skipping health check.")
        return True
    phase = kubectl_jsonpath(ns, component_label, component_label, ".status.phase")
    health = kubectl_jsonpath(ns, component_label, component_label, ".status.health")
    print(f"  CR phase:  {phase or 'unknown'}")
    print(f"  CR health: {health or 'unknown'}")
    abort = False
    if phase in ("ApplyingChanges", "MigratingData", "ChangingStackVersion"):
        print(
            f"  ERROR: CR is in transient state '{phase}'. A reconcile may be in progress."
        )
        abort = True
    elif phase == "Invalid":
        print("  ERROR: CR is in 'Invalid' state. Fix the CR before upgrading.")
        abort = True
    elif phase == "Ready":
        pass
    elif phase == "":
        print("  WARN: CR phase is empty. Proceed with caution.")
    else:
        print(f"  WARN: CR phase '{phase}' is not 'Ready'. Proceed with caution.")
    if health == "red":
        print("  ERROR: CR health is 'red' (data loss risk). Fix cluster before upgrading.")
        abort = True
    return not abort


def check_dependency_version(
    target: str,
    dep_kind: str,
    dep_name: str,
    helmfile_path: Path | None,
    component_label: str,
) -> bool:
    """Ensure ``target <= <dep_kind>.spec.version``.

    Used by Kibana (depends on Elasticsearch). Empty dep_kind/name →
    noop (True). Missing kubectl or unreachable dep CR → noop (True).
    """
    if not dep_kind or not dep_name:
        return True
    if not kubectl_available():
        return True
    ns = read_helmfile_namespace(helmfile_path)
    if not ns:
        return True
    dep_ver = kubectl_jsonpath(ns, dep_kind, dep_name, ".spec.version")
    if not dep_ver:
        return True
    print(f"  Dependency {dep_kind}/{dep_name} version: {dep_ver}")
    cmp = semver_compare(target, dep_ver)
    if cmp == 1:
        print()
        print(
            f"  ERROR: target version {target} is HIGHER than {dep_kind} version {dep_ver}."
        )
        print(
            f"  {component_label} must be <= {dep_kind} version (otherwise connection fails)."
        )
        print(f"  Upgrade {dep_kind} first, then retry.")
        return False
    print(f"  OK ({target} <= {dep_ver}).")
    return True


def wait_for_cr_ready(
    component_label: str,
    helmfile_path: Path | None,
    timeout: int = DEFAULT_CR_READY_TIMEOUT,
) -> bool:
    """Block until ``status.phase == "Ready"`` or timeout.

    Polls every 5s. Returns False on timeout (only WARN, not fatal).
    True on success or kubectl missing (best-effort).
    """
    if not kubectl_available() or not component_label:
        return True
    ns = read_helmfile_namespace(helmfile_path)
    if not ns:
        return True
    print(
        f"    Waiting up to {timeout}s for {component_label} CR to reach Ready..."
    )
    interval = 5
    elapsed = 0
    phase = ""
    while elapsed < timeout:
        phase = kubectl_jsonpath(ns, component_label, component_label, ".status.phase")
        if phase == "Ready":
            print(f"    CR phase=Ready after {elapsed}s.")
            return True
        time.sleep(interval)
        elapsed += interval
    print(
        f"    WARN: CR did not reach Ready within {timeout}s "
        f"(current phase: {phase or 'unknown'})."
    )
    print(f"    Investigate: kubectl -n {ns} describe {component_label} {component_label}")
    return False


def wait_for_operator_ready(
    operator_ns: str,
    operator_sts: str,
    timeout: int = DEFAULT_OPERATOR_READY_TIMEOUT,
) -> bool:
    """Block until ``pod/<sts>-0`` reaches condition=Ready or timeout."""
    if not operator_ns or not operator_sts:
        return True
    if not kubectl_available():
        return True
    print(f"    Waiting up to {timeout}s for operator pod to become Ready...")
    rc = subprocess.run(
        [
            "kubectl", "-n", operator_ns, "wait", "--for=condition=Ready",
            f"pod/{operator_sts}-0", f"--timeout={timeout}s",
        ],
        capture_output=True,
        check=False,
    )
    if rc.returncode != 0:
        print(f"    WARN: operator pod did not become Ready within {timeout}s.")
        return False
    return True


def get_live_cr_version(
    component_label: str, helmfile_path: Path | None
) -> str:
    """Best-effort fetch of live ``.spec.version`` from the running CR.

    Returns empty string when kubectl, namespace, or the CR is missing.
    """
    if not kubectl_available() or not component_label:
        return ""
    ns = read_helmfile_namespace(helmfile_path)
    if not ns:
        return ""
    return kubectl_jsonpath(ns, component_label, component_label, ".spec.version")


# =============================================================
# Helm helpers (release status / history JSON parsing)
# =============================================================

def read_helm_release_status(release_name: str, release_ns: str) -> str:
    """Return ``helm status -o json`` ``info.status`` or empty string."""
    rc = subprocess.run(
        ["helm", "status", release_name, "-n", release_ns, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rc.returncode != 0 or not rc.stdout.strip():
        return ""
    try:
        data = json.loads(rc.stdout)
    except json.JSONDecodeError:
        return ""
    return data.get("info", {}).get("status", "")


def read_last_good_revision(release_name: str, release_ns: str) -> str:
    """Scan ``helm history -o json`` for the newest deployed/superseded rev."""
    rc = subprocess.run(
        ["helm", "history", release_name, "-n", release_ns, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if rc.returncode != 0 or not rc.stdout.strip():
        return ""
    try:
        history = json.loads(rc.stdout)
    except json.JSONDecodeError:
        return ""
    good = [r for r in history if r.get("status") in ("deployed", "superseded")]
    if not good:
        return ""
    good.sort(key=lambda r: r.get("revision", 0), reverse=True)
    return str(good[0].get("revision", ""))


# =============================================================
# Helmfile helpers
# =============================================================

def read_helmfile_release_metadata(
    helmfile_path: Path | None,
) -> tuple[str, str]:
    """Read ``(release_name, release_namespace)`` from helmfile.

    First ``- name:`` line wins for the release name (skipping templated
    `{{ ... }}` values). Namespace via :func:`read_helmfile_namespace`.
    """
    if helmfile_path is None or not helmfile_path.is_file():
        return "", ""
    release_name = ""
    for raw in helmfile_path.read_text().splitlines():
        m = re.match(r"^\s*-\s+name:\s+(\S+)", raw)
        if m and "{{" not in raw:
            release_name = m.group(1).strip("\"'")
            break
    release_ns = read_helmfile_namespace(helmfile_path)
    return release_name, release_ns


# =============================================================
# Image verification (Docker Registry v2 + bearer token flow)
# =============================================================

def verify_image_exists(container_image: str, tag: str) -> bool:
    """Probe ``HEAD /v2/<repo>/manifests/<tag>`` honoring bearer challenge.

    For registries that return ``WWW-Authenticate: Bearer realm=...
    service=... scope=...`` we fetch a token from realm and retry.
    Empty image or tag → True (skip). Returns True iff the manifest
    endpoint returns 200.
    """
    if not container_image or not tag:
        return True
    registry, _, repo = container_image.partition("/")
    if not repo:
        return False
    manifest_url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    accept_v2 = "application/vnd.docker.distribution.manifest.v2+json"

    req = urllib.request.Request(manifest_url, method="GET")
    req.add_header("Accept", accept_v2)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return False
        auth_header = e.headers.get("WWW-Authenticate", "")
    except (urllib.error.URLError, TimeoutError, OSError):
        return False

    realm_m = re.search(r'realm="([^"]+)"', auth_header)
    service_m = re.search(r'service="([^"]+)"', auth_header)
    scope_m = re.search(r'scope="([^"]+)"', auth_header)
    if realm_m is None:
        return False
    token_url = realm_m.group(1)
    params: list[str] = []
    if service_m:
        params.append(f"service={service_m.group(1)}")
    if scope_m:
        params.append(f"scope={scope_m.group(1)}")
    if params:
        token_url += "?" + "&".join(params)

    token_body = http_get(token_url)
    if not token_body:
        return False
    try:
        token = json.loads(token_body).get("token", "")
    except json.JSONDecodeError:
        return False
    if not token:
        return False

    auth_req = urllib.request.Request(manifest_url, method="GET")
    auth_req.add_header("Accept", accept_v2)
    auth_req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(auth_req, timeout=HTTP_TIMEOUT) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# =============================================================
# Step 4 (image verify + fallback) — shared by ``local_cr_version`` / ``external_oci_cr_version``
# =============================================================

@dataclass(frozen=True)
class ImageVerifyOutcome:
    """Outcome of :func:`verify_image_with_fallback`.

    Mapping back to the caller's exit code:

    - ``proceed=True``  → caller continues the upgrade flow, using
      ``effective_version`` (may differ from the originally-requested
      ``latest_version`` when the fallback search picked an earlier GA
      with a published image).
    - ``proceed=False`` → caller returns ``exit_code`` from ``run()``.
      ``exit_code=0`` is "nothing to do" (e.g. fallback matched the
      currently-installed version), ``exit_code=1`` is abort (user
      declined, no published image found within search limit, or
      ``--version`` was pinned to an unpublished tag).
    """

    proceed: bool
    effective_version: str = ""
    exit_code: int = 0


def verify_image_with_fallback(
    container_image: str,
    latest_version: str,
    current_version: str,
    version_source: str,
    version_source_arg: str,
    major_pin: str,
    target_version: str,
    dry_run: bool,
) -> ImageVerifyOutcome:
    """Step 4: verify the container image exists; fall back to the newest
    GA version that has a published image when the requested tag is
    missing.

    Extracted from the byte-for-byte identical Step 4 blocks in
    ``local_cr_version`` and ``external_oci_cr_version``. Output
    (stdout) is preserved verbatim.
    """
    print()
    print("[Step 4/7] Verifying container image...")
    if not container_image:
        print("  Skipped (CONTAINER_IMAGE not configured).")
        return ImageVerifyOutcome(proceed=True, effective_version=latest_version)

    print(f"  Checking: {container_image}:{latest_version}")
    if verify_image_exists(container_image, latest_version):
        print("  Image verified OK.")
        return ImageVerifyOutcome(proceed=True, effective_version=latest_version)

    print()
    print("  WARNING: Container image not found in registry.")
    print(f"    Image: {container_image}:{latest_version}")
    print()
    print(f"  The version {latest_version} is listed in the upstream feed but the")
    print("  container image has not been published yet.")
    if target_version:
        print()
        print("  Options:")
        print("    - Wait for the image to be published and retry.")
        print("    - Choose a different version.")
        return ImageVerifyOutcome(proceed=False, exit_code=1)
    print()
    print("  Searching for the newest GA version with a published image...")
    available_version = find_latest_available_version(
        version_source, version_source_arg, major_pin, container_image
    )
    if not available_version:
        print()
        print("  ERROR: No GA version with a published image found within search limit.")
        print("  Retry later or check the release notes manually.")
        return ImageVerifyOutcome(proceed=False, exit_code=1)
    if available_version == current_version:
        print()
        print(
            f"  Newest available version ({available_version}) matches the current version."
        )
        print(f"  Nothing to upgrade. Retry when {latest_version} image is published.")
        return ImageVerifyOutcome(proceed=False, exit_code=0)
    print()
    print(f"  Latest available (with published image): {available_version}")
    if dry_run:
        print()
        print(f"  [DRY-RUN] Would prompt to switch to {available_version}.")
        print(f"  To apply: ./upgrade.py --version {available_version}")
        return ImageVerifyOutcome(proceed=True, effective_version=available_version)
    print()
    use_alt = input(
        f"  Use {available_version} instead of {latest_version}? [y/N]: "
    ).strip()
    if not use_alt.lower().startswith("y"):
        print()
        print("  Aborted. To apply manually:")
        print(f"    ./upgrade.py --version {available_version}")
        return ImageVerifyOutcome(proceed=False, exit_code=1)
    print(f"  Proceeding with {available_version}.")
    return ImageVerifyOutcome(proceed=True, effective_version=available_version)


# =============================================================
# CR rollback (7-step kubectl flow with operator webhook bypass)
# =============================================================

def rollback_with_webhook_handling(
    config: dict, chart_dir: Path, helmfile_path: Path | None
) -> None:
    """Run the 7-step kubectl flow for CR-downgrade rollback.

    Bash counterpart: ``rollback_with_webhook_handling``. Steps:
      1. Scale operator StatefulSet down (replicas=0), wait for pod delete.
      2. Delete the admission webhook ValidatingWebhookConfiguration.
      3. If the helm release is ``status=failed``, ``helm rollback`` to
         the last good revision so ``helmfile apply`` sees the diff.
      4. ``helmfile apply`` from CHART_DIR.
      5. Recreate the webhook by ``helmfile sync`` in the operator
         chart directory (searched as an ancestor of CHART_DIR).
      6. Scale operator back up (replicas=1) and wait for Ready.
      7. Wait for the CR to reach phase=Ready.

    Reads the operator + webhook + chart dir from ``config`` so the
    same helper drives both ``local_cr_version`` and ``external_oci_cr_version`` rollback paths.
    """
    release_name, release_ns = read_helmfile_release_metadata(helmfile_path)
    operator_ns = config["CR_OPERATOR_NS"]
    operator_sts = config["CR_OPERATOR_STS"]
    webhook = config["CR_WEBHOOK_NAME"]
    component_label = config["COMPONENT_LABEL"]
    operator_chart_dir = config.get("CR_OPERATOR_CHART_DIR", "")

    print()
    print(f"  [1/7] Scaling down operator ({operator_ns}/{operator_sts})...")
    subprocess.run(
        ["kubectl", "-n", operator_ns, "scale", "statefulset", operator_sts, "--replicas=0"],
        check=False,
    )
    subprocess.run(
        [
            "kubectl", "-n", operator_ns, "wait", "--for=delete",
            f"pod/{operator_sts}-0", "--timeout=60s",
        ],
        capture_output=True,
        check=False,
    )

    print(f"  [2/7] Removing admission webhook ({webhook})...")
    subprocess.run(
        ["kubectl", "delete", "validatingwebhookconfiguration", webhook, "--ignore-not-found"],
        check=False,
    )

    print("  [3/7] Recovering Helm release state...")
    if release_name and release_ns:
        status = read_helm_release_status(release_name, release_ns)
        if status == "failed":
            print(
                f"    Helm release '{release_name}' is in 'failed' state. "
                "Rolling back to last successful revision..."
            )
            last_good = read_last_good_revision(release_name, release_ns)
            if last_good:
                subprocess.run(
                    ["helm", "rollback", release_name, last_good, "-n", release_ns],
                    check=False,
                )
                print(f"    Rolled back to revision {last_good}.")
            else:
                print("    WARN: no successful revision found. Proceeding with helmfile apply.")
        else:
            print(f"    Helm release is clean (status: {status or 'unknown'}).")
    else:
        print("    WARN: could not read release info from helmfile. Skipping Helm recovery.")

    print("  [4/7] Applying rollback via helmfile...")
    subprocess.run(["helmfile", "apply"], cwd=chart_dir, check=False)

    print("  [5/7] Recreating webhook via operator helmfile sync...")
    operator_dir = ""
    if operator_chart_dir:
        search_base = chart_dir.resolve()
        while True:
            candidate = search_base / operator_chart_dir
            if (
                (candidate / "helmfile.yaml").is_file()
                or (candidate / "helmfile.yaml.gotmpl").is_file()
            ):
                operator_dir = str(candidate)
                break
            if search_base.parent == search_base:
                break
            search_base = search_base.parent
    if operator_dir:
        subprocess.run(["helmfile", "sync"], cwd=Path(operator_dir), check=False)
    elif operator_chart_dir:
        print(
            f"    WARN: operator chart dir '{operator_chart_dir}' not found. "
            "Recreate the webhook manually."
        )
    else:
        print("    SKIP: CR_OPERATOR_CHART_DIR not configured. Recreate the webhook manually.")

    print("  [6/7] Scaling operator back up and waiting for Ready...")
    subprocess.run(
        ["kubectl", "-n", operator_ns, "scale", "statefulset", operator_sts, "--replicas=1"],
        check=False,
    )
    wait_for_operator_ready(operator_ns, operator_sts, DEFAULT_OPERATOR_READY_TIMEOUT)

    print("  [7/7] Waiting for CR to reach Ready state...")
    wait_for_cr_ready(component_label, helmfile_path, DEFAULT_CR_READY_TIMEOUT)

    print()
    print("Rollback complete! Cluster CR has been restored to the backup version.")
    print(f"Final status: kubectl -n <ns> get {component_label}")


def handle_downgrade_rollback(
    config: dict,
    chart_dir: Path,
    helmfile_path: Path | None,
    live_ver: str,
    backup_ver: str,
    *,
    operator_chart_label: str = "operator-dir",
) -> None:
    """Print the CR downgrade warning + offer auto-webhook rollback + emit
    the 7-step manual instructions when auto is declined or config-incomplete.

    Shared by ``local_cr_version`` and ``external_oci_cr_version``
    — both consumers print the identical block aside from the step-5 chart
    directory placeholder (``operator-dir`` vs ``eck-operator-dir``).

    ``operator_chart_label`` is the bare-word placeholder rendered inside
    ``<...>`` on step 5 (``cd <{operator_chart_label}> && helmfile sync``).
    """
    print()
    print(f"  WARNING: This is a version downgrade ({live_ver} -> {backup_ver}).")
    print("  Operator admission webhooks typically block CR version downgrades.")
    if (
        config.get("CR_WEBHOOK_NAME")
        and config.get("CR_OPERATOR_NS")
        and config.get("CR_OPERATOR_STS")
    ):
        print()
        auto_apply = input("  Automatically handle the webhook and apply rollback? [y/N]: ").strip()
        if auto_apply.lower().startswith("y"):
            rollback_with_webhook_handling(config, chart_dir, helmfile_path)
            return
    print()
    print("  To apply this rollback manually:")
    print(f"    1. kubectl -n {config.get('CR_OPERATOR_NS', '')} scale sts {config.get('CR_OPERATOR_STS', '')} --replicas=0")
    print(f"    2. kubectl delete validatingwebhookconfiguration {config.get('CR_WEBHOOK_NAME', '')} --ignore-not-found")
    print("    3. If 'helm list -n <ns>' shows status=failed:")
    print("         helm rollback <release> <last-good-revision> -n <ns>")
    print("    4. helmfile apply")
    print(f"    5. Recreate webhook: cd <{operator_chart_label}> && helmfile sync")
    print(f"    6. kubectl -n {config.get('CR_OPERATOR_NS', '')} scale sts {config.get('CR_OPERATOR_STS', '')} --replicas=1")
    print(
        f"    7. Wait for CR: kubectl -n <ns> wait {config['COMPONENT_LABEL']}/"
        f"{config['COMPONENT_LABEL']} --for=jsonpath='{{.status.phase}}'=Ready --timeout=300s"
    )

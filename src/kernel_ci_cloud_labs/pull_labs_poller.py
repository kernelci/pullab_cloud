# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# Author: Denys Fedoryshchenko <denys.f@collabora.com>
# Co-Author: Max Hubmann <mxhbm@amazon.de>
# Co-Author: Norbert Manthey <nmanthey@amazon.de>
#
# Note: _default_job_executor() mirrors the registry-based instantiation
# pattern from kernel_ci_cloud_labs/main.py (Amazon-authored).

"""Pull-lab poller — bridge between kernelci-api and pullab_cloud.

Long-lived service (or one-shot job) that:
  1. Polls kernelci-api /events for new pull-lab jobs.
  2. Claims each job node (state=running) so other pollers skip it.
  3. Fetches each job's PULL_LABS job_definition JSON.
  4. Translates it into a pullab_cloud run config and runs the pipeline.
  5. Submits per-test results directly to KCIDB.
  6. Marks the job node done in kernelci-api (state=done + result, plus
     data.error_code / data.error_msg on an infrastructure failure).

Generic Python only — uses stdlib urllib for HTTP, supports env-var and
config-file configuration, can be invoked as a CLI, a long-running
container loop, or a Lambda handler (see `lambda_handler`).

Reference for the polling pattern: kernelci-pipeline/tools/example_pull_lab.py.
Reference for the KernelCI events API path:
  GET {api_base_uri}/events?state=available&kind=job&recursive=true&from=<ts>
"""

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from kernel_ci_cloud_labs.kcidb_submit import (
    build_test_row,
    submit_tests,
    to_kcidb_status,
)
from kernel_ci_cloud_labs.pull_labs_translate import translate_job

logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SEC = 30
DEFAULT_FROM_TIMESTAMP = "1970-01-01T00:00:00.000000"
DEFAULT_CURSOR_FILE = "/tmp/pullab_cloud_cursor.json"  # nosec B108

# Environment variable names (all optional — fall back to config.json values).
ENV_API_BASE_URI = "KERNELCI_API_BASE_URI"
ENV_API_TOKEN = "KERNELCI_API_TOKEN"
ENV_RUNTIME_NAME = "KERNELCI_RUNTIME_NAME"
# Optional comma-separated allowlist of node.data.platform values. When set,
# events whose platform isn't in the list are skipped — useful for running
# parallel pollers on the same runtime label but distinct hardware.
ENV_PLATFORMS = "KERNELCI_PLATFORMS"
ENV_KCIDB_URL = "KCIDB_SUBMIT_URL"
ENV_KCIDB_JWT = "KCIDB_JWT"
ENV_KCIDB_ORIGIN = "KCIDB_ORIGIN"
# kci-dev compatibility: single env var carrying both URL and token in the form
# https://<token>@<host>[:<port>][/path][/submit]. Used when KCIDB_SUBMIT_URL /
# KCIDB_JWT are not both set.
ENV_KCIDB_REST = "KCIDB_REST"
# Shared fallback when the KernelCI API token and the KCIDB JWT are the same
# value (common in single-credential deployments). Lower priority than the
# dedicated env vars but higher than config-file values.
ENV_UNIFIED_TOKEN = "UNIFIED_TOKEN"
ENV_CURSOR_FILE = "PULLAB_CURSOR_FILE"
ENV_POLL_INTERVAL = "PULLAB_POLL_INTERVAL_SEC"
ENV_BASE_CONFIG = "PULLAB_BASE_CONFIG"


# System and read-only database-managed fields on node objects.
# These fields are omitted when updating a node (PUT /node/<id>) to prevent
# FastAPI/Pydantic validation errors (HTTP 400 Bad Request / 422 Unprocessable Entity)
# since they are read-only and not accepted in the update schema.
NODE_READ_ONLY_FIELDS = {
    "id",
    "_id",
    "created",
    "updated",
    "user",
    "user_groups",
    "owner",
    "submitter",
    "treeid",
    "processed_by_kcidb_bridge",
    "retry_counter",
    "timeout",
}


def _parse_kcidb_rest(env_value: str) -> Tuple[Optional[str], Optional[str]]:
    """Parse a KCIDB_REST URL of the form https://<token>@<host>[/path].

    Returns (submit_url, token). Mirrors kci-dev's
    kcidev.libs.kcidb._parse_kcidb_rest_env so operators with a kci-dev
    configuration can reuse the same env var. Returns (None, None) if the
    value cannot be parsed or carries no token.
    """
    if not env_value:
        return None, None
    parsed = urllib.parse.urlparse(env_value)
    token = parsed.username
    if not token:
        return None, None
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    if not path.endswith("/submit"):
        path = path.rstrip("/") + "/submit"
    clean = urllib.parse.urlunparse(
        (parsed.scheme, host, path, parsed.params, parsed.query, parsed.fragment)
    )
    return clean, token


# ---------------------------------------------------------------------------
# HTTP helpers — stdlib only.
# ---------------------------------------------------------------------------


def _http_get_json(url: str, token: Optional[str] = None, timeout: float = 30.0) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error("HTTP GET to %s failed (HTTP %s): %s. Response body: %s", url, e.code, e.reason, err_body)
        except Exception:
            pass
        raise


def _http_put_json(
    url: str,
    payload: Any,
    token: Optional[str] = None,
    timeout: float = 30.0,
) -> Any:
    """PUT a JSON body; return the parsed JSON response (or None).

    Raises urllib.error.URLError / HTTPError on transport or HTTP errors,
    mirroring _http_get_json so callers can handle both with one except.
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return json.loads(resp_body) if resp_body else None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")
            logger.error("HTTP PUT to %s failed (HTTP %s): %s. Response body: %s", url, e.code, e.reason, err_body)
        except Exception:
            pass
        raise


def _validate_api_token(
    api_base_uri: str, api_token: Optional[str], runtime_name: str
) -> None:
    """Startup preflight: confirm api_token authenticates and can edit nodes.

    Calls GET /whoami once and logs the outcome. Never fatal -- a transient
    API error must not stop the poller from starting, and node updates have
    their own per-call error handling. It surfaces, at startup, the two
    failure modes that otherwise only show up as a 401 on every job:
      * the token does not authenticate at all (no token / invalid / expired);
      * the token authenticates but the user cannot edit job nodes for this
        runtime (kernelci-api _user_can_edit_node).
    """
    if not api_token:
        logger.warning(
            "No kernelci-api token set (KERNELCI_API_TOKEN / UNIFIED_TOKEN / "
            "config kernelci.api_token) -- node claim/finish updates will "
            "fail with HTTP 401"
        )
        return

    url = f"{api_base_uri.rstrip('/')}/whoami"
    try:
        whoami = _http_get_json(url, token=api_token) or {}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            logger.error(
                "kernelci-api token rejected by %s (HTTP %s) -- the token is "
                "invalid, expired, or not a kernelci-api token; node updates "
                "will fail",
                url, e.code,
            )
        else:
            logger.warning(
                "Could not validate kernelci-api token via %s: HTTP %s",
                url, e.code,
            )
        return
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        logger.warning(
            "Could not reach %s to validate the kernelci-api token (%s) -- "
            "continuing; node updates will be retried per job",
            url, e,
        )
        return

    username = whoami.get("username") or whoami.get("email") or "<unknown>"
    is_superuser = bool(whoami.get("is_superuser"))
    groups = {
        g.get("name")
        for g in whoami.get("groups", [])
        if isinstance(g, dict) and g.get("name")
    }
    # Groups that let a user edit a job node it does not own
    # (kernelci-api _user_can_edit_node).
    editor_groups = {
        "node:edit:any",
        f"runtime:{runtime_name}:node-editor",
        f"runtime:{runtime_name}:node-admin",
    }
    logger.info(
        "kernelci-api token OK: user=%s superuser=%s groups=%s",
        username, is_superuser, sorted(groups) or [],
    )
    if not is_superuser and not (groups & editor_groups):
        logger.warning(
            "kernelci-api user %s cannot edit job nodes for runtime '%s': "
            "not a superuser and in none of %s -- node claim/finish updates "
            "will fail with HTTP 401 unless the user owns the nodes. Add the "
            "user to group 'runtime:%s:node-editor'.",
            username, runtime_name, sorted(editor_groups), runtime_name,
        )


# ---------------------------------------------------------------------------
# Cursor persistence — generic filesystem backend by default.
# A deployment can swap in a custom CursorStore (e.g. backed by S3) by
# constructing PullLabsPoller(cursor_store=...).
# ---------------------------------------------------------------------------


class FileCursorStore:
    """Persist the polling cursor as a JSON file on the local filesystem.

    Suitable for: local dev, container deployments with a persistent volume,
    Lambda with /tmp (per-instance, accepts that warm starts share it).
    """

    def __init__(self, path: str = DEFAULT_CURSOR_FILE):
        self.path = path

    def read(self) -> str:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("timestamp", DEFAULT_FROM_TIMESTAMP)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return DEFAULT_FROM_TIMESTAMP

    def write(self, timestamp: str) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"timestamp": timestamp}, f)


# ---------------------------------------------------------------------------
# Pluggable job executor. Default invokes the existing AWS pipeline via the
# registry pattern; tests / non-AWS deployments can pass their own callable.
# Return value: (per_test_results, optional_log_url) where per_test_results
# is a list of dicts with at least {"name": str, "status": str}.
# ---------------------------------------------------------------------------


JobExecutor = Callable[[Dict[str, Any]], Tuple[List[Dict[str, Any]], Optional[str]]]


_DEFAULT_EXECUTOR_PACKAGES = (
    "kernel_ci_cloud_labs.providers",
    "kernel_ci_cloud_labs.storage",
    "kernel_ci_cloud_labs.auth",
)


def _validate_default_executor_deps() -> None:
    """Eagerly import everything the default executor will need.

    Called from PullLabsPoller.__init__ when no custom job_executor is
    supplied, so a missing runtime dep (boto3, an un-installed package)
    surfaces at startup instead of on the first event hours later. Raises
    SystemExit with a single combined message listing every problem found.
    """
    from kernel_ci_cloud_labs.main import import_all_packages  # noqa: PLC0415

    problems: List[str] = []
    try:
        import boto3  # noqa: F401,PLC0415
    except ImportError as e:
        problems.append(
            f"boto3 import failed ({e}) — run: python3.11 -m pip install -e ."
        )

    for pkg in _DEFAULT_EXECUTOR_PACKAGES:
        try:
            import_all_packages(pkg)
        except ImportError as e:
            problems.append(f"{pkg} import failed: {e}")

    if problems:
        raise SystemExit(
            "Default job executor dependencies are not installed:\n  - "
            + "\n  - ".join(problems)
            + "\nFix the install on this host, or pass a custom job_executor "
              "to PullLabsPoller if you don't need the AWS pipeline."
        )


def _default_job_executor(run_config: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Invoke the existing pipeline (provider-pluggable via registry).

    This is the only function in this module that knows about the rest of
    pullab_cloud — kept as a default so a poller running outside the bundled
    pipeline (custom executor, mock, etc.) can swap it out cleanly.
    """
    # Lazy import: avoids forcing the (AWS-coded) pipeline modules on consumers
    # who only want translate / kcidb_submit / poll.
    from kernel_ci_cloud_labs.core.pipeline import run_pipeline  # noqa: PLC0415
    from kernel_ci_cloud_labs.core.registry import (  # noqa: PLC0415
        AUTH_REGISTRY,
        PROVIDER_REGISTRY,
        STORAGE_REGISTRY,
    )
    from kernel_ci_cloud_labs.main import import_all_packages  # noqa: PLC0415

    for pkg in _DEFAULT_EXECUTOR_PACKAGES:
        import_all_packages(pkg)

    auth_class = AUTH_REGISTRY[run_config["auth_credentials"]["auth_provider"]]
    provider_class = PROVIDER_REGISTRY[run_config["provider"]]
    storage_class = STORAGE_REGISTRY[run_config["storage"]["type"]]
    auth = auth_class(run_config, None)
    storage_config = {
        **run_config["storage"],
        "region": run_config.get("region"),
        "external_storage": run_config.get("external_storage", {}),
    }
    storage = storage_class(storage_config, auth)
    provider = provider_class(auth, run_config, storage)

    summary = run_pipeline(provider, storage)
    return _extract_test_results(summary or {})


# Pipeline/PULL_LABS test names that denote a kernel boot test. The dashboard
# classifies a KCIDB test as a "boot" (rather than a generic test) only when
# its path is exactly "boot" or starts with "boot." -- see is_boot() in the
# kernelci-dashboard backend (kernelCI_app/utils.py). Every pullab_cloud job is
# a url-kernel-boot job, so these names are remapped to the "boot" path on
# submission. "baseline" is the PULL_LABS test type; "url-kernel-boot" is the
# vm-tests directory name it translates to and which appears in pipeline logs.
_BOOT_TEST_NAMES = frozenset({"baseline", "url-kernel-boot", "boot"})


def _test_name_to_path(name: str) -> str:
    """Map a pipeline test name to a KCIDB test path.

    Boot tests are remapped to the "boot" path so the dashboard classifies
    them as boots; every other name passes through unchanged (build_test_row
    then verifies it is a KCIDB-valid path and raises if it is not).
    """
    return "boot" if name.strip().lower() in _BOOT_TEST_NAMES else name


def _extract_test_results(summary: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Pull per-test status out of the summary dict returned by run_pipeline.

    The summary shape is owned by core/pipeline.create_summary(); this
    helper isolates the dependency on its exact field names.
    """
    rows: List[Dict[str, Any]] = []
    vms = summary.get("vms", {}) or {}
    test_names = vms.get("test_names") or []
    failed_by_test = vms.get("failed_by_test") or {}
    for name in test_names:
        status = "FAIL" if failed_by_test.get(name) else "PASS"
        rows.append({"name": _test_name_to_path(name), "status": status})
    return rows, None


def _node_result_from_rows(test_rows: List[Dict[str, Any]]) -> str:
    """Derive a kernelci-api node result for a job that actually ran.

    "incomplete" is reserved for infrastructure failures and is decided by
    the caller -- this never returns it. Any non-passing test status
    (FAIL/ERROR/MISS) fails the node.
    """
    statuses = {row.get("status") for row in test_rows}
    if statuses & {"FAIL", "ERROR", "MISS"}:
        return "fail"
    if statuses & {"PASS", "DONE"}:
        return "pass"
    if "SKIP" in statuses:
        return "skip"
    return "fail"


# kernelci-api Node.data.error_code values (a subset of the kernelci
# ErrorCodes enum in kernelci/api/models.py). Per that enum's docstring,
# error_code is set when an infrastructure error occurs; "Infrastructure"
# is the generic catch-all and "invalid_job_params" flags a bad job.
_ERR_INFRASTRUCTURE = "Infrastructure"
_ERR_INVALID_JOB_PARAMS = "invalid_job_params"


@dataclass
class NodeOutcome:
    """How a job node should be finished in kernelci-api.

    *error_code* / *error_msg* go into the node's ``data`` and are set only
    on an infrastructure failure (result == "incomplete"), matching the
    kernelci-pipeline scheduler convention.
    """

    result: str
    error_code: Optional[str] = None
    error_msg: Optional[str] = None


# ---------------------------------------------------------------------------
# Main poller class.
# ---------------------------------------------------------------------------


class PullLabsPoller:
    """Polls kernelci-api, runs jobs, submits results to KCIDB."""

    def __init__(
        self,
        config: Dict[str, Any],
        cursor_store: Optional[FileCursorStore] = None,
        job_executor: Optional[JobExecutor] = None,
    ):
        kc = config.get("kernelci") or {}

        self.api_base_uri: str = _required(
            os.getenv(ENV_API_BASE_URI) or kc.get("api_base_uri"),
            "kernelci.api_base_uri",
        )
        self.api_token: Optional[str] = (
            os.getenv(ENV_API_TOKEN)
            or os.getenv(ENV_UNIFIED_TOKEN)
            or kc.get("api_token")
        )
        self.runtime_name: str = _required(
            os.getenv(ENV_RUNTIME_NAME) or kc.get("runtime_name"),
            "kernelci.runtime_name",
        )
        self.platforms: Optional[List[str]] = self._resolve_platforms(kc)
        # Resolution order for the KCIDB endpoint + token, matching kci-dev's
        # priority: explicit URL+JWT > KCIDB_REST combined env > config values.
        kcidb_url, kcidb_jwt = self._resolve_kcidb_endpoint(kc)
        self.kcidb_submit_url: str = _required(kcidb_url, "kernelci.kcidb_submit_url")
        self.kcidb_jwt: str = _required(
            kcidb_jwt,
            "kernelci.kcidb_jwt (env KCIDB_JWT, KCIDB_REST=https://<token>@host/submit, or UNIFIED_TOKEN)",
        )
        self.kcidb_origin: str = _required(
            os.getenv(ENV_KCIDB_ORIGIN) or kc.get("kcidb_origin"),
            "kernelci.kcidb_origin",
        )

        try:
            self.poll_interval_sec: int = int(
                os.getenv(ENV_POLL_INTERVAL) or kc.get("poll_interval_sec") or DEFAULT_POLL_INTERVAL_SEC
            )
        except (TypeError, ValueError):
            self.poll_interval_sec = DEFAULT_POLL_INTERVAL_SEC

        cursor_path = os.getenv(ENV_CURSOR_FILE) or kc.get("cursor_file") or DEFAULT_CURSOR_FILE
        self.cursor_store = cursor_store or FileCursorStore(cursor_path)
        self.job_executor: JobExecutor = job_executor or _default_job_executor
        self.base_config: Dict[str, Any] = config

        if job_executor is None:
            _validate_default_executor_deps()

        # Startup preflight: surface a bad/under-privileged kernelci-api token
        # now, rather than as a 401 on every job's claim/finish update.
        _validate_api_token(self.api_base_uri, self.api_token, self.runtime_name)

    # -- Credential resolution -------------------------------------------

    @staticmethod
    def _resolve_kcidb_endpoint(
        kc: Dict[str, Any],
    ) -> Tuple[Optional[str], Optional[str]]:
        """Pick the KCIDB submit URL and token.

        Priority (highest first):
          1. KCIDB_SUBMIT_URL + KCIDB_JWT env vars (both set).
          2. KCIDB_REST env var (kci-dev compatibility,
             format https://<token>@host/submit).
          3. UNIFIED_TOKEN env var as the JWT, paired with KCIDB_SUBMIT_URL
             if set otherwise the config submit URL.
          4. config.json: kernelci.kcidb_submit_url + kernelci.kcidb_jwt.
        """
        env_url = os.getenv(ENV_KCIDB_URL)
        env_jwt = os.getenv(ENV_KCIDB_JWT)
        if env_url and env_jwt:
            return env_url, env_jwt
        rest = os.getenv(ENV_KCIDB_REST)
        if rest:
            url, token = _parse_kcidb_rest(rest)
            if url and token:
                return url, token
            logger.warning(
                "KCIDB_REST is set but could not be parsed — "
                "expected https://<token>@host/submit"
            )
        unified = os.getenv(ENV_UNIFIED_TOKEN)
        if unified:
            return env_url or kc.get("kcidb_submit_url"), unified
        return kc.get("kcidb_submit_url"), kc.get("kcidb_jwt")

    # -- Polling --------------------------------------------------------

    def _events_url(self, from_ts: str) -> str:
        qs = urllib.parse.urlencode(
            {
                "state": "available",
                "kind": "job",
                "recursive": "true",
                "limit": 1000,
                "from": from_ts,
            }
        )
        return f"{self.api_base_uri.rstrip('/')}/events?{qs}"

    def fetch_events(self, from_ts: str) -> List[Dict[str, Any]]:
        url = self._events_url(from_ts)
        logger.debug("Polling: %s", url)
        events = _http_get_json(url, token=self.api_token) or []
        return events

    def _matches_runtime(self, event: Dict[str, Any]) -> bool:
        node = event.get("node") or {}
        data = node.get("data") or {}
        return data.get("runtime") == self.runtime_name

    def _matches_platform(self, event: Dict[str, Any]) -> bool:
        if not self.platforms:
            return True
        node = event.get("node") or {}
        data = node.get("data") or {}
        return data.get("platform") in self.platforms

    @staticmethod
    def _resolve_platforms(kc: Dict[str, Any]) -> Optional[List[str]]:
        raw = os.getenv(ENV_PLATFORMS)
        if raw is None:
            raw = kc.get("platforms")
        if raw is None:
            return None
        if isinstance(raw, str):
            items = [p.strip() for p in raw.split(",")]
        else:
            items = [str(p).strip() for p in raw]
        items = [p for p in items if p]
        return items or None

    def _job_definition_url(self, event: Dict[str, Any]) -> Optional[str]:
        node = event.get("node") or {}
        artifacts = node.get("artifacts") or {}
        url = artifacts.get("job_definition")
        if url and url.startswith("http"):
            return url
        return None

    # -- Build ID resolution from the maestro node tree ------------------

    def resolve_build_id(self, node: Dict[str, Any]) -> Optional[str]:
        """Walk up node.parent → kbuild ancestor, format as origin:<node_id>.

        Mirrors the convention used by kernelci-pipeline/src/send_kcidb.py:294
        (build.id = f"{origin}:{kbuild_node['id']}"). Returns None if no
        kbuild ancestor is found within a reasonable hop limit.
        """
        current = node
        for _ in range(8):
            kind = current.get("kind")
            if kind == "kbuild":
                return f"{self.kcidb_origin}:{current['id']}"
            parent_id = current.get("parent")
            if not parent_id:
                return None
            try:
                current = _http_get_json(
                    f"{self.api_base_uri.rstrip('/')}/node/{parent_id}",
                    token=self.api_token,
                ) or {}
            except (urllib.error.URLError, json.JSONDecodeError) as e:
                logger.warning("Failed to walk parent %s: %s", parent_id, e)
                return None
        return None

    # -- Node state updates ---------------------------------------------

    def _node_url(self, node_id: str) -> str:
        return f"{self.api_base_uri.rstrip('/')}/node/{node_id}"

    def _claim_node(self, node: Dict[str, Any]) -> bool:
        """Claim a job node by transitioning it to state=running.

        Re-reads the node first: if it is no longer "available", another
        poller has already taken it, so we skip it. This narrows -- but,
        without an atomic compare-and-set in kernelci-api, cannot fully
        close -- the window for two pollers claiming the same job.

        Returns True only if this poller now owns the node.
        """
        node_id = node.get("id")
        if not node_id:
            logger.warning("Cannot claim node without an id")
            return False
        url = self._node_url(node_id)
        try:
            current = _http_get_json(url, token=self.api_token) or {}
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error("Could not re-read node %s before claim: %s", node_id, e)
            return False
        state = current.get("state")
        if state != "available":
            logger.info("Skipping node %s: already claimed (state=%s)", node_id, state)
            return False
        current["state"] = "running"
        payload = {k: v for k, v in current.items() if k not in NODE_READ_ONLY_FIELDS}
        try:
            _http_put_json(url, payload, token=self.api_token)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error("Failed to claim node %s (PUT state=running): %s", node_id, e)
            return False
        logger.info("Claimed node %s (state=running)", node_id)
        return True

    def _finish_node(self, node_id: str, outcome: NodeOutcome) -> bool:
        """Mark a claimed job node done with the given outcome.

        Sets state=done and result; on an infrastructure failure also sets
        data.error_code / data.error_msg (kernelci ErrorCodes values),
        matching the kernelci-pipeline scheduler. A failure here is logged
        but not fatal -- the job already ran and its results were submitted
        to KCIDB.
        """
        url = self._node_url(node_id)
        try:
            current = _http_get_json(url, token=self.api_token) or {}
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error("Could not re-read node %s before finish: %s", node_id, e)
            return False
        current["state"] = "done"
        current["result"] = outcome.result
        if outcome.error_code:
            # error_code/error_msg live in node.data (kernelci JobData), not
            # at the top level.
            data = current.get("data") or {}
            data["error_code"] = outcome.error_code
            data["error_msg"] = outcome.error_msg
            current["data"] = data
        payload = {k: v for k, v in current.items() if k not in NODE_READ_ONLY_FIELDS}
        try:
            _http_put_json(url, payload, token=self.api_token)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error(
                "Failed to finish node %s (PUT state=done result=%s): %s",
                node_id, outcome.result, e,
            )
            return False
        logger.info(
            "Finished node %s (state=done, result=%s%s)",
            node_id, outcome.result,
            f", error_code={outcome.error_code}" if outcome.error_code else "",
        )
        return True

    # -- Per-event processing -------------------------------------------

    def process_event(self, event: Dict[str, Any]) -> bool:
        """Process one event end to end. Returns True on success.

        The job node is claimed (state=running) before any work starts and
        finished (state=done + result, plus error_code/error_msg on an
        infrastructure failure) afterwards, whatever the outcome. A node we
        fail to claim -- already taken, or an API error -- is skipped
        without being run or submitted.
        """
        node = event.get("node") or {}
        node_id = node.get("id")

        if not self._matches_runtime(event):
            logger.debug("Skipping event %s: runtime mismatch", node_id)
            return True

        if not self._matches_platform(event):
            logger.debug("Skipping event %s: platform mismatch", node_id)
            return True

        jobdef_url = self._job_definition_url(event)
        if not jobdef_url:
            logger.debug("Skipping event %s: no job_definition artifact", node_id)
            return True

        if not self._claim_node(node):
            return True

        logger.info("Processing pull-lab job node=%s definition=%s", node_id, jobdef_url)

        # We own the node now: it must be finished whatever happens below.
        # This default covers an unexpected crash inside _execute_job.
        node_outcome = NodeOutcome(
            "incomplete", _ERR_INFRASTRUCTURE, "unexpected internal error"
        )
        try:
            ok, node_outcome = self._execute_job(node, node_id, jobdef_url)
            return ok
        finally:
            self._finish_node(node_id, node_outcome)

    def _execute_job(
        self, node: Dict[str, Any], node_id: str, jobdef_url: str
    ) -> Tuple[bool, NodeOutcome]:
        """Fetch, translate, run and submit one already-claimed job.

        Returns (ok, outcome): *ok* is False on a recoverable failure;
        *outcome* is the NodeOutcome passed to _finish_node().
        """
        try:
            jobdef = _http_get_json(jobdef_url, token=self.api_token)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error("Failed to fetch job_definition for %s: %s", node_id, e)
            return False, NodeOutcome(
                "incomplete", _ERR_INFRASTRUCTURE,
                f"failed to fetch job_definition: {e}",
            )

        build_id = self.resolve_build_id(node)
        if not build_id:
            logger.warning(
                "Could not resolve build_id for node %s — submitting test rows without it "
                "may be rejected by KCIDB",
                node_id,
            )
            build_id = f"{self.kcidb_origin}:unknown_{node_id}"

        try:
            run_config = translate_job(jobdef, self.base_config, node_id=node_id)
        except ValueError as e:
            logger.error("Translation failed for node %s: %s", node_id, e)
            return False, NodeOutcome(
                "incomplete", _ERR_INVALID_JOB_PARAMS,
                f"job translation failed: {e}",
            )

        infra_error: Optional[NodeOutcome] = None
        try:
            per_test, log_url = self.job_executor(run_config)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Job execution failed for node %s: %s", node_id, e, exc_info=True)
            # An executor crash is an infrastructure failure: emit an ERROR
            # row so KCIDB sees we picked it up, and mark the node incomplete.
            # The boot. prefix has the dashboard classify it as a failed boot.
            infra_error = NodeOutcome(
                "incomplete", _ERR_INFRASTRUCTURE, f"job execution failed: {e}"
            )
            per_test = [{"name": "boot.infrastructure", "status": "ERROR"}]
            log_url = None

        test_rows = [
            build_test_row(
                origin=self.kcidb_origin,
                build_id=build_id,
                test_id=f"{node_id}.{idx}",
                path=t.get("name", f"test_{idx}"),
                status=to_kcidb_status(t.get("status", "error")),
                duration_ms=t.get("duration_ms"),
                log_url=log_url,
                misc={"kernelci_node_id": node_id},
            )
            for idx, t in enumerate(per_test or [])
        ]
        if not test_rows:
            # No per-test results came back -> the outcome is unknown, itself
            # an infrastructure failure (-> node result incomplete).
            infra_error = NodeOutcome(
                "incomplete", _ERR_INFRASTRUCTURE,
                "executor returned no per-test results",
            )
            test_rows = [
                build_test_row(
                    origin=self.kcidb_origin,
                    build_id=build_id,
                    test_id=f"{node_id}.0",
                    # "boot" path => the dashboard classifies this as a boot
                    # test (is_boot() in kernelCI_app/utils.py).
                    path="boot",
                    status="ERROR",
                    log_url=log_url,
                    misc={
                        "kernelci_node_id": node_id,
                        "note": "executor returned no per-test results",
                    },
                )
            ]

        # error_code + "incomplete" only on an infrastructure failure; a job
        # that actually ran is pass/fail/skip from its tests. Independent of
        # whether the KCIDB submission below succeeds.
        outcome = infra_error or NodeOutcome(_node_result_from_rows(test_rows))
        try:
            submit_tests(
                self.kcidb_submit_url,
                self.kcidb_jwt,
                self.kcidb_origin,
                build_id,
                test_rows,
            )
        except urllib.error.URLError as e:
            logger.error("KCIDB submit failed for node %s: %s", node_id, e)
            return False, outcome
        logger.info("Submitted %d test row(s) for node %s", len(test_rows), node_id)
        return True, outcome

    # -- Loop -----------------------------------------------------------

    def poll_once(self) -> int:
        """Single poll cycle. Returns the number of events processed."""
        from_ts = self.cursor_store.read()
        try:
            events = self.fetch_events(from_ts)
        except urllib.error.URLError as e:
            logger.error("Event poll failed: %s", e)
            return 0
        if not events:
            return 0

        processed = 0
        last_ts = from_ts
        for event in events:
            try:
                self.process_event(event)
                processed += 1
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("Unhandled error processing event: %s", e, exc_info=True)
            ts = event.get("timestamp")
            if ts:
                last_ts = ts

        if last_ts != from_ts:
            self.cursor_store.write(last_ts)
        return processed

    def run_forever(self) -> None:
        logger.info(
            "Starting pull-lab poller — api=%s runtime=%s kcidb=%s origin=%s interval=%ds",
            self.api_base_uri,
            self.runtime_name,
            self.kcidb_submit_url,
            self.kcidb_origin,
            self.poll_interval_sec,
        )
        while True:
            count = self.poll_once()
            if count == 0:
                time.sleep(self.poll_interval_sec)


# ---------------------------------------------------------------------------
# Helpers and entry points.
# ---------------------------------------------------------------------------


def _required(value: Optional[str], name: str) -> str:
    if not value:
        raise SystemExit(
            f"Missing required configuration: {name}. "
            f"Set the corresponding env var or add it to the kernelci section of config.json."
        )
    return value


def _load_base_config(path: Optional[str]) -> Dict[str, Any]:
    path = path or os.getenv(ENV_BASE_CONFIG) or "examples/aws/config.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Poll kernelci-api for pull-lab jobs and run them, then push results to KCIDB.",
    )
    parser.add_argument(
        "--config",
        help="Path to base config JSON (default: $PULLAB_BASE_CONFIG or examples/aws/config.json)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit (useful for cron / Lambda).",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        help="Python logging level (default: INFO).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = _load_base_config(args.config)
    poller = PullLabsPoller(config)
    if args.once:
        processed = poller.poll_once()
        logger.info("Single poll cycle processed %d event(s)", processed)
        return 0
    poller.run_forever()
    return 0


def lambda_handler(event, context=None):  # pylint: disable=unused-argument
    """AWS Lambda entry point. Runs a single poll cycle per invocation.

    Configuration is read entirely from environment variables — the Lambda
    deployment should set KERNELCI_API_BASE_URI, KERNELCI_RUNTIME_NAME,
    KCIDB_SUBMIT_URL, KCIDB_JWT, KCIDB_ORIGIN, and PULLAB_BASE_CONFIG (path
    to a config JSON bundled with the deployment or fetched from S3 by the
    caller before invoking).
    """
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config_path = (event or {}).get("config_path") or os.getenv(ENV_BASE_CONFIG)
    config = _load_base_config(config_path)
    poller = PullLabsPoller(config)
    processed = poller.poll_once()
    return {"status": "ok", "processed": processed}


if __name__ == "__main__":
    sys.exit(main())

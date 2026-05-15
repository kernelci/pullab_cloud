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
  2. Fetches each job's PULL_LABS job_definition JSON.
  3. Translates it into a pullab_cloud run config and runs the pipeline.
  4. Submits per-test results directly to KCIDB.

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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        return json.loads(body) if body else None


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

    for pkg in [
        "kernel_ci_cloud_labs.providers",
        "kernel_ci_cloud_labs.storage",
        "kernel_ci_cloud_labs.auth",
    ]:
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
        rows.append({"name": name, "status": status})
    return rows, None


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
        data = (node.get("data") or {}).get("data") or {}
        return data.get("runtime") == self.runtime_name

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

    # -- Per-event processing -------------------------------------------

    def process_event(self, event: Dict[str, Any]) -> bool:
        """Process one event end to end. Returns True on success."""
        node = event.get("node") or {}
        node_id = node.get("id")

        if not self._matches_runtime(event):
            logger.debug("Skipping event %s: runtime mismatch", node_id)
            return True

        jobdef_url = self._job_definition_url(event)
        if not jobdef_url:
            logger.debug("Skipping event %s: no job_definition artifact", node_id)
            return True

        logger.info("Processing pull-lab job node=%s definition=%s", node_id, jobdef_url)

        try:
            jobdef = _http_get_json(jobdef_url, token=self.api_token)
        except (urllib.error.URLError, json.JSONDecodeError) as e:
            logger.error("Failed to fetch job_definition for %s: %s", node_id, e)
            return False

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
            return False

        try:
            per_test, log_url = self.job_executor(run_config)
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("Job execution failed for node %s: %s", node_id, e, exc_info=True)
            # Submit an ERROR row so KCIDB sees we picked it up.
            per_test = [{"name": "infrastructure", "status": "ERROR"}]
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
            test_rows = [
                build_test_row(
                    origin=self.kcidb_origin,
                    build_id=build_id,
                    test_id=f"{node_id}.0",
                    path="pullab_cloud",
                    status="ERROR",
                    log_url=log_url,
                    misc={
                        "kernelci_node_id": node_id,
                        "note": "executor returned no per-test results",
                    },
                )
            ]

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
            return False
        logger.info("Submitted %d test row(s) for node %s", len(test_rows), node_id)
        return True

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

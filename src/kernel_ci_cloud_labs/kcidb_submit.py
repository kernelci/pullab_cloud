# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Direct KCIDB submission via the kcidb-restd-rs REST endpoint.

Pure-Python module — uses stdlib urllib only, no boto3 / no third-party
HTTP client. Builds a tests-only KCIDB revision dict (per the schema in
kernelci-pipeline/src/send_kcidb.py:730) and POSTs it to a configured
`/submit` URL with `Authorization: Bearer <jwt>`.

Builds and checkouts are NOT re-emitted: this producer attaches test
results to an existing build_id resolved from the maestro (kernelci-api)
node tree.
"""

import json
import logging
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)


KCIDB_SCHEMA_VERSION = {"major": 5, "minor": 3}

# Map PULL_LABS / pullab_cloud status strings to KCIDB status values.
# KCIDB tests.status enum: PASS, FAIL, SKIP, ERROR, MISS, DONE (and a few).
STATUS_MAP = {
    "pass": "PASS",
    "ok": "PASS",
    "success": "PASS",
    "fail": "FAIL",
    "failed": "FAIL",
    "skip": "SKIP",
    "skipped": "SKIP",
    "error": "ERROR",
    "errored": "ERROR",
    "incomplete": "ERROR",
    "miss": "MISS",
    "done": "DONE",
}


def to_kcidb_status(raw: Optional[str]) -> str:
    if not raw:
        return "ERROR"
    return STATUS_MAP.get(str(raw).strip().lower(), "ERROR")


# KCIDB v5.3 field constraints (see kcidb_io.schema.V5_3). pullab_cloud
# constructs these fields itself, so each value is verified before submission:
# an invalid one would otherwise make the *whole* submission fail at the
# ingester, with a far less obvious error than a local raise.
#   tests[*].path -- dot-separated segments of [A-Za-z0-9_-], or empty
#                    (kcidb_io.schema.V5_3.test_path_re)
#   *.origin      -- [a-z0-9_]+
_KCIDB_PATH_RE = re.compile(r"^([a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*)?$")
_KCIDB_ORIGIN_RE = re.compile(r"^[a-z0-9_]+$")


def validate_test_path(path: str) -> str:
    """Verify *path* is a KCIDB v5.3-compliant test path; return it unchanged.

    KCIDB v5.3 restricts ``tests[*].path`` to dot-separated segments of
    ``[A-Za-z0-9_-]`` (the empty string is allowed -- it denotes the test
    tree root). A path with a space, slash or other punctuation makes the
    entire submission fail schema validation at the ingester, so it is
    rejected here, at the producer, rather than silently rewritten.

    Raises:
        ValueError: if *path* is not KCIDB v5.3-compliant.
    """
    if not isinstance(path, str) or not _KCIDB_PATH_RE.match(path):
        raise ValueError(
            f"invalid KCIDB test path {path!r}: must be dot-separated "
            "segments of [A-Za-z0-9_-] (KCIDB v5.3)"
        )
    return path


def validate_origin(origin: str) -> str:
    """Verify *origin* is a KCIDB v5.3-compliant origin; return it unchanged.

    KCIDB v5.3 restricts every object's ``origin`` to ``[a-z0-9_]+``
    (lowercase letters, digits and underscores).

    Raises:
        ValueError: if *origin* is not KCIDB v5.3-compliant.
    """
    if not isinstance(origin, str) or not _KCIDB_ORIGIN_RE.match(origin):
        raise ValueError(
            f"invalid KCIDB origin {origin!r}: must match [a-z0-9_]+ "
            "(lowercase letters, digits and underscores; KCIDB v5.3)"
        )
    return origin


def build_test_row(
    *,
    origin: str,
    build_id: str,
    test_id: str,
    path: str,
    status: str,
    duration_ms: Optional[int] = None,
    log_url: Optional[str] = None,
    output_files: Optional[List[Dict[str, str]]] = None,
    misc: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a single KCIDB tests[*] row.

    Required fields per the KCIDB v5.3 IO schema: id, build_id, origin.
    `path` and `status` are optional in the schema, but always emitted here.
    Optional fields: duration, log_url, output_files, misc, comment.

    `origin` and `path` are verified against the KCIDB v5.3 constraints;
    an invalid value raises ValueError instead of being submitted, so a bad
    test name is caught here rather than failing the whole submission at the
    ingester.

    Raises:
        ValueError: if `origin` or `path` is not KCIDB v5.3-compliant.
    """
    origin = validate_origin(origin)
    row: Dict[str, Any] = {
        "id": f"{origin}:{test_id}",
        "build_id": build_id,
        "origin": origin,
        "path": validate_test_path(path),
        "status": to_kcidb_status(status),
    }
    if duration_ms is not None:
        row["duration"] = float(duration_ms) / 1000.0
    if log_url:
        row["log_url"] = log_url
    if output_files:
        row["output_files"] = output_files
    if misc:
        row["misc"] = misc
    return row


def build_revision(test_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a KCIDB revision payload containing only tests."""
    return {
        "checkouts": [],
        "builds": [],
        "tests": list(test_rows),
        "version": dict(KCIDB_SCHEMA_VERSION),
    }


def submit_revision(
    submit_url: str,
    jwt_token: str,
    revision: Dict[str, Any],
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """POST a pre-built revision dict to kcidb-restd-rs /submit.

    Returns the parsed JSON response on success. Raises on HTTP error.
    """
    if not submit_url:
        raise ValueError("submit_url is required")
    if not jwt_token:
        raise ValueError("jwt_token is required")

    body = json.dumps(revision).encode("utf-8")
    req = urllib.request.Request(
        submit_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(payload) if payload else {}
            except json.JSONDecodeError:
                return {"raw_response": payload}
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        logger.error(
            "KCIDB submit failed: HTTP %s — %s — body: %s",
            e.code,
            e.reason,
            err_body[:500],
        )
        raise


def submit_tests(
    submit_url: str,
    jwt_token: str,
    origin: str,
    build_id: str,
    test_rows: Iterable[Dict[str, Any]],
    timeout: float = 30.0,
) -> Dict[str, Any]:
    """Convenience wrapper: build a tests-only revision and submit it.

    *test_rows* should be dicts produced by build_test_row(); this wrapper
    fills in `build_id` and `origin` if a row omits them.
    """
    rows: List[Dict[str, Any]] = []
    for r in test_rows:
        row = dict(r)
        row.setdefault("build_id", build_id)
        row.setdefault("origin", origin)
        rows.append(row)
    revision = build_revision(rows)
    logger.info(
        "Submitting %d test rows to KCIDB (origin=%s, build_id=%s)",
        len(rows),
        origin,
        build_id,
    )
    return submit_revision(submit_url, jwt_token, revision, timeout=timeout)

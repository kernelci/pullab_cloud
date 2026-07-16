"""Per-run artifact collection and manifest for KCIDB submission.

This module fills the gap between two existing flows:

  * `launch_vm.py` (inside the ECS container) uploads per-instance output —
    notably ``console-output.log`` (kernel boot log) — to S3 under
    ``s3://<bucket>/<run_prefix>/test_<test>/output/<instance_id>/``.

  * `pull_labs_poller.submit_tests()` posts KCIDB test rows whose
    ``log_url`` / ``output_files`` fields are currently always empty.

The orchestrator (``core.pipeline.run_pipeline``) calls
:func:`collect_run_artifacts` after VM CloudWatch logs have been pulled.
For each instance under the run prefix it:

  1. Downloads the kernel boot console log from S3 to
     ``logs/run_<ts>/vms/<instance_id>-console.log`` so a developer can grep
     it locally without an `aws s3 cp` round-trip.
  2. Constructs the public HTTPS URL of that object on S3 — the value KCIDB
     expects in the ``tests[*].log_url`` field. The URL only resolves when
     the bucket carries the public-read policy installed by
     ``setup_validate.check_s3_logs_public_policy``.
  3. Records a manifest entry per artifact (sha256, size, content-type,
     S3 URI, https URL) into ``logs/run_<ts>/artifacts.json``.

The KCIDB submitter (currently ``pull_labs_poller``) is expected to consume
this manifest and pass each entry's ``log_url`` field into
``kcidb_submit.build_test_row(log_url=..., output_files=[...])`` keyed by
``(test, instance_id)``. If the project later switches to files.kernelci.org,
only the URL-construction step in this module changes — the manifest schema
and the submitter integration stay the same.
"""

__authors__ = ["Denys Fedoryshchenko <nuclearcat@gmail.com>"]
__copyright__ = "Copyright (c) 2026 KernelCI project. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from kernel_ci_cloud_labs.core.logging_config import get_logger

logger = get_logger(__name__)


# Schema version for artifacts.json. Bump when the on-disk format changes
# in a way an external uploader needs to know about.
ARTIFACTS_MANIFEST_VERSION = 1

# Files we know about under the per-instance S3 output prefix. Each entry
# maps the basename in S3 to (kcidb_role, content_type). `kcidb_role`
# steers a future uploader's decision between populating ``log_url`` (a
# single primary log) or appending to ``output_files`` (everything else).
#
# "console-output.log" is the boot log — the natural ``log_url`` for boot
# tests. For functional tests the executor may later prefer the latest
# ``run-N-output.log``; that decision is left to the uploader/poller.
_KNOWN_ARTIFACT_KINDS: Dict[str, Tuple[str, str]] = {
    "console-output.log": ("log", "text/plain; charset=utf-8"),
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_to(s3_client, bucket: str, key: str, dest: Path) -> Optional[int]:
    """Download ``s3://bucket/key`` to ``dest``. Returns size in bytes, or
    None if the key does not exist.

    NoSuchKey is the expected outcome for instances where launch_vm.py
    failed before the console buffer was uploaded — we want a quiet skip,
    not a noisy warning.
    """
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        obj = s3_client.get_object(Bucket=bucket, Key=key)
        body = obj["Body"].read()
        dest.write_bytes(body)
        return len(body)
    except s3_client.exceptions.NoSuchKey:
        return None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Failed to download s3://%s/%s: %s", bucket, key, e)
        return None


def _discover_instances(s3_client, bucket: str, run_prefix: str) -> List[Tuple[str, str]]:
    """List (test_name, instance_id) pairs under ``run_prefix`` in S3.

    The layout written by launch_vm.py is:

        <run_prefix>/test_<test_name>/output/<instance_id>/<file>

    We list ``run_prefix/`` with delimiter='/' twice to walk the two
    intermediate directories without dragging back every file in the run.
    """
    pairs: List[Tuple[str, str]] = []
    try:
        test_dirs = s3_client.list_objects_v2(
            Bucket=bucket,
            Prefix=f"{run_prefix}/",
            Delimiter="/",
        ).get("CommonPrefixes", [])
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Could not list S3 prefix %s/: %s", run_prefix, e)
        return pairs

    for tdir in test_dirs:
        test_prefix = tdir.get("Prefix", "")
        # Expect ".../test_<name>/" — anything else (e.g. the bare TEST_CONFIG
        # JSON sitting in run_prefix/) is not an instance container.
        leaf = test_prefix.rstrip("/").rsplit("/", 1)[-1]
        if not leaf.startswith("test_"):
            continue
        test_name = leaf[len("test_") :]

        try:
            inst_dirs = s3_client.list_objects_v2(
                Bucket=bucket,
                Prefix=f"{test_prefix}output/",
                Delimiter="/",
            ).get("CommonPrefixes", [])
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("Could not list S3 prefix %soutput/: %s", test_prefix, e)
            continue

        for idir in inst_dirs:
            iprefix = idir.get("Prefix", "")
            instance_id = iprefix.rstrip("/").rsplit("/", 1)[-1]
            if instance_id:
                pairs.append((test_name, instance_id))

    return pairs


def s3_public_url(bucket: str, region: str, key: str) -> str:
    """Virtual-hosted-style public HTTPS URL for an S3 object.

    Form: ``https://<bucket>.s3.<region>.amazonaws.com/<key>``. Only
    resolves when the bucket policy grants anonymous ``s3:GetObject`` on
    the key — see ``setup_validate.check_s3_logs_public_policy``.
    """
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def collect_run_artifacts(
    run_dir: Path,
    *,
    s3_client,
    bucket: str,
    region: str,
    run_prefix: str,
    origin: Optional[str] = None,
) -> Dict[str, Any]:
    """Collect uploadable artifacts for a finished run.

    For every (test, instance) discovered under ``run_prefix`` in S3 this:
      * downloads ``console-output.log`` into ``run_dir/vms/<id>-console.log``;
      * records a manifest entry with sha256/size/content-type, the S3 URI
        it was fetched from, and the path it should land at on the remote
        store.

    The manifest is written to ``run_dir/artifacts.json`` and also returned.

    Args:
        run_dir: Local directory for this pipeline run (already created).
        s3_client: boto3 S3 client.
        bucket: S3 bucket name.
        region: AWS region (used to build the public HTTPS URL).
        run_prefix: Per-run S3 prefix (e.g. ``run_<test_id>_<timestamp>``).
        origin: Optional KCIDB origin string; embedded into the manifest for
            traceability.

    Returns:
        The manifest dict (also persisted to ``artifacts.json``).
    """
    run_dir = Path(run_dir)
    vms_dir = run_dir / "vms"
    vms_dir.mkdir(parents=True, exist_ok=True)

    pairs = _discover_instances(s3_client, bucket, run_prefix)
    if not pairs:
        logger.info("No per-instance S3 output found under %s/", run_prefix)

    entries: List[Dict[str, Any]] = []
    for test_name, instance_id in pairs:
        s3_key = f"{run_prefix}/test_{test_name}/output/{instance_id}/console-output.log"
        local_path = vms_dir / f"{instance_id}-console.log"
        s3_uri = f"s3://{bucket}/{s3_key}"
        log_url = s3_public_url(bucket, region, s3_key)
        role, ctype = _KNOWN_ARTIFACT_KINDS["console-output.log"]

        size = _download_to(s3_client, bucket, s3_key, local_path)
        if size is None:
            # Console buffer never made it to S3 — typical when the EC2
            # GetConsoleOutput call returned an empty buffer (very early
            # boot failure) or the launcher exited before cleanup. We still
            # emit a 'missing' entry so the KCIDB submitter knows there is
            # nothing to link for this instance.
            entries.append(
                {
                    "test": test_name,
                    "instance_id": instance_id,
                    "kind": "console-output.log",
                    "kcidb_role": role,
                    "status": "missing",
                    "s3_uri": s3_uri,
                    "log_url": None,
                    "local_path": None,
                    "sha256": None,
                    "size_bytes": 0,
                    "content_type": ctype,
                }
            )
            continue

        entries.append(
            {
                "test": test_name,
                "instance_id": instance_id,
                "kind": "console-output.log",
                "kcidb_role": role,
                "status": "ready",
                "s3_uri": s3_uri,
                "log_url": log_url,
                "local_path": str(local_path.relative_to(run_dir)),
                "sha256": _sha256_file(local_path),
                "size_bytes": size,
                "content_type": ctype,
            }
        )

    manifest = {
        "schema_version": ARTIFACTS_MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_prefix": run_prefix,
        "s3_bucket": bucket,
        "origin": origin,
        "artifacts": entries,
    }

    manifest_path = run_dir / "artifacts.json"
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    ready = sum(1 for e in entries if e["status"] == "ready")
    missing = sum(1 for e in entries if e["status"] == "missing")
    logger.info(
        "✓ Wrote artifacts manifest (%d ready, %d missing) to %s",
        ready,
        missing,
        manifest_path,
    )
    return manifest

# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Translate a PULL_LABS job definition into pullab_cloud test_config.

Pure-Python module — no boto3 / AWS imports. Produces a config dict that
the existing pipeline runner (any provider) can consume.

Reference for the PULL_LABS job_definition schema:
  kernelci-core/kernelci/runtime/pull_labs.py:272 (PullLabs.generate)

Reference for the pullab_cloud config schema:
  examples/aws/config.json (test_config.vms[*])
"""

import copy
import json
import uuid
from typing import Any, Dict, List, Optional


# Architecture → instance_type / AMI hints. Override via the platform_map
# argument of translate_job() to customise per-deployment.
DEFAULT_PLATFORM_MAP: Dict[str, Dict[str, str]] = {
    "x86_64": {
        "instance_type": "c5a.4xlarge",
        "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64",
    },
    "arm64": {
        "instance_type": "c6g.4xlarge",
        "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
    },
    "aarch64": {
        "instance_type": "c6g.4xlarge",
        "ami_id": "resolve:ssm:/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64",
    },
}

# Map a PULL_LABS test "type" (jobdef.tests[].type) to a vm-tests directory.
# Extend via test_type_map argument; unknown types fall back to "url-kernel-boot".
DEFAULT_TEST_TYPE_MAP: Dict[str, str] = {
    "baseline": "url-kernel-boot",
    "ltp": "ltp",
    "unixbench": "url-kernel-boot",
}


def _normalise_arch(jobdef: Dict[str, Any]) -> str:
    """Derive a normalised arch string from job_definition.

    Looks at environment.arch first, then falls back to inferring from
    environment.platform (e.g. "qemu-arm64" → "arm64").
    """
    env = jobdef.get("environment", {}) or {}
    arch = env.get("arch")
    if arch:
        return arch
    platform = env.get("platform", "") or ""
    if "arm64" in platform or "aarch64" in platform:
        return "arm64"
    return "x86_64"


def _resolve_test_dir(test_type: str, test_type_map: Dict[str, str]) -> str:
    return test_type_map.get(test_type, test_type_map.get("_default", "url-kernel-boot"))


def translate_job(
    jobdef: Dict[str, Any],
    base_config: Dict[str, Any],
    node_id: Optional[str] = None,
    platform_map: Optional[Dict[str, Dict[str, str]]] = None,
    test_type_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Build a per-run config from a PULL_LABS job definition.

    *jobdef* is the JSON fetched from the node's `artifacts.job_definition`
    URL. *base_config* is the deployment-wide template (auth, ECS,
    cloudwatch, storage, role) loaded from disk. The returned dict is a
    deep copy of base_config with `test_config` rewritten for this job.

    Each PULL_LABS job becomes exactly one entry in `test_config.vms[*]`
    (one VM per job, multiple tests run sequentially in the same VM).
    Artifact URLs (kernel/modules/rootfs) and the job timeout are passed
    to the vm-test as `test_params` so the run scripts can pick them up
    via env vars.
    """
    platform_map = platform_map or DEFAULT_PLATFORM_MAP
    test_type_map = test_type_map or DEFAULT_TEST_TYPE_MAP

    arch = _normalise_arch(jobdef)
    plat_defaults = platform_map.get(arch) or platform_map["x86_64"]

    artifacts = jobdef.get("artifacts", {}) or {}
    kernel_url = artifacts.get("kernel")
    modules_url = artifacts.get("modules")
    rootfs_url = artifacts.get("rootfs") or artifacts.get("ramdisk")

    if not kernel_url or not modules_url:
        raise ValueError(
            "PULL_LABS job_definition missing required artifacts.kernel or "
            "artifacts.modules — refusing to translate"
        )

    job_tests = jobdef.get("tests", []) or []
    test_dirs: List[str] = []
    for t in job_tests:
        test_dirs.append(_resolve_test_dir(t.get("type", ""), test_type_map))
    if not test_dirs:
        test_dirs = [test_type_map.get("_default", "url-kernel-boot")]
    # Deduplicate while preserving order
    seen = set()
    test_dirs = [d for d in test_dirs if not (d in seen or seen.add(d))]

    test_params: Dict[str, str] = {
        "KERNEL_URL": kernel_url,
        "MODULES_URL": modules_url,
        "ARCH": arch,
    }
    if rootfs_url:
        test_params["ROOTFS_URL"] = rootfs_url
    if node_id:
        test_params["KERNELCI_NODE_ID"] = node_id

    # Pass each individual test's id/type so the vm-test can run them in order.
    if job_tests:
        test_params["PULL_LABS_TESTS"] = ",".join(
            f"{t.get('id', t.get('type', 'unknown'))}:{t.get('type', 'unknown')}"
            for t in job_tests
        )
        # Full test descriptions, including the free-form `parameters` string
        # the id:type pairs above cannot carry; PULL_LABS_TESTS is kept for
        # compatibility with existing vm-tests.
        test_params["PULL_LABS_TESTS_JSON"] = json.dumps(
            [
                {
                    "id": t.get("id", t.get("type", "unknown")),
                    "type": t.get("type", "unknown"),
                    "parameters": t.get("parameters", ""),
                    "timeout_s": t.get("timeout_s"),
                }
                for t in job_tests
            ],
            separators=(",", ":"),
        )

    timeout = jobdef.get("timeout") or 3600
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 3600

    # The pipeline groups outputs by test_id; make it unique per job so that
    # parallel pulls don't collide.
    short_id = (node_id or uuid.uuid4().hex)[:12]
    test_id = f"pulllab-{arch}-{short_id}"

    vm_entry: Dict[str, Any] = {
        "ami_id": plat_defaults["ami_id"],
        "instance_type": plat_defaults["instance_type"],
        "root_volume_size": 40,
        "max_runtime": timeout,
        "test": test_dirs,
        "min_count": 1,
        "test_params": test_params,
    }

    cfg = copy.deepcopy(base_config)
    cfg.setdefault("test_config", {})
    # Preserve any deployment-wide role_name set in the base template.
    base_role = (base_config.get("test_config") or {}).get("role_name")
    cfg["test_config"] = {
        "test_id": test_id,
        "vms": [vm_entry],
    }
    if base_role:
        cfg["test_config"]["role_name"] = base_role

    return cfg

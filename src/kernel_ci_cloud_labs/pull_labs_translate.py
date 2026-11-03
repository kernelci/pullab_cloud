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
import re
import shlex
import uuid
from typing import Any, Dict, List, Optional

from kernel_ci_cloud_labs.kernel_test_results import BOOT_TEST_TYPES, TESTS_PARAM


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
# Unsupported tests must never silently become successful boot tests.
DEFAULT_TEST_TYPE_MAP: Dict[str, str] = {
    "boot": "url-kernel-boot",
    "baseline": "url-kernel-boot",
    "ltp": "url-kernel-ltp",
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
    directory = test_type_map.get(test_type) or test_type_map.get("_default")
    if not directory:
        raise ValueError(f"Unsupported pull-lab test type: {test_type!r}")
    return directory


def _test_parameters(test: Dict[str, Any]) -> Dict[str, str]:
    raw = test.get("parameters") or {}
    if isinstance(raw, str):
        parameters = {}
        for field in shlex.split(raw):
            key, separator, value = field.partition("=")
            if not separator or key in parameters:
                raise ValueError(f"Invalid or duplicate test parameter: {field!r}")
            parameters[key] = value
        return parameters
    if not isinstance(raw, dict):
        raise ValueError("Test parameters must be a mapping or key=value string")
    return {key: str(value) for key, value in raw.items()}


def _test_requests(jobdef: Dict[str, Any], test_type_map: Dict[str, str]) -> List[Dict[str, Any]]:
    """Validate the executable contract before claiming any test as supported."""
    tests = jobdef.get("tests") or [{"id": "boot", "type": "boot"}]
    defaults = jobdef.get("parameters") or {}
    requests = []
    seen = set()
    for index, test in enumerate(tests):
        kind = test.get("type", "")
        _resolve_test_dir(kind, test_type_map)
        name = test.get("id") or f"{kind}-{index}"
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or name in seen:
            raise ValueError(f"Invalid or duplicate test id: {name!r}")
        seen.add(name)
        if test.get("pre-commands") or test.get("post-commands"):
            raise ValueError("Pull-lab pre/post commands are not implemented by this runner")
        timeout = int(test.get("timeout_s") or 600)
        if not 0 < timeout <= 43200:
            raise ValueError("Test timeout must be between 1 and 43200 seconds")
        request = {"id": name, "type": kind, "timeout_s": timeout, "depends": list(test.get("depends") or [])}
        params = _test_parameters(test)
        if kind == "ltp":
            unknown = set(params) - {"tst_cmdfiles", "skipfile", "workers", "skip_install"}
            if unknown:
                raise ValueError(f"Unsupported LTP parameters: {sorted(unknown)}")
            if params.get("skip_install", "true").lower() not in ("true", "1"):
                raise ValueError("LTP must be supplied by the job's rootfs artifact")
            suites = params.get("tst_cmdfiles") or "smoketest"
            suites = suites.split(",")
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", suite) for suite in suites):
                raise ValueError("Invalid LTP suite name")
            workers = params.get("workers", defaults.get("KIRK_WORKERS", "1"))
            if str(workers) != "max" and not str(workers).isdigit():
                raise ValueError("LTP workers must be a positive integer or 'max'")
            if str(workers).isdigit() and not 1 <= int(workers) <= 128:
                raise ValueError("LTP workers must be between 1 and 128")
            request.update(
                suites=suites,
                workers=str(workers),
                skipfile=params.get("skipfile", defaults.get("SKIPFILE", "")),
                runner=defaults.get("RUNNER", "/opt/kirk/kirk"),
            )
        elif params:
            raise ValueError(f"Parameters are not implemented for {kind!r}")
        requests.append(request)
    # Respect dependency order; reject unknown dependencies and cycles.
    ordered = []
    pending = list(requests)
    while pending:
        ready = [request for request in pending if set(request["depends"]) <= {r["id"] for r in ordered}]
        if not ready:
            raise ValueError("Unknown or cyclic pull-lab test dependencies")
        for request in ready:
            ordered.append(request)
            pending.remove(request)
    return ordered


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
    platform_map = platform_map or (base_config.get("kernelci") or {}).get("platform_map") or DEFAULT_PLATFORM_MAP
    test_type_map = test_type_map or DEFAULT_TEST_TYPE_MAP

    arch = _normalise_arch(jobdef)
    platform_key = "arm64" if arch == "aarch64" and "arm64" in platform_map else arch
    if platform_key not in platform_map:
        raise ValueError(f"Unsupported pull-lab architecture: {arch!r}")
    plat_defaults = platform_map[platform_key]

    artifacts = jobdef.get("artifacts", {}) or {}
    kernel_url = artifacts.get("kernel")
    modules_url = artifacts.get("modules")
    rootfs_url = artifacts.get("rootfs") or artifacts.get("ramdisk")

    if not kernel_url or not modules_url:
        raise ValueError(
            "PULL_LABS job_definition missing required artifacts.kernel or artifacts.modules — refusing to translate"
        )

    job_tests = _test_requests(jobdef, test_type_map)
    test_dirs: List[str] = []
    for t in job_tests:
        test_dirs.append(_resolve_test_dir(t.get("type", ""), test_type_map))
    if not test_dirs:
        test_dirs = [test_type_map.get("_default", "url-kernel-boot")]
    # Deduplicate while preserving order
    seen = set()
    test_dirs = [d for d in test_dirs if not (d in seen or seen.add(d))]
    # Native boot + LTP share one VM and one reboot, not independent VMs that
    # could run tests against different kernels. The LTP payload runs every
    # requested test and reports the boot/baseline result from its evidence.
    if test_type_map.get("ltp") in test_dirs:
        boot_dirs = {test_type_map.get(kind) for kind in BOOT_TEST_TYPES}
        test_dirs = [d for d in test_dirs if d not in boot_dirs or d == test_type_map["ltp"]]
    if any(test["type"] == "ltp" for test in job_tests) and not rootfs_url:
        raise ValueError("LTP requires artifacts.rootfs containing Kirk and LTP")

    test_params: Dict[str, str] = {
        "KERNEL_URL": kernel_url,
        "MODULES_URL": modules_url,
        "ARCH": arch,
        TESTS_PARAM: json.dumps(job_tests, separators=(",", ":")),
    }
    kernelci = base_config.get("kernelci") or {}
    test_params["PULL_LABS_SKIPFILES_JSON"] = json.dumps(kernelci.get("skipfiles") or {})
    revision = jobdef.get("kernel_revision") or {}
    test_params["PULL_LABS_CONTEXT_JSON"] = json.dumps(
        {
            "platform": (jobdef.get("environment") or {}).get("platform", ""),
            "branch": revision.get("branch", ""),
            "environment": kernelci.get("environment", ""),
        }
    )
    checksums = (jobdef.get("integrity") or {}).get("sha256") or {}
    for name in ("kernel", "modules", "rootfs"):
        checksum = checksums.get(name)
        if checksum is not None:
            if not isinstance(checksum, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", checksum):
                raise ValueError(f"Invalid SHA256 for {name}")
            test_params[f"{name.upper()}_SHA256"] = checksum.lower()
    if rootfs_url:
        test_params["ROOTFS_URL"] = rootfs_url
    if node_id:
        test_params["KERNELCI_NODE_ID"] = node_id

    # Pass each individual test's id/type so the vm-test can run them in order.
    if job_tests:
        test_params["PULL_LABS_TESTS"] = ",".join(
            f"{t.get('id', t.get('type', 'unknown'))}:{t.get('type', 'unknown')}" for t in job_tests
        )

    # Allow kernel installation/reboot in addition to full suite timeouts when
    # the job does not impose an overall deadline.
    default_timeout = min(43200, max(3600, sum(test["timeout_s"] for test in job_tests) + 900))
    timeout = jobdef.get("timeout") or default_timeout
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 3600
    if not 0 < timeout <= 43200:
        raise ValueError("Job timeout must be between 1 and 43200 seconds")

    # The pipeline groups outputs by test_id; make it unique per job so that
    # parallel pulls don't collide.
    short_id = (node_id or uuid.uuid4().hex)[:12]
    test_id = f"pulllab-{arch}-{short_id}"

    vm_entry: Dict[str, Any] = {
        "ami_id": plat_defaults["ami_id"],
        "instance_type": plat_defaults["instance_type"],
        "root_volume_size": plat_defaults.get("root_volume_size", 40),
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

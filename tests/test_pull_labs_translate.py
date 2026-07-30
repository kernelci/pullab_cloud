# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 Collabora Limited
# Author: Denys Fedoryshchenko <denys.f@collabora.com>

"""Unit tests for pull_labs_translate."""

import json

import pytest

from kernel_ci_cloud_labs.pull_labs_translate import (
    DEFAULT_PLATFORM_MAP,
    translate_job,
)


BASE_CONFIG = {
    "provider": "aws",
    "region": "eu-west-2",
    "storage": {"type": "s3", "bucket": "test-bucket"},
    "auth_credentials": {"auth_provider": "aws"},
    "test_config": {"test_id": "placeholder", "role_name": "test-role"},
}


def _job(arch="x86_64", platform=None, tests=None, timeout=None, extra_artifacts=None):
    artifacts = {
        "kernel": "https://storage.example.org/k/bzImage",
        "modules": "https://storage.example.org/k/modules.tar.gz",
    }
    if extra_artifacts:
        artifacts.update(extra_artifacts)
    env = {"arch": arch}
    if platform is not None:
        env["platform"] = platform
    job = {"environment": env, "artifacts": artifacts}
    if tests is not None:
        job["tests"] = tests
    if timeout is not None:
        job["timeout"] = timeout
    return job


class TestTranslateJobHappyPath:
    """Happy-path scenarios for translate_job."""

    def test_minimal_x86_job(self):
        out = translate_job(_job(), BASE_CONFIG, node_id="node-x86")
        vm = out["test_config"]["vms"][0]
        assert vm["instance_type"] == DEFAULT_PLATFORM_MAP["x86_64"]["instance_type"]
        assert vm["ami_id"] == DEFAULT_PLATFORM_MAP["x86_64"]["ami_id"]
        assert vm["test"] == ["url-kernel-boot"]
        assert vm["test_params"]["KERNEL_URL"].endswith("bzImage")
        assert vm["test_params"]["ARCH"] == "x86_64"
        assert vm["test_params"]["KERNELCI_NODE_ID"] == "node-x86"

    def test_arm64_inferred_from_platform(self):
        # No arch in env, but platform name carries arm64
        jobdef = _job()
        jobdef["environment"] = {"platform": "qemu-arm64"}
        out = translate_job(jobdef, BASE_CONFIG, node_id="n1")
        vm = out["test_config"]["vms"][0]
        assert vm["instance_type"] == DEFAULT_PLATFORM_MAP["arm64"]["instance_type"]
        assert vm["test_params"]["ARCH"] == "arm64"

    def test_aarch64_alias_resolves_to_arm64_ami(self):
        out = translate_job(_job(arch="aarch64"), BASE_CONFIG, node_id="n1")
        vm = out["test_config"]["vms"][0]
        assert vm["ami_id"] == DEFAULT_PLATFORM_MAP["aarch64"]["ami_id"]

    def test_unknown_arch_falls_back_to_x86(self):
        out = translate_job(_job(arch="riscv64"), BASE_CONFIG, node_id="n1")
        vm = out["test_config"]["vms"][0]
        assert vm["instance_type"] == DEFAULT_PLATFORM_MAP["x86_64"]["instance_type"]

    def test_default_map_routes_ltp_to_own_vm_test(self):
        assert DEFAULT_TEST_TYPE_MAP["ltp"] == "ltp"
        assert DEFAULT_TEST_TYPE_MAP["baseline"] == "url-kernel-boot"

    def test_test_types_map_to_vm_test_dirs(self):
        jobdef = _job(tests=[
            {"id": "ltp-syscalls", "type": "ltp"},
            {"id": "baseline-x86", "type": "baseline"},
            {"id": "unknown-thing", "type": "weird"},
        ])
        out = translate_job(jobdef, BASE_CONFIG, node_id="n1")
        vm = out["test_config"]["vms"][0]
        # ltp has its own vm-test; baseline and unknown types fall back to
        # url-kernel-boot and are deduplicated
        assert vm["test"] == ["ltp", "url-kernel-boot"]
        assert "PULL_LABS_TESTS" in vm["test_params"]
        assert "ltp-syscalls:ltp" in vm["test_params"]["PULL_LABS_TESTS"]
        assert "baseline-x86:baseline" in vm["test_params"]["PULL_LABS_TESTS"]

    def test_tests_json_carries_parameters(self):
        jobdef = _job(tests=[
            {
                "id": "ltp-smoketest",
                "type": "ltp",
                "timeout_s": 3600,
                "parameters": "tst_cmdfiles=smoketest skip_install=true",
            },
            {"id": "baseline-x86", "type": "baseline"},
        ])
        out = translate_job(jobdef, BASE_CONFIG, node_id="n1")
        tests = json.loads(out["test_config"]["vms"][0]["test_params"]["PULL_LABS_TESTS_JSON"])
        assert tests[0] == {
            "id": "ltp-smoketest",
            "type": "ltp",
            "parameters": "tst_cmdfiles=smoketest skip_install=true",
            "timeout_s": 3600,
        }
        assert tests[1]["id"] == "baseline-x86"
        assert tests[1]["parameters"] == ""
        assert tests[1]["timeout_s"] is None

    def test_no_tests_json_without_tests(self):
        out = translate_job(_job(tests=[]), BASE_CONFIG)
        assert "PULL_LABS_TESTS_JSON" not in out["test_config"]["vms"][0]["test_params"]

    def test_rootfs_artifact_passed_through(self):
        jobdef = _job(extra_artifacts={"rootfs": "https://x/rootfs.bin"})
        out = translate_job(jobdef, BASE_CONFIG)
        vm = out["test_config"]["vms"][0]
        assert vm["test_params"]["ROOTFS_URL"] == "https://x/rootfs.bin"

    def test_ramdisk_artifact_used_when_rootfs_absent(self):
        jobdef = _job(extra_artifacts={"ramdisk": "https://x/ramdisk.bin"})
        out = translate_job(jobdef, BASE_CONFIG)
        vm = out["test_config"]["vms"][0]
        assert vm["test_params"]["ROOTFS_URL"] == "https://x/ramdisk.bin"

    def test_timeout_propagated_as_max_runtime(self):
        out = translate_job(_job(timeout=900), BASE_CONFIG)
        assert out["test_config"]["vms"][0]["max_runtime"] == 900

    def test_default_timeout_when_missing(self):
        out = translate_job(_job(), BASE_CONFIG)
        assert out["test_config"]["vms"][0]["max_runtime"] == 3600

    def test_invalid_timeout_falls_back_to_default(self):
        out = translate_job(_job(timeout="not-a-number"), BASE_CONFIG)
        assert out["test_config"]["vms"][0]["max_runtime"] == 3600

    def test_role_name_preserved_from_base(self):
        out = translate_job(_job(), BASE_CONFIG, node_id="n1")
        assert out["test_config"]["role_name"] == "test-role"

    def test_test_id_includes_arch_and_short_node(self):
        out = translate_job(_job(), BASE_CONFIG, node_id="abcdef1234567890")
        tid = out["test_config"]["test_id"]
        assert tid.startswith("pulllab-x86_64-")
        # node_id is truncated to 12 chars
        assert tid.endswith("abcdef123456")

    def test_no_tests_block_uses_default_vm_test(self):
        out = translate_job(_job(tests=[]), BASE_CONFIG)
        assert out["test_config"]["vms"][0]["test"] == ["url-kernel-boot"]

    def test_base_config_not_mutated(self):
        before = {k: v for k, v in BASE_CONFIG["test_config"].items()}
        translate_job(_job(), BASE_CONFIG, node_id="n1")
        assert BASE_CONFIG["test_config"] == before


class TestTranslateJobErrors:
    """Failure modes for translate_job."""

    def test_missing_kernel_url_raises(self):
        jobdef = _job()
        del jobdef["artifacts"]["kernel"]
        with pytest.raises(ValueError, match="missing required artifacts"):
            translate_job(jobdef, BASE_CONFIG)

    def test_missing_modules_url_raises(self):
        jobdef = _job()
        del jobdef["artifacts"]["modules"]
        with pytest.raises(ValueError, match="missing required artifacts"):
            translate_job(jobdef, BASE_CONFIG)

    def test_custom_platform_map_override(self):
        custom_map = dict(DEFAULT_PLATFORM_MAP)
        custom_map["x86_64"] = {"instance_type": "m6i.large", "ami_id": "ami-custom"}
        out = translate_job(_job(), BASE_CONFIG, platform_map=custom_map)
        assert out["test_config"]["vms"][0]["instance_type"] == "m6i.large"
        assert out["test_config"]["vms"][0]["ami_id"] == "ami-custom"

    def test_custom_test_type_map_override(self):
        custom = {"ltp": "my-custom-ltp", "_default": "fallback"}
        jobdef = _job(tests=[{"id": "t1", "type": "ltp"}])
        out = translate_job(jobdef, BASE_CONFIG, test_type_map=custom)
        assert out["test_config"]["vms"][0]["test"] == ["my-custom-ltp"]

    def test_unknown_type_uses_default_key_from_custom_map(self):
        custom = {"_default": "fallback"}
        jobdef = _job(tests=[{"id": "t1", "type": "unknown"}])
        out = translate_job(jobdef, BASE_CONFIG, test_type_map=custom)
        assert out["test_config"]["vms"][0]["test"] == ["fallback"]

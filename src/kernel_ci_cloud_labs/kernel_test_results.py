# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Collabora Limited
"""Result contracts shared by VM payloads and the AWS-side verifier."""

import json
import re

# A VM test whose parameters carry the pull-lab request list is a native
# kernel test: its VM-side SUCCESS only counts after the AWS-side verifier
# accepts its structured evidence. Keyed on the contract, not directory names.
TESTS_PARAM = "PULL_LABS_TESTS_JSON"
BOOT_TEST_TYPES = ("boot", "baseline")
# Container-log line prefix carrying one JSON verdict per verified instance.
EVIDENCE_MARKER = "PULLAB_EVIDENCE "


def requires_evidence(test_params):
    return TESTS_PARAM in (test_params or {})


def parse_runtest_names(text):
    """Return test names from an LTP runtest file, as Kirk reads them.

    A runtest line is "<name> <command...>"; blank lines and lines starting
    with '#' are ignored. The command is never shell-parsed here.
    """
    names = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.split(None, 1)[0])
    return names


def validate_boot_evidence(data, kernel_url, arch, checksum=None):
    if data.get("status") != "pass" or data.get("kernel_url") != kernel_url:
        raise ValueError("Boot evidence does not identify the requested kernel artifact")
    expected_arch = {"aarch64": "arm64"}.get(arch, arch)
    if data.get("arch") != expected_arch:
        raise ValueError("Boot evidence has the wrong architecture")
    if not data.get("bootstrap_boot_id") or not data.get("boot_id") or data["boot_id"] == data["bootstrap_boot_id"]:
        raise ValueError("Boot evidence does not show a reboot")
    for key in ("release", "version"):
        if not data.get(key) or data[key] != data.get("requested_" + key):
            raise ValueError("Boot evidence has a different kernel " + key)
    image_hash = data.get("image_sha256", "")
    if not re.fullmatch(r"[a-f0-9]{64}", image_hash):
        raise ValueError("Boot evidence has no valid image hash")
    if "pullab.image_sha256=" + image_hash not in data.get("cmdline", "").split():
        raise ValueError("Boot evidence has no matching image marker")
    if checksum and data.get("download_sha256") != checksum:
        raise ValueError("Boot evidence does not match the requested artifact checksum")


def validate_kirk_report(report, expected_release, expected_tests=None, expected_version=None):
    """Kirk exit status alone does not describe individual test outcomes."""
    rows = report.get("results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Kirk returned no individual LTP results")
    reported_kernel = report.get("environment", {}).get("kernel")
    identity_matches = reported_kernel == expected_release
    if isinstance(reported_kernel, str) and reported_kernel.startswith("Linux "):
        release, separator, version = reported_kernel[len("Linux ") :].partition(" ")
        identity_matches = release == expected_release and bool(separator) and bool(version)
        if expected_version is not None:
            identity_matches = identity_matches and version == expected_version
    if not identity_matches:
        raise ValueError("LTP report was produced on a different kernel")
    if expected_tests is not None:
        if not expected_tests:
            raise ValueError("No LTP tests were selected")
        observed = {row.get("test_fqn") for row in rows}
        missing = set(expected_tests) - observed
        if missing:
            raise ValueError("LTP did not report all selected tests: " + ", ".join(sorted(missing)))
    passed = 0
    failed = []
    if any(int(report.get("stats", {}).get(key, 0)) for key in ("failed", "broken", "warnings")):
        raise ValueError("Kirk aggregate statistics contain failed/broken/warning results")
    for row in rows:
        test = row.get("test") or {}
        status = row.get("status") or test.get("result")
        if test.get("result") and test["result"] != status:
            raise ValueError("Kirk test status conflicts with its detailed result")
        if status not in ("pass", "conf", "skip") or any(
            int(test.get(key, 0)) for key in ("failed", "broken", "warnings")
        ):
            failed.append(row.get("test_fqn", "unknown"))
        if status == "pass":
            passed += 1
    if failed:
        raise ValueError("LTP failures/broken tests: " + ", ".join(failed))
    if not passed:
        raise ValueError("No LTP tests passed; empty/all-skipped runs are not passes")
    return {"passed": passed, "skipped": len(rows) - passed, "failed": 0}


def verify_test_evidence(test_params, read_report):
    """Check every requested native test of one VM against its evidence.

    *read_report(filename)* returns the parsed JSON artifact or raises.
    Returns a verdict dict: "boot" and "result" are "PASS"/"FAIL" (boot is
    judged separately so a later LTP failure cannot hide a good boot), and
    "reason" carries the first rejection message.
    """
    verdict = {"boot": "FAIL", "result": "FAIL", "boot_requested": False, "reason": ""}
    try:
        requests = json.loads(test_params[TESTS_PARAM])
        if not requests:
            raise ValueError("No requested kernel tests in VM configuration")
        verdict["boot_requested"] = any(request.get("type") in BOOT_TEST_TYPES for request in requests)
        evidence = read_report("results_boot.json")
        validate_boot_evidence(
            evidence,
            test_params["KERNEL_URL"],
            test_params["ARCH"],
            test_params.get("KERNEL_SHA256"),
        )
        verdict["boot"] = "PASS"
        for request in requests:
            if request["type"] == "ltp":
                ltp_report = read_report("results_ltp-" + request["id"] + ".json")
                selection = ltp_report.get("pullab") or {}
                if selection.get("requested_suites") != request["suites"]:
                    raise ValueError("LTP report does not identify the requested suites")
                if selection.get("image_sha256") != evidence["image_sha256"]:
                    raise ValueError("LTP report has a different kernel image hash")
                validate_kirk_report(
                    ltp_report,
                    evidence["release"],
                    selection.get("expected_tests", []),
                    expected_version=evidence["version"],
                )
            elif request["type"] not in BOOT_TEST_TYPES:
                raise ValueError("Unsupported native kernel test type: " + str(request["type"]))
        verdict["result"] = "PASS"
    except Exception as error:  # pylint: disable=broad-exception-caught
        verdict["reason"] = str(error) or type(error).__name__
    return verdict

# SPDX-License-Identifier: Apache-2.0
# pylint: disable=protected-access
"""Native kernel tests must distinguish real boots/tests from scaffold passes."""

import gzip
import base64
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from kernel_ci_cloud_labs.kernel_test_results import (
    parse_runtest_names,
    validate_boot_evidence,
    validate_kirk_report,
    verify_test_evidence,
)
from kernel_ci_cloud_labs.launch_vm import VMLauncher
from kernel_ci_cloud_labs.core.pipeline import parse_evidence_verdicts, parse_vm_logs

LIB = Path(__file__).parents[1] / "vm-tests/lib"
sys.path.insert(0, str(LIB))
spec = importlib.util.spec_from_file_location("url_kernel", LIB / "url_kernel.py")
kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kernel)

RELEASE = "7.3.0-next-test"
VERSION = "#2 SMP PREEMPT_DYNAMIC @1234567"
SHA = "a" * 64
URL = "https://files.example/kernel"


def evidence():
    return {
        "status": "pass",
        "kernel_url": URL,
        "arch": "arm64",
        "release": RELEASE,
        "requested_release": RELEASE,
        "version": VERSION,
        "requested_version": VERSION,
        "boot_id": "after",
        "bootstrap_boot_id": "before",
        "image_sha256": SHA,
        "download_sha256": "b" * 64,
        "cmdline": "root=UUID=root pullab.image_sha256=" + SHA,
    }


def report(status="pass", kernel_release=RELEASE):
    return {
        "environment": {"kernel": kernel_release},
        "results": [{"test_fqn": "fork01", "status": status, "test": {"failed": 0, "broken": 0, "warnings": 0}}],
    }


def write_vm_log(directory, instance_id, test="url-kernel-ltp"):
    (directory / f"{instance_id}.log").write_text(
        f"test_{test}/input/payload.zip\nTest execution completed: SUCCESS\n"
    )


def test_rejected_native_evidence_cannot_become_a_kernelci_pass(tmp_path):
    write_vm_log(tmp_path, "i-test", "url-kernel-boot")
    result = parse_vm_logs(tmp_path, evidence_tests={"url-kernel-boot"}, verdicts={})
    assert result["successful"] == 0
    assert result["instances"][0]["status"] == "FAIL"


def test_native_verdicts_are_judged_per_instance(tmp_path):
    write_vm_log(tmp_path, "i-good")
    write_vm_log(tmp_path, "i-bad")
    write_vm_log(tmp_path, "i-other", "basic-test")
    verdicts = {
        "i-good": {"result": "PASS", "boot": "PASS", "boot_requested": True},
        "i-bad": {"result": "FAIL", "boot": "PASS", "boot_requested": True},
    }
    result = parse_vm_logs(tmp_path, evidence_tests={"url-kernel-ltp"}, verdicts=verdicts)
    by_id = {entry["instance_id"]: entry for entry in result["instances"]}
    assert by_id["i-good"]["status"] == "PASS" and by_id["i-good"]["boot_status"] == "PASS"
    # A failing sibling VM does not reject a verified one, and an LTP failure
    # does not take the boot result down with it.
    assert by_id["i-bad"]["status"] == "FAIL" and by_id["i-bad"]["boot_status"] == "PASS"
    assert by_id["i-other"]["status"] == "PASS" and "boot_status" not in by_id["i-other"]


def test_evidence_verdicts_are_read_from_the_container_log(tmp_path):
    log = tmp_path / "container.log"
    log.write_text(
        "[2026-11-03 10:00:00] [CONTAINER] INFO: unrelated\n"
        '[2026-11-03 10:00:01] [CONTAINER] PULLAB_EVIDENCE {"instance_id": "i-a", "result": "PASS"}\n'
        "[2026-11-03 10:00:02] [CONTAINER] PULLAB_EVIDENCE not-json\n"
    )
    assert parse_evidence_verdicts(log) == {"i-a": {"instance_id": "i-a", "result": "PASS"}}
    assert parse_evidence_verdicts(tmp_path / "missing.log") == {}


def test_kirk_conflicting_results_and_aggregate_failures_are_rejected():
    data = report()
    data["stats"] = {"failed": 1}
    with pytest.raises(ValueError):
        validate_kirk_report(data, RELEASE)
    data.pop("stats")
    data["results"][0]["test"]["result"] = "brok"
    with pytest.raises(ValueError):
        validate_kirk_report(data, RELEASE)


def make_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            if isinstance(value, tuple):
                member.type, member.linkname = value
                archive.addfile(member)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))


def test_arm64_image_identity_is_read_from_the_supplied_binary():
    image = bytearray(64)
    image[56:60] = b"ARMd"
    image += ("Linux version " + RELEASE + " (builder@host) (GCC 15) " + VERSION + "\0").encode()
    raw, identity = kernel.image_identity(gzip.compress(image))
    assert raw == image
    assert identity == {"arch": "arm64", "release": RELEASE, "version": VERSION}


def test_x86_image_uses_boot_protocol_version_pointer():
    image = bytearray(0x1000)
    image[0x202:0x206] = b"HdrS"
    struct.pack_into("<H", image, 0x236, 1)
    struct.pack_into("<H", image, 0x20E, 0x100)
    banner = (RELEASE + " (builder@host) " + VERSION + "\0").encode()
    image[0x300 : 0x300 + len(banner)] = banner
    _, identity = kernel.image_identity(image)
    assert identity == {"arch": "x86_64", "release": RELEASE, "version": VERSION}
    struct.pack_into("<H", image, 0x236, 0)
    with pytest.raises(ValueError, match="64-bit"):
        kernel.image_identity(image)


@pytest.mark.parametrize(
    "field,value",
    [
        ("boot_id", "before"),
        ("release", "6.18.amzn"),
        ("version", "#1 distro build"),
        ("cmdline", "root=UUID=root"),
        ("image_sha256", "b" * 64),
    ],
)
def test_no_reboot_or_ami_fallback_is_not_a_boot_pass(field, value):
    state = {"release": RELEASE, "version": VERSION, "bootstrap_boot_id": "before", "image_sha256": SHA}
    actual = {
        "release": RELEASE,
        "version": VERSION,
        "boot_id": "after",
        "cmdline": "pullab.image_sha256=" + SHA,
        "image_sha256": SHA,
    }
    actual[field] = value
    with pytest.raises(ValueError):
        kernel.verify_identity(
            state, actual["release"], actual["version"], actual["boot_id"], actual["cmdline"], actual["image_sha256"]
        )


def test_matching_boot_evidence_passes():
    validate_boot_evidence(evidence(), URL, "aarch64", "b" * 64)


@pytest.mark.parametrize(
    "field,value",
    [
        ("kernel_url", "https://other/kernel"),
        ("arch", "x86_64"),
        ("requested_release", "different"),
        ("status", "skip"),
        ("image_sha256", "invalid"),
        ("boot_id", "before"),
        ("bootstrap_boot_id", ""),
    ],
)
def test_incomplete_or_unrelated_evidence_is_rejected(field, value):
    data = evidence()
    data[field] = value
    with pytest.raises(ValueError):
        validate_boot_evidence(data, URL, "arm64")


@pytest.mark.parametrize("status", ["fail", "brok", "warn", "unknown", "conf", "skip"])
def test_kirk_exit_zero_is_insufficient_for_passing_tests(status):
    with pytest.raises(ValueError):
        validate_kirk_report(report(status), RELEASE)


def test_kirk_mixed_pass_and_config_skip_is_valid():
    data = report()
    data["results"].extend(report("conf")["results"])
    assert validate_kirk_report(data, RELEASE) == {"passed": 1, "skipped": 1, "failed": 0}


def test_kirk_report_must_match_the_booted_kernel():
    with pytest.raises(ValueError, match="different kernel"):
        validate_kirk_report(report(kernel_release="6.18.amzn"), RELEASE)


def test_kirk_full_uname_identity_matches_the_verified_release_and_build():
    data = report(kernel_release=f"Linux {RELEASE} {VERSION}")
    assert validate_kirk_report(data, RELEASE, ["fork01"], expected_version=VERSION)["passed"] == 1
    with pytest.raises(ValueError, match="different kernel"):
        validate_kirk_report(data, RELEASE, expected_version="#1 different build")
    with pytest.raises(ValueError, match="different kernel"):
        validate_kirk_report(data, "other-release", expected_version=VERSION)


def test_kirk_empty_report_is_not_a_pass():
    with pytest.raises(ValueError):
        validate_kirk_report({"environment": {"kernel": RELEASE}, "results": []}, RELEASE)


def test_kirk_failure_counters_override_a_pass_label():
    data = report()
    data["results"][0]["test"]["failed"] = 1
    with pytest.raises(ValueError):
        validate_kirk_report(data, RELEASE)


def test_partial_suite_cannot_pass_after_timeout():
    with pytest.raises(ValueError, match="all selected tests"):
        validate_kirk_report(report(), RELEASE, ["fork01", "missing02"])


def test_explicit_empty_selection_is_not_a_pass():
    with pytest.raises(ValueError, match="No LTP tests"):
        validate_kirk_report(report(), RELEASE, [])


@pytest.mark.parametrize(
    "name,entry",
    [("../escape", b"bad"), ("link", (tarfile.SYMTYPE, "../../escape")), ("link", (tarfile.LNKTYPE, "../escape"))],
)
def test_archives_cannot_write_outside_staging(tmp_path, name, entry):
    archive = tmp_path / "bad.tar.gz"
    make_archive(archive, {name: entry})
    with pytest.raises(ValueError):
        kernel.safe_extract(archive, tmp_path / "destination")
    assert not (tmp_path / "escape").exists()


def test_guest_absolute_links_remain_within_guest(tmp_path):
    archive = tmp_path / "rootfs.tar.gz"
    make_archive(archive, {"usr/bin/tool": b"tool", "bin": (tarfile.SYMTYPE, "/usr/bin")})
    root = tmp_path / "root"
    kernel.safe_extract(archive, root)
    assert (root / "bin/tool").read_bytes() == b"tool"
    assert (root / "bin").resolve() == root / "usr/bin"


def test_module_build_machine_links_are_not_installed(tmp_path):
    archive = tmp_path / "modules.tar.gz"
    make_archive(
        archive,
        {
            "lib/modules/" + RELEASE + "/kernel/test.ko": b"module",
            "lib/modules/" + RELEASE + "/build": (tarfile.SYMTYPE, "/builder/linux"),
        },
    )
    root = tmp_path / "modules"
    kernel.safe_extract(archive, root, modules=True)
    assert kernel.modules_release(root) == RELEASE
    assert not (root / "lib/modules" / RELEASE / "build").is_symlink()


def test_skipfile_conditions_do_not_skip_other_platforms_or_branches():
    data = {
        "skiplist": [
            {"boards": "all", "branches": "all", "environments": "all", "tests": ["always"]},
            {"boards": ["aws-ec2-arm64"], "branches": ["main"], "environments": ["staging"], "tests": ["specific"]},
            {"boards": ["different"], "tests": ["other"]},
        ]
    }
    assert kernel.select_skip_tests(
        data, {"platform": "aws-ec2-arm64", "branch": "main", "environment": "staging"}
    ) == ["always", "specific"]
    assert kernel.select_skip_tests(
        data, {"platform": "aws-ec2-arm64", "branch": "stable", "environment": "production"}
    ) == ["always"]


def launcher(objects, test="url-kernel-boot", requests=None):
    instance = object.__new__(VMLauncher)
    instance.test = test
    instance.run_prefix = "run_test"
    instance.instance_id = "i-test"
    instance.s3_bucket = "bucket"
    instance.test_params = {
        "KERNEL_URL": URL,
        "ARCH": "arm64",
        "PULL_LABS_TESTS_JSON": json.dumps(requests or [{"id": "boot", "type": "boot"}]),
    }

    class Missing(Exception):
        pass

    def get_object(**kwargs):
        name = kwargs["Key"].rsplit("/", 1)[-1]
        if name not in objects:
            raise Missing(name)
        value = objects[name]
        return {"Body": io.BytesIO(value.encode() if isinstance(value, str) else json.dumps(value).encode())}

    instance.s3 = SimpleNamespace(get_object=get_object, exceptions=SimpleNamespace(NoSuchKey=Missing))
    return instance


@pytest.mark.parametrize("text", ["FAILED: SUCCESS was not reached", "NOT SUCCESS", "SUCCESS extra"])
def test_result_file_requires_exact_success(text):
    assert not launcher({"result.txt": text}).check_test_result()


def test_legacy_scaffold_success_without_boot_evidence_fails():
    assert not launcher({"result.txt": "SUCCESS"}).check_test_result()


def test_real_boot_evidence_and_success_are_accepted():
    assert launcher({"result.txt": "SUCCESS", "results_boot.json": evidence()}).check_test_result()


def test_terminated_vm_console_uses_buffered_snapshot_when_live_buffer_is_empty():
    instance = launcher({})
    instance._console_captured = False
    instance.ec2 = Mock()
    instance.ec2.get_console_output.side_effect = [{}, {"Output": base64.b64encode(b"Booting KernelCI 7.3\n").decode()}]
    instance.s3 = Mock()
    instance.capture_console_output(reason="post-terminate")
    assert instance.ec2.get_console_output.call_args_list[1].kwargs["Latest"] is False
    assert instance.s3.put_object.call_args.kwargs["Body"] == b"Booting KernelCI 7.3\n"


def test_ltp_success_requires_its_report_and_no_failed_cases():
    objects = {"result.txt": "SUCCESS", "results_boot.json": evidence()}
    requests = [{"id": "ltp-smoke", "type": "ltp", "suites": ["smoketest"]}]
    assert not launcher(objects, "url-kernel-ltp", requests).check_test_result()
    objects["results_ltp-ltp-smoke.json"] = report("fail")
    assert not launcher(objects, "url-kernel-ltp", requests).check_test_result()
    objects["results_ltp-ltp-smoke.json"] = report()
    objects["results_ltp-ltp-smoke.json"]["pullab"] = {
        "expected_tests": ["fork01"],
        "requested_suites": ["smoketest"],
        "image_sha256": SHA,
    }
    assert launcher(objects, "url-kernel-ltp", requests).check_test_result()


def test_suite_selection_applies_skip_patterns_but_cannot_skip_everything(tmp_path):
    source = tmp_path / "opt/ltp/runtest"
    source.mkdir(parents=True)
    (source / "smoketest").write_text('# comment\nfork01 fork01\npipe01 echo "OK" | pipe01\n')
    skip = tmp_path / "skip"
    skip.write_text("^pipe01$\n")
    assert kernel.selected_ltp_tests(tmp_path, {"suites": ["smoketest"]}, skip) == ["fork01"]
    skip.write_text(".*\n")
    with pytest.raises(ValueError, match="no LTP tests"):
        kernel.selected_ltp_tests(tmp_path, {"suites": ["smoketest"]}, skip)


def test_ltp_mount_setup_refuses_to_run_outside_its_pid_namespace(tmp_path):
    with pytest.raises(ValueError, match="PID namespace"):
        kernel.ltp_child(tmp_path, tmp_path, tmp_path / "request.json")


def test_namespace_signal_reset_makes_executed_helpers_terminable():
    # Reproduce unshare 2.37's ignored SIGTERM in an isolated subprocess,
    # then verify a guest-like external helper exits promptly on SIGTERM.
    code = f"""
import signal, subprocess, sys
sys.path.insert(0, {str(LIB)!r})
import url_kernel
signal.signal(signal.SIGTERM, signal.SIG_IGN)
url_kernel.reset_namespace_signals()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
try:
    child.terminate()
    assert child.wait(timeout=2) == -signal.SIGTERM
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5)


@pytest.mark.parametrize("name", ["url-kernel-boot", "url-kernel-ltp"])
def test_actual_payload_packager_includes_independent_two_stage_runner(tmp_path, name):
    import zipfile
    from botocore.exceptions import ClientError
    from kernel_ci_cloud_labs.storage.s3_storage import S3Storage

    storage = object.__new__(S3Storage)
    storage.bucket = "bucket"
    storage.run_prefix = "run_test"
    storage.s3 = Mock()
    storage.s3.head_object.side_effect = ClientError({"Error": {"Code": "404"}}, "HeadObject")
    storage.copy_external_requirements = Mock()
    storage.save_file = Mock()
    storage.upload_test_payload(name, test_dir=str(Path(__file__).parents[1] / "vm-tests"))
    zipped = storage.save_file.call_args.args[1]
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        names = archive.namelist()
        assert "run.sh" not in names
        assert "run-01-install-kernel.sh" in names and "run-02-verify-and-test.sh" in names
        assert "kernel_test_results.py" in names and "url_kernel.py" in names
        archive.extractall(tmp_path)
    # A payload must import on its own, without the installed poller package.
    subprocess.run(
        [sys.executable, "-I", "-c", "import sys;sys.path.insert(0,sys.argv[1]);import url_kernel", str(tmp_path)],
        check=True,
    )


def verdict_line(capsys):
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("PULLAB_EVIDENCE ")]
    assert len(lines) == 1
    return json.loads(lines[0].split(" ", 1)[1])


def test_boot_is_reported_separately_when_ltp_fails(capsys):
    objects = {"result.txt": "FAILED", "results_boot.json": evidence()}
    requests = [{"id": "baseline", "type": "baseline"}, {"id": "ltp", "type": "ltp", "suites": ["smoketest"]}]
    assert not launcher(objects, "url-kernel-ltp", requests).check_test_result()
    verdict = verdict_line(capsys)
    assert verdict["instance_id"] == "i-test"
    assert (verdict["boot"], verdict["result"], verdict["boot_requested"]) == ("PASS", "FAIL", True)


def test_missing_evidence_reports_the_real_reason(capsys):
    assert not launcher({"result.txt": "SUCCESS"}).check_test_result()
    verdict = verdict_line(capsys)
    assert verdict["boot"] == "FAIL"
    assert "results_boot.json" in str(verdict["reason"])
    assert "result.txt" not in capsys.readouterr().err


def test_evidence_verdict_rejects_a_different_image_hash():
    ltp = report()
    ltp["pullab"] = {"expected_tests": ["fork01"], "requested_suites": ["smoketest"], "image_sha256": "c" * 64}
    files = {"results_boot.json": evidence(), "results_ltp-ltp.json": ltp}
    params = {
        "KERNEL_URL": URL,
        "ARCH": "arm64",
        "PULL_LABS_TESTS_JSON": json.dumps([{"id": "ltp", "type": "ltp", "suites": ["smoketest"]}]),
    }
    verdict = verify_test_evidence(params, files.__getitem__)
    assert (verdict["boot"], verdict["result"], verdict["boot_requested"]) == ("PASS", "FAIL", False)
    assert "image hash" in str(verdict["reason"])


def test_runtest_names_match_kirk_parsing_without_shell_quoting():
    text = "# header\n\n  fork01 fork01\nquote01 echo \"unbalanced\nhash01 echo a#b # trailing\n"
    assert parse_runtest_names(text) == ["fork01", "quote01", "hash01"]


def test_boot_entry_uses_grubby_id_for_one_shot_boot():
    info = 'index=2\nkernel="/boot/vmlinuz-pullab-abc"\ntitle="KernelCI 7.3"\nid="ec2abc-7.3.0"\n'
    assert kernel.boot_entry(info, "/boot/vmlinuz-pullab-abc") == "ec2abc-7.3.0"
    with pytest.raises(ValueError, match="no entry"):
        kernel.boot_entry(info, "/boot/vmlinuz-other")


def test_streamed_ltp_output_reaches_console_and_log_with_heartbeat(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(kernel, "HEARTBEAT_SECONDS", 0.2)
    log = tmp_path / "ltp.log"
    command = [sys.executable, "-c", "import time; print('fork01 pass', flush=True); time.sleep(0.5)"]
    assert kernel.run_streamed(command, log, 10, "ltp-x") == 0
    out = capsys.readouterr().out
    assert "[ltp-x] fork01 pass" in out and "[ltp-x] still running" in out
    assert log.read_text() == "fork01 pass\n"


def test_streamed_ltp_is_killed_at_its_total_budget(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert kernel.run_streamed(command, tmp_path / "ltp.log", 0.3, "ltp-x") is None

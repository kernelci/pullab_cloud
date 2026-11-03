#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 Collabora Limited
"""Install a supplied kernel, verify the reboot, and run requested LTP suites.

This file is included in each VM payload. Keep it compatible with AL2023's
system Python 3.9; it must work before the project's Python package is installed.
"""

import argparse
import gzip
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path, PurePosixPath

from kernel_test_results import parse_runtest_names, validate_boot_evidence, validate_kirk_report


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def write_json(path, data):
    path = Path(path)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o644)


def download(url, destination, expected_hash=None):
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("Artifact URL must use HTTP(S)")
    destination = Path(destination)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            if destination.stat().st_size == 0:
                raise ValueError("Downloaded artifact is empty")
            if expected_hash and digest(destination) != expected_hash:
                raise ValueError("Artifact SHA256 does not match the job definition")
            return
        except OSError:
            if attempt == 2:
                raise
            time.sleep(2)


def image_identity(data):
    """Read the release/build identity embedded in x86 bzImage or arm64 Image."""
    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    if data[0x202:0x206] == b"HdrS":
        if not struct.unpack_from("<H", data, 0x236)[0] & 1:
            raise ValueError("Expected a 64-bit x86 kernel image")
        offset = struct.unpack_from("<H", data, 0x20E)[0] + 0x200
        banner = data[offset : offset + 4096].split(b"\0", 1)[0].decode("ascii")
        arch = "x86_64"
        release = banner.split(" ", 1)[0]
    elif data[56:60] == b"ARMd":
        match = re.search(rb"Linux version ([^\x00\r\n]+)", data)
        if not match:
            raise ValueError("arm64 image has no Linux version banner")
        banner = match.group(1).decode("ascii")
        release = banner.split(" ", 1)[0]
        arch = "arm64"
    else:
        raise ValueError("Unsupported kernel image; expected x86 bzImage or arm64 Image[.gz]")
    version = re.search(r"(#[0-9]+ .+)$", banner)
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", release) or not version:
        raise ValueError("Kernel image has no verifiable release/build version")
    return data, {"arch": arch, "release": release, "version": version.group(1)}


def member_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Archive member escapes extraction directory: " + name)
    return str(path)


def safe_extract(archive, destination, modules=False):
    """Extract data without allowing archive links to write outside the destination.

    Absolute *guest* symlinks become equivalent relative links. Module build/
    source symlinks are intentionally omitted; they refer to the build machine.
    Device nodes in a rootfs are omitted because /dev is mounted at runtime.
    """
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source:
            name = member_name(member.name)
            if name == ".":
                continue
            parts = PurePosixPath(name).parts
            if modules:
                valid = name in ("lib", "usr", "usr/lib") or name.startswith(("lib/modules", "usr/lib/modules"))
                if not valid:
                    raise ValueError("Unexpected file in modules archive: " + name)
                if member.issym() and parts[-1] in ("build", "source"):
                    continue
            target = (destination / name).resolve()
            if not target.is_relative_to(destination):
                raise ValueError("Archive member resolves outside extraction directory: " + name)
            member.name = name
            if member.issym():
                link = member.linkname
                if link.startswith("/"):
                    link = posixpath.relpath(link.lstrip("/"), posixpath.dirname(name) or ".")
                linked = (target.parent / link).resolve()
                if not linked.is_relative_to(destination):
                    raise ValueError("Archive symlink escapes extraction directory: " + name)
                member.linkname = link
            elif member.islnk():
                member.linkname = member_name(member.linkname)
                linked = destination / member.linkname
                if not linked.resolve().is_relative_to(destination):
                    raise ValueError("Archive hardlink escapes extraction directory: " + name)
                # Avoid tarfile's fallback extraction of a not-yet-validated
                # forward link target. Normal tar hardlinks follow their file.
                if linked.is_symlink() or not linked.is_file():
                    raise ValueError("Archive hardlink target must be an earlier regular file: " + name)
            elif not (member.isfile() or member.isdir()):
                if not modules and parts[0] == "dev":
                    continue
                raise ValueError("Unsupported archive member: " + name)
            # Preserve guest permissions (including sticky/suid bits required by
            # LTP). Extraction lives in a root-only directory on a disposable VM.
            member.mode &= 0o7777
            if modules:
                member.uid = member.gid = 0
                member.uname = member.gname = "root"
            # All path/link checks above are required on Python versions predating
            # tarfile's extraction filters. Do not rely on the interpreter default.
            if hasattr(tarfile, "fully_trusted_filter"):
                source.extract(member, destination, filter="fully_trusted")
            else:
                source.extract(member, destination)


def modules_release(directory):
    roots = [Path(directory) / prefix for prefix in ("lib/modules", "usr/lib/modules")]
    releases = {child.name for root in roots if root.is_dir() for child in root.iterdir() if child.is_dir()}
    if len(releases) != 1:
        raise ValueError("Modules archive must contain exactly one kernel release")
    return releases.pop()


def run(command, **kwargs):
    command = [str(argument) for argument in command]
    print("+", " ".join(command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def install(state_path):
    state_path = Path(state_path).resolve()
    stage = state_path.parent / "kernel-artifacts"
    stage.mkdir(mode=0o700, exist_ok=True)
    requested_arch = {"aarch64": "arm64"}.get(os.environ.get("ARCH"), os.environ.get("ARCH"))
    machine = {"aarch64": "arm64"}.get(platform.machine(), platform.machine())
    if requested_arch != machine or machine not in ("x86_64", "arm64"):
        raise ValueError("Requested kernel architecture does not match the VM")
    download(os.environ["KERNEL_URL"], stage / "kernel.download", os.environ.get("KERNEL_SHA256"))
    download(os.environ["MODULES_URL"], stage / "modules.tar", os.environ.get("MODULES_SHA256"))
    image, identity = image_identity((stage / "kernel.download").read_bytes())
    if identity["arch"] != machine:
        raise ValueError("Downloaded kernel image architecture does not match the VM")
    safe_extract(stage / "modules.tar", stage / "modules", modules=True)
    release = modules_release(stage / "modules")
    if release != identity["release"]:
        raise ValueError("Kernel image and modules have different releases")
    if identity["release"] == platform.release() and identity["version"] == platform.version():
        raise ValueError("Requested kernel is indistinguishable from the bootstrap kernel")
    image_hash = hashlib.sha256(image).hexdigest()
    kernel = Path("/boot") / ("vmlinuz-pullab-" + image_hash[:20])
    initrd = Path("/boot") / ("initramfs-pullab-" + image_hash[:20] + ".img")
    kernel.write_bytes(image)
    kernel.chmod(0o644)
    module_source = next(
        root / release
        for root in [stage / "modules/lib/modules", stage / "modules/usr/lib/modules"]
        if (root / release).is_dir()
    )
    module_dest = Path("/lib/modules") / release
    if module_dest.exists():
        raise ValueError("Refusing to overwrite an existing module tree: " + release)
    shutil.copytree(module_source, module_dest, symlinks=True)
    # The temporary VM retains its AMI userspace and management agent. Its
    # initramfs must contain drivers for the supplied kernel, never the AMI's.
    run(["depmod", "-a", release])
    run(["dracut", "--force", "--no-hostonly", "--gzip", "--kver", release, "--kernel-image", kernel, initrd])
    marker = "pullab.image_sha256=" + image_hash
    run(
        [
            "grubby",
            "--add-kernel=" + str(kernel),
            "--initrd=" + str(initrd),
            "--copy-default",
            "--title=KernelCI " + release,
            "--args=" + marker + " fips=0 selinux=0 panic=30",
        ]
    )
    run(["grubby", "--update-kernel=" + str(kernel), "--remove-args=fips=1 rd.fips=1 selinux=1"])
    # Boot the supplied kernel exactly once. The AMI kernel stays the default,
    # so a kernel that panics (panic=30) reboots into the AMI, the management
    # agent reconnects, and verification fails fast as a boot failure instead
    # of the VM looping on a broken kernel until the job times out.
    entry = boot_entry(subprocess.check_output(["grubby", "--info=" + str(kernel)], text=True), kernel)
    run(["grub2-reboot", entry])
    grubenv = subprocess.check_output(["grub2-editenv", "list"], text=True).splitlines()
    if "next_entry=" + entry not in grubenv:
        raise ValueError("Bootloader did not schedule a one-time boot of the requested kernel")
    state = dict(
        identity,
        image_sha256=image_hash,
        kernel_path=str(kernel),
        download_sha256=digest(stage / "kernel.download"),
        bootstrap_release=platform.release(),
        bootstrap_version=platform.version(),
        bootstrap_boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        kernel_url=os.environ["KERNEL_URL"],
        modules_url=os.environ["MODULES_URL"],
    )
    write_json(state_path, state)
    run(["sync"])
    print("Kernel staged and selected. The VM client must reboot before verification.", flush=True)


def boot_entry(info, kernel):
    """Return the bootloader entry id (or title) grubby reports for *kernel*."""
    fields = {}
    for line in info.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields.setdefault(key.strip(), value.strip().strip('"'))
    if Path(fields.get("kernel", "")).name != Path(kernel).name:
        raise ValueError("Bootloader has no entry for the requested kernel")
    entry = fields.get("id") or fields.get("title")
    if not entry:
        raise ValueError("Bootloader entry for the requested kernel has no id or title")
    return entry


def verify_identity(state, release, version, boot_id, cmdline, image_hash):
    """An AMI fallback, missing reboot, changed image, or wrong build must fail."""
    if boot_id == state["bootstrap_boot_id"]:
        raise ValueError("VM did not reboot after kernel installation")
    if release != state["release"] or version != state["version"]:
        raise ValueError("Running kernel release/build differs from the requested image")
    if "pullab.image_sha256=" + state["image_sha256"] not in cmdline.split():
        raise ValueError("Booted kernel has no matching image marker")
    if image_hash != state["image_sha256"]:
        raise ValueError("Installed kernel bytes changed after staging")
    return {
        "status": "pass",
        "release": release,
        "version": version,
        "boot_id": boot_id,
        "image_sha256": image_hash,
        "cmdline": cmdline.strip(),
    }


def verify(state_path):
    state = json.loads(Path(state_path).read_text())
    machine = {"aarch64": "arm64"}.get(platform.machine(), platform.machine())
    if machine != state["arch"]:
        raise ValueError("Running kernel architecture differs from the requested image")
    result = verify_identity(
        state,
        platform.release(),
        platform.version(),
        Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        Path("/proc/cmdline").read_text(),
        digest(state["kernel_path"]),
    )
    result.update(
        arch=state["arch"],
        kernel_url=state["kernel_url"],
        requested_release=state["release"],
        requested_version=state["version"],
        bootstrap_boot_id=state["bootstrap_boot_id"],
        download_sha256=state["download_sha256"],
    )
    validate_boot_evidence(result, state["kernel_url"], state["arch"], os.environ.get("KERNEL_SHA256"))
    write_json(Path(state_path).parent / "results_boot.json", result)
    print("Verified requested kernel:", result["release"], result["version"], flush=True)
    return state


def root_path(root, guest_path):
    result = (Path(root) / guest_path.lstrip("/")).resolve()
    if not result.is_relative_to(Path(root).resolve()):
        raise ValueError("Guest path escapes root filesystem")
    return result


def select_skip_tests(data, context):
    """Apply skipgen-format selectors instead of treating YAML as a regex file."""
    tests = []
    for entry in data.get("skiplist", []):
        matches = True
        for key, actual in (
            ("boards", context.get("platform", "")),
            ("branches", context.get("branch", "")),
            ("environments", context.get("environment", "")),
        ):
            selector = entry.get(key, "all")
            if isinstance(selector, str):
                selector = [selector]
            if "all" not in selector and actual not in selector:
                matches = False
        if matches:
            tests.extend(entry.get("tests", []))
    if any(not isinstance(test, str) for test in tests):
        raise ValueError("Invalid skipfile test name")
    return sorted(set(tests))


def prepare_skipfile(request, root, output, context):
    name = request.get("skipfile")
    if not name or name == "/dev/null":
        return None
    source = root_path(root, name if name.startswith("/") else "/opt/ltp/" + name)
    if not source.is_file():
        sources = json.loads(os.environ.get("PULL_LABS_SKIPFILES_JSON", "{}"))
        if name not in sources:
            raise ValueError("Requested skipfile is absent from rootfs and has no configured URL: " + name)
        source = output / "skipfile.input"
        download(sources[name], source)
    if name.endswith((".yaml", ".yml")):
        import yaml  # Installed on the bootstrap VM, not assumed inside the rootfs.

        tests = select_skip_tests(yaml.safe_load(source.read_text()), context)
        # YAML test names are exact names, not regular expressions.
        text = "\n".join("^" + re.escape(test) + "$" for test in tests) + "\n"
    else:
        text = source.read_text()
        for line in text.splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                re.compile(line.strip())
    target = output / "skipfile"
    target.write_text(text)
    return "/run/pullab-results/skipfile"


def reset_namespace_signals():
    """Ensure guest helpers can be stopped after unshare --fork on AL2023."""
    # util-linux 2.37 --fork ignores these before forking. Exec preserves
    # ignored dispositions, causing LTP shell cleanup's SIGTERM to be ignored
    # by tst_timeout_kill. Restore defaults before starting any guest process.
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


def ltp_child(root, output, request_path):
    """Runs in a private mount/PID namespace; all mounts die with the child."""
    if os.getpid() != 1:
        raise ValueError("LTP child must run as init inside its disposable PID namespace")
    reset_namespace_signals()
    root, output = Path(root), Path(output)
    request = json.loads(Path(request_path).read_text())
    run(["mount", "--make-rprivate", "/"])
    proc = root_path(root, "/proc")
    proc.mkdir(parents=True, exist_ok=True)
    # util-linux 2.37's --mount-proc=<path> tries to change propagation
    # before mounting, which fails for a new (not-yet-mounted) guest /proc.
    run(["mount", "-t", "proc", "-o", "nosuid,noexec,nodev", "proc", proc])
    for source, guest in [
        ("/dev", "/dev"),
        ("/sys", "/sys"),
        ("/lib/modules", "/lib/modules"),
        (str(output), "/run/pullab-results"),
    ]:
        target = root_path(root, guest)
        target.mkdir(parents=True, exist_ok=True)
        run(["mount", "--rbind", source, target])
        run(["mount", "--make-rslave", target])
    runner = request["runner"]
    if not runner.startswith("/") or not root_path(root, runner).is_file():
        raise ValueError("Requested Kirk runner is absent from the rootfs")
    for suite in request["suites"]:
        if not root_path(root, "/opt/ltp/runtest/" + suite).is_file():
            raise ValueError("Requested LTP suite is absent from rootfs: " + suite)
    workers = (os.cpu_count() or 1) if request["workers"] == "max" else int(request["workers"])
    command = [
        "chroot",
        root,
        "/usr/bin/env",
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin:/opt/ltp/testcases/bin",
        "LTPROOT=/opt/ltp",
        "LTP_COLORIZE_OUTPUT=0",
        runner,
        "--no-colors",
        "--run-suite",
    ] + request["suites"]
    command += [
        "--json-report",
        "/run/pullab-results/report.json",
        "--workers",
        str(min(workers, 128)),
        "--suite-timeout",
        str(request["timeout_s"]),
    ]
    if request.get("resolved_skipfile"):
        command += ["--skip-file", request["resolved_skipfile"]]
    # timeout_s is the request's total budget, enforced by execute_ltp() across
    # all suites. No single suite can usefully exceed it, so it also bounds
    # Kirk's per-suite timeout.
    run(command)


def selected_ltp_tests(root, request, skipfile):
    patterns = []
    if skipfile:
        for line in Path(skipfile).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                patterns.append(re.compile(line.strip()))
    selected = set()
    for suite in request["suites"]:
        source = root_path(root, "/opt/ltp/runtest/" + suite)
        if not source.is_file():
            raise ValueError("Requested LTP suite is absent from rootfs: " + suite)
        for name in parse_runtest_names(source.read_text()):
            if not any(pattern.search(name) for pattern in patterns):
                selected.add(name)
    if not selected:
        raise ValueError("Requested suites/skipfile selected no LTP tests")
    return sorted(selected)


HEARTBEAT_SECONDS = 300


def run_streamed(command, log_path, timeout, label):
    """Run *command*, copying its output to *log_path* and to stdout.

    Stdout is the management agent's live console stream, which the AWS side
    watches for hangs; long, quiet LTP tests therefore also get a periodic
    heartbeat. Returns the exit code, or None after killing the process group
    when *timeout* seconds elapse.
    """
    with Path(log_path).open("w") as log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            errors="replace",
        )

        def copy_output():
            for line in process.stdout:
                log.write(line)
                log.flush()
                sys.stdout.write("[" + label + "] " + line)
                sys.stdout.flush()

        reader = threading.Thread(target=copy_output, daemon=True)
        reader.start()
        start = time.monotonic()
        deadline = start + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                reader.join(10)
                return None
            try:
                code = process.wait(timeout=min(remaining, HEARTBEAT_SECONDS))
                break
            except subprocess.TimeoutExpired:
                print("[%s] still running after %ds" % (label, time.monotonic() - start), flush=True)
        reader.join(10)
        return code


def execute_ltp(state_path, state, request, output_dir):
    stage = Path(state_path).parent / "kernel-artifacts"
    root = stage / "rootfs"
    if not root.is_dir():
        download(os.environ["ROOTFS_URL"], stage / "rootfs.tar", os.environ.get("ROOTFS_SHA256"))
        safe_extract(stage / "rootfs.tar", root)
    output = stage / ("ltp-" + request["id"])
    output.mkdir()
    context = json.loads(os.environ.get("PULL_LABS_CONTEXT_JSON", "{}"))
    request["resolved_skipfile"] = prepare_skipfile(request, root, output, context)
    expected_tests = selected_ltp_tests(root, request, output / "skipfile" if request["resolved_skipfile"] else None)
    request_file = output / "request.json"
    write_json(request_file, request)
    (root / "proc").mkdir(exist_ok=True)
    command = [
        "unshare",
        "--mount",
        "--pid",
        "--fork",
        "--kill-child",
        sys.executable,
        str(Path(__file__).resolve()),
        "ltp-child",
        "--root",
        str(root),
        "--output",
        str(output),
        "--request",
        str(request_file),
    ]
    log_path = output_dir / ("results_ltp-" + request["id"] + ".log")
    code = run_streamed(command, log_path, request["timeout_s"], "ltp-" + request["id"])
    if code is None:
        raise ValueError(
            "LTP exceeded its total timeout_s budget of %ds for suites %s"
            % (request["timeout_s"], ",".join(request["suites"]))
        )
    report_path = output / "report.json"
    report = None
    if report_path.exists():
        report = json.loads(report_path.read_text())
        report["pullab"] = {
            "expected_tests": expected_tests,
            "requested_suites": request["suites"],
            "image_sha256": state["image_sha256"],
            "context": context,
        }
        write_json(output_dir / ("results_ltp-" + request["id"] + ".json"), report)
    if code != 0:
        raise ValueError("Kirk execution failed; see " + log_path.name)
    if not report_path.is_file():
        raise ValueError("Kirk did not produce a JSON result report")
    summary = validate_kirk_report(report, state["release"], expected_tests, expected_version=state["version"])
    print("LTP", request["id"], json.dumps(summary), flush=True)


def test(state_path):
    state = verify(state_path)
    output = Path(state_path).resolve().parent
    requests = json.loads(os.environ["PULL_LABS_TESTS_JSON"])
    if not requests:
        raise ValueError("No tests were requested")
    for request in requests:
        if request["type"] in ("boot", "baseline"):
            continue  # verify() above is the actual baseline test.
        if request["type"] != "ltp":
            raise ValueError("Unsupported test type: " + request["type"])
        execute_ltp(state_path, state, request, output)
    print("All requested tests completed against the verified kernel.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "test", "ltp-child"))
    parser.add_argument("--state")
    parser.add_argument("--root")
    parser.add_argument("--output")
    parser.add_argument("--request")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Kernel installation and LTP require root in the disposable test VM")
    try:
        if args.action == "install":
            install(args.state)
        elif args.action == "test":
            test(args.state)
        else:
            ltp_child(args.root, args.output, args.request)
    except Exception as error:
        if args.state:
            write_json(
                Path(args.state).resolve().parent / "results_error.json",
                {"stage": args.action, "status": "fail", "error": str(error)},
            )
        print("ERROR:", error, file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

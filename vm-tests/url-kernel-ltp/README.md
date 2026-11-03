# LTP against the supplied kernel

This payload shares the two native boot stages with `url-kernel-boot`. After
verifying the requested kernel, it extracts the job's `ROOTFS_URL` tar archive
and runs the rootfs's Kirk and LTP in a chroot with private mount/PID namespaces.
The rootfs must contain `/opt/kirk/kirk` and `/opt/ltp/runtest/<suite>`.

The translator preserves test dependencies, suite names (`tst_cmdfiles`, comma
separated), workers (1–128 or `max`), skipfile and timeout. Unsupported test
types, parameters and pre/post commands fail explicitly. Each requested LTP
test produces a complete Kirk JSON report and a log. Every selected test must
appear in the report; failures, broken tests, warnings, empty reports, and
all-skipped runs fail even if Kirk exits zero. `conf`/`skip` are accepted when
at least one selected test passes. The report must identify the booted kernel.

A test's `timeout_s` is its total budget across all requested suites; it also
bounds Kirk's per-suite timeout. Kirk output is mirrored to the VM console
stream, with a heartbeat every five minutes, so long quiet suites are not
mistaken for hangs. When a job requests `baseline`/`boot` as well as LTP, one
VM runs both and reports a separate `boot` result from the boot evidence.

Named skipfiles must exist in the rootfs or have an explicit deployment URL.
For the current KernelCI rootfs, configure the pinned LKFT skipfile, for example:

```json
{
  "kernelci": {
    "environment": "staging",
    "skipfiles": {
      "skipfile-lkft.yaml": "https://raw.githubusercontent.com/Linaro/test-definitions/a6247b6442319af519b8760421a50cb504351eec/automated/linux/ltp/skipfile-lkft.yaml"
    }
  }
}
```

LKFT YAML selectors use the job's platform, kernel branch and configured
environment. Test names become anchored regexes for Kirk. Missing skipfiles
fail rather than silently changing the test selection. An explicit empty name
or `/dev/null` selects no skips. The final reports include the selected tests,
suites, context and image hash for independent AWS-side validation.

Run only on disposable VMs. LTP executes as root and exercises the real kernel.

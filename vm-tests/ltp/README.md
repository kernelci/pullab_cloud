# ltp

A vm-test that runs LTP (Linux Test Project) command files from a
KernelCI LTP rootfs inside a chroot on the VM.

Used by `pull_labs_translate.translate_job` for PULL_LABS jobs whose
`tests[].type` is `ltp` (e.g. the `ltp-smoke-aws-ec2` job in
kernelci-pipeline). The rootfs referenced by `ROOTFS_URL` must carry an
LTP installation in `/opt/ltp`, as the KernelCI Debian `*-ltp` images do.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `ROOTFS_URL` | yes | URL to the LTP rootfs tarball (e.g. `full.rootfs.tar.xz`) |
| `PULL_LABS_TESTS_JSON` | no | JSON list of job tests; `parameters` may carry `tst_cmdfiles=<a,b,...>` (default: `smoketest`) |
| `KERNEL_URL` | no | URL to the kernel image, only used with `BOOT_HOOK` |
| `MODULES_URL` | with `BOOT_HOOK` | URL to the modules tarball |
| `BOOT_HOOK` | no | Path to an executable that stages the kernel and reboots/kexecs |
| `ARCH` | no | Target architecture string (default: `uname -m`) |
| `KERNELCI_NODE_ID` | no | Job node ID from kernelci-api (recorded in logs) |

## Output

- `results_ltp-<cmdfile>.log` — LTP result log (one `PASS`/`FAIL`/`CONF`
  line per test), uploaded to S3 by `test-vm-client.sh`.
- `results_ltp-<cmdfile>-output.log` — full LTP console output.
- `results_ltp-<cmdfile>-failed.log` — failing command lines, if any.
- `benchmark-ltp-<kernel_version>.csv` — one `ltp.<cmdfile>` bool row per
  command file plus `passed`/`failed`/`skipped` counts, in the schema
  consumed by `BenchmarkAnalyzer`.

The script exits non-zero when any command file reports failures, which
marks the VM instance (and hence the job node) as failed.

## Limitations

- Without a `BOOT_HOOK`, LTP runs against the currently booted AMI
  kernel, not the kernel built by KernelCI (same behaviour as
  `url-kernel-boot`).
- The `skipfile`, `workers` and `skip_install` parameters used by the
  LAVA/LKFT runner are logged and ignored.
- Per-subtest results are only available in the uploaded logs; the KCIDB
  side currently records one row per VM instance.

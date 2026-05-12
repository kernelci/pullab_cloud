# url-kernel-boot

A vm-test that downloads a kernel image, modules tarball, and optional
rootfs from URLs provided in environment variables, then runs a
benchmark against the resulting environment.

Used by `pull_labs_translate.translate_job` to consume PULL_LABS
`artifacts.kernel` / `artifacts.modules` / `artifacts.rootfs` URLs from a
KernelCI job definition.

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `KERNEL_URL` | yes | URL to the kernel image (e.g. bzImage / vmlinuz) |
| `MODULES_URL` | yes | URL to the modules tarball (tar.gz) |
| `ROOTFS_URL` | no | Optional URL to a rootfs / ramdisk image |
| `ARCH` | no | Target architecture string (default: `uname -m`) |
| `KERNELCI_NODE_ID` | no | Job node ID from kernelci-api (recorded in logs) |
| `PULL_LABS_TESTS` | no | Comma-separated `id:type` list from the job definition |
| `BOOT_HOOK` | no | Path to an executable that stages the kernel and reboots/kexecs |

## Output

A CSV at `benchmark-tip-<kernel_version>.csv` matching the
`metric,unit,value,more_is_better,kernel_version,instance_id,instance_type,arch`
schema consumed by `BenchmarkAnalyzer`.

The scaffold emits a single `pullab.boot_check` row; replace or extend
the trailing CSV-write block with actual benchmark output (UnixBench,
LTP, etc.) to produce real results.

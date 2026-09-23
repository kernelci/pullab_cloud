# kvm-unit-tests

Runs the upstream [kvm-unit-tests](https://gitlab.com/kvm-unit-tests/kvm-unit-tests)
suite against the guest's KVM hypervisor. Intended to run on an EC2 instance
with **nested virtualization enabled** (c8i/c7i and related Intel families,
handled automatically by `_supports_nested_virtualization()` in `launch_vm.py`).

## What it does

The test runs in two stages (two `run-*.sh` scripts, with a reboot between them
handled by the pipeline client):

**run-01 -- install the target kernel.** Records the running kernel, then
installs the kernel under test from the pipeline's shared `kernel-rpms` area
(reusing the flow from `unixbench-kernel-regression`) and makes it the default
boot target; the client then reboots into it. If no kernel RPMs are provided,
the current kernel is kept.

**run-02 -- run kvm-unit-tests.** After the reboot:

1. Confirms the running kernel is the one run-01 installed (when applicable).
2. Installs the build toolchain (`git`, `gcc`, `make`, `binutils`) and builds
   **QEMU from source** into `/opt/qemu` (`x86_64-softmmu`, `--enable-kvm`) via
   `build-qemu.sh`. Amazon Linux 2023 ships no qemu system emulator in its core
   repos (only `qemu-img`), and its EPEL9-derived SPAL repo does not carry it
   either, so a source build is required. `install_qemu()` exports `QEMU` so
   `run_tests.sh` uses the built binary.
3. Verifies `/dev/kvm` is present in the guest -- a built-in regression check on
   the EC2 nested-virtualization enablement itself.
4. Clones and builds kvm-unit-tests (pinned to a fixed revision by default via
   `KVMUT_REF`; set it empty to track upstream tip).
5. Runs a **curated stable subset** by default (see `KVMUT_TESTS`) with
   `ACCEL=kvm ./run_tests.sh -v`. Set `KVMUT_TESTS=""` for the full suite.
6. Writes `results_kvm-unit-tests.txt` (human summary) and
   `results_kvm-unit-tests.csv` (`test_name,status,kernel_version,arch`) for
   KCIDB ingestion. The parser strips ANSI colour codes from run_tests.sh output
   before counting. Both use the `results_` prefix so `test-vm-client.sh`
   uploads them; the pass/fail verdict itself is conveyed by the script's exit
   code, which the client records as `SUCCESS`/`FAILED` in `result.txt`.

This is a **functional** test (pass/fail per sub-test), not a benchmark, so it
produces no `benchmark-*.csv` and is not part of the performance-regression
analysis.

## Why

kvm-unit-tests is the standard upstream suite for exercising a running kernel's
KVM implementation. Running it continuously in nested-virtualization cloud
guests gives a per-kernel regression signal for the virtualization stack, and
additional test groups can be layered in later via `KVMUT_GROUP`.

## Validated on c8i (2026-07-10)

Run on a live `c8i.4xlarge` with nested virt + source-built QEMU 9.2.0:

- **Full suite:** 54 PASS / 10 FAIL / 23 SKIP. The 10 failures are a coherent
  nested-virt-sensitive class: timeouts (`xapic`, `access`, `vmx_apicv_test`,
  `vmx_posted_intr_test`, `vmx_pf_exception_test`), MSR/PMU emulation gaps
  (`msr`, `msr64`, `pmu`), and nested-VMX instability (`vmx` SIGSEGV, `la57`).
- **Curated subset (default):** 6 PASS / 0 FAIL / 0 SKIP -- clean and stable,
  suitable as a hard gate.

## Two-tier model

- **Default = stable gate.** `KVMUT_TESTS` runs the proven-stable subset and
  `KVMUT_OBSERVE_ONLY=false` gates on it. A regression that breaks one of these
  tests fails the pipeline.
- **Full suite = tracking.** Set `KVMUT_TESTS=""`. `KVMUT_IGNORE_FAILURES` is
  pre-seeded with the 10 nested-virt-sensitive tests above so a full run still
  gates on the stable core; set `KVMUT_OBSERVE_ONLY=true` to only report.

## Configuration (environment overrides)

| Variable | Default | Purpose |
|---|---|---|
| `KVMUT_TESTS` | `debug intel_iommu lam vmx_init_signal_test vmx_sipi_signal_test hyperv_clock` | Space-separated test names to run. Empty = full suite. |
| `KVMUT_OBSERVE_ONLY` | `false` | Report counts but never fail. Set `true` for full-suite tracking runs. |
| `KVMUT_IGNORE_FAILURES` | *(10 nested-virt-sensitive tests)* | Test names excluded from the pass/fail decision. |
| `KVMUT_REF` | *(pinned commit `1da1819e`)* | kvm-unit-tests revision for reproducibility. Empty = upstream tip. |
| `KVMUT_GROUP` | *(empty)* | Run only a specific `run_tests.sh -g` group. |
| `QEMU_VERSION` | `9.2.0` | QEMU source version built by `build-qemu.sh`. |

## Requirements

- x86_64 instance from a nested-virt-capable family (e.g. `c8i.4xlarge`),
  32 GB+ RAM and ~40 GB disk (QEMU builds in `/tmp`, needs ~3 GB free).
- Kernel RPMs for the kernel under test, supplied by the pipeline
  (`external_requirements.json` sets `kernel-rpms/binary: true`). Without them
  run-01 keeps the current kernel.

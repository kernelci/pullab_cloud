# Shared kernel-management helpers across vm-tests

## Approach (implemented)

The kernel install/upgrade helpers live once in:

    vm-tests/lib/kernel_helpers.sh

Each kernel test includes it with a **symlink** in its own directory:

    vm-tests/<test>/kernel_helpers.sh -> ../lib/kernel_helpers.sh

and its `common_lib.sh` sources it after setting `SOURCE_DIR`:

    source "${SOURCE_DIR}/kernel_helpers.sh"

Why a symlink works with zero pipeline changes: `upload_test_payload()` builds
the payload with `Path(test_dir).rglob("*")` + `zf.write(...)`, which follows
the symlink and stores the **target's content** as a real file named
`kernel_helpers.sh`. On the VM the payload is extracted flat, so the test dir
gets a normal `kernel_helpers.sh` next to the `run*.sh` scripts.

Fix once, benefit everywhere: the underscore/dash RPM-version handling, the
FIPS-disable-before-reboot logic, and the `--allowerasing` cross-series install
live only in the shared lib.

## Status — migration complete

All kernel tests now source the shared lib and keep only their test-specific
functions in `common_lib.sh`:

- [x] `example-kernel-reboot-test` — no test-specific functions; just sources
      the shared lib.
- [x] `simple-source-reboot` — source-RPM build helpers
      (`install_source_kernel_rpm`, `build_kernel_rpm_src`,
      `get_first_source_kernel_rpm_from_dir`, `install_and_build_kernel`) local.
- [x] `unixbench-kernel-regression` — UnixBench helpers (`prepare_unixbench`,
      `run_unixbench`, `summarize_unixbench_log`) local.

`simple-unixbench` and other non-kernel tests do not install kernels and do not
use the shared lib.

## Adding a new kernel test

1. `cd vm-tests/<test> && ln -s ../lib/kernel_helpers.sh kernel_helpers.sh`
2. In `common_lib.sh`, `source "${SOURCE_DIR}/kernel_helpers.sh"` and add only
   test-specific functions.
3. Verify: `bash -n common_lib.sh` and a source-order smoke test with
   `SOURCE_DIR` set.

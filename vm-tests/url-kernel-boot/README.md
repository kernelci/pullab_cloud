# Native KernelCI boot test

This test installs the supplied kernel and its modules on a disposable Amazon
Linux 2023 VM. Stage 1 generates an initramfs with dracut, adds a grubby entry
and schedules it for exactly one boot (`grub2-reboot`), and lets the VM client
reboot through SSM exit code 194. A kernel that panics therefore falls back to
the AMI kernel and fails verification instead of looping. Stage 2
requires a new boot ID, the release and build version embedded in the supplied
image, and its SHA256 marker on the kernel command line. AMI fallback fails.

Supported images are x86_64 bzImage and arm64 Image or Image.gz. Modules must
be a tar archive containing exactly the matching lib/modules release tree.
The AMI userspace and SSM agent remain available for management; the supplied
kernel must support the VM's root filesystem, storage, networking, EFI, and
initramfs. Missing drivers or an incompatible image are test failures.

Required environment: `KERNEL_URL`, `MODULES_URL`, `ARCH`, and
`PULL_LABS_TESTS_JSON`. Optional `KERNEL_SHA256` and `MODULES_SHA256` validate
artifact downloads. The translator constructs these values.

`results_boot.json` records the requested artifact, image hash, release/build,
boot IDs and command line. `results_error.json` records installation or
verification failures. The VM client uploads diagnostic results on failure as
well as success. The AWS runner independently validates the boot evidence;
`SUCCESS` without evidence cannot pass. No synthetic benchmark CSV is emitted.

Use `url-kernel-ltp` for jobs requesting LTP. These stages require root and
modify the bootloader: run them only in disposable test VMs.

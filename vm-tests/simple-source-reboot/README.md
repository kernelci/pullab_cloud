# Simple Source Reboot Test

Tests kernel compilation from source RPM and verifies successful boot into the newly built kernel.

## Test Flow

1. **run-01-install_src_kernel.sh**: Records current kernel, downloads source RPM from S3, builds with `rpmbuild`, installs, reboots
2. **run-02-check_kernel.sh**: Verifies kernel version changed after reboot

## Requirements

- Kernel source RPM uploaded to the external storage bucket via `kernel-ci-cloud-runner aws setup upload-rpms`
- Build tools installed automatically from `dependencies.txt`

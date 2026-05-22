# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2026 KernelCI project
# Author: Denys Fedoryshchenko <nuclearcat@gmail.com>

"""Unit tests for core.log_scrub.

The scrubber runs at the upload boundary in launch_vm.capture_console_output,
just before put_object on a public-read bucket. These tests pin down two
things:

  * Known credential shapes are redacted and counted.
  * Innocuous boot-console content (kernel banners, PCI IDs, MACs) is left
    alone — false positives would mangle the very output that motivates
    publishing the log.
"""

from kernel_ci_cloud_labs.core.log_scrub import REDACTION, scrub_bytes, scrub_text


# ---- helpers ----------------------------------------------------------------


def _marker(kind: str) -> str:
    return REDACTION.format(kind=kind)


# ---- positive cases (must be redacted) --------------------------------------


def test_jwt_is_redacted():
    sample = (
        "Authorization line: "
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36"
    )
    out, counts = scrub_text(sample)
    assert counts == {"jwt": 1}
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert _marker("jwt") in out


def test_bearer_token_is_redacted_keyword_kept():
    sample = "GET / HTTP/1.1\\r\\nAuthorization: Bearer abc.DEF-123_xyz"
    out, counts = scrub_text(sample)
    assert counts.get("bearer-token") == 1
    assert "Bearer" in out  # the literal keyword is preserved
    assert "abc.DEF-123_xyz" not in out


def test_aws_access_key_id_is_redacted():
    sample = "creds dumped: AKIAIOSFODNN7EXAMPLE and ASIAYJRWVUHQQ4EXAMPL"
    out, counts = scrub_text(sample)
    assert counts.get("aws-access-key-id") == 2
    assert "AKIA" not in out
    assert "ASIA" not in out


def test_github_pat_is_redacted():
    pat = "ghp_" + "A" * 36
    out, counts = scrub_text(f"token={pat} ok")
    assert counts.get("github-token") == 1
    assert pat not in out


def test_pem_private_key_block_is_redacted():
    sample = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAxxxxxxxx\nyyyyyyyyzzzz\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    out, counts = scrub_text(sample)
    assert counts.get("pem-private-key") == 1
    assert "MIIEowIBAAKCAQEAxxxxxxxx" not in out
    assert "before" in out and "after" in out


def test_ssh_public_key_is_redacted():
    key = "ssh-ed25519 AAAA" + "B" * 60 + " user@host"
    out, counts = scrub_text(key)
    assert counts.get("ssh-public-key") == 1
    assert "AAAAB" not in out


def test_credential_kv_password_export():
    # Mirrors what `set -x` would echo for the export loop in launch_vm.spawn_vm.
    sample = "+ export PASSWORD=hunter2-secret"
    out, counts = scrub_text(sample)
    assert counts.get("credential-kv") == 1
    assert "hunter2-secret" not in out
    # Key name is preserved for debuggability.
    assert "PASSWORD=" in out


def test_credential_kv_session_token_quoted():
    sample = 'config: session_token="abc-def-1234-XYZ"'
    out, counts = scrub_text(sample)
    assert counts.get("credential-kv") == 1
    assert "abc-def-1234-XYZ" not in out


def test_multiple_secrets_counted_separately():
    sample = (
        "AKIAIOSFODNN7EXAMPLE\n"
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signaturepart\n"
        "Bearer abcdefghij\n"
    )
    out, counts = scrub_text(sample)
    assert counts["aws-access-key-id"] == 1
    assert counts["jwt"] == 1
    assert counts["bearer-token"] == 1
    assert "AKIA" not in out and "eyJhbGc" not in out and "abcdefghij" not in out


# ---- negative cases (must NOT be touched) -----------------------------------


def test_kernel_boot_banner_is_untouched():
    """A canonical first second of dmesg should pass through verbatim."""
    sample = (
        "[    0.000000] Linux version 6.10.0-1-amd64 (build@kbuilder) "
        "(gcc-13 (Debian 13.2.0-23) 13.2.0, GNU ld (GNU Binutils for Debian) 2.42) "
        "#1 SMP PREEMPT_DYNAMIC Debian 6.10.6-1 (2024-08-19)\n"
        "[    0.000123] BIOS-provided physical RAM map:\n"
        "[    0.000456] BIOS-e820: [mem 0x0000000000000000-0x000000000009fbff] usable\n"
    )
    out, counts = scrub_text(sample)
    assert out == sample
    assert counts == {}


def test_pci_and_mac_addresses_are_untouched():
    sample = (
        "pci 0000:00:1f.2: reg 0x10: [io  0x0000-0x0007]\n"
        "eth0: link up, 1000Mbps, full-duplex, lpa 0x45e1\n"
        "eth0: hw_addr 00:11:22:33:44:55\n"
        "wlan0: 02:00:00:aa:bb:cc associated with ap\n"
    )
    out, counts = scrub_text(sample)
    assert out == sample
    assert counts == {}


def test_short_password_below_threshold_is_ignored():
    """The KV rule requires 6+ chars to limit false positives on noise."""
    sample = "password=x  # placeholder"
    out, counts = scrub_text(sample)
    assert out == sample
    assert counts == {}


def test_empty_input_short_circuits():
    out, counts = scrub_text("")
    assert out == ""
    assert counts == {}


# ---- bytes wrapper ----------------------------------------------------------


def test_scrub_bytes_roundtrip():
    raw = b"AKIAIOSFODNN7EXAMPLE on a line\n"
    out, counts = scrub_bytes(raw)
    assert counts == {"aws-access-key-id": 1}
    assert b"AKIA" not in out
    assert isinstance(out, bytes)


def test_scrub_bytes_tolerates_invalid_utf8():
    """Boot consoles occasionally carry framebuffer/UART noise; we don't skip."""
    raw = b"prefix \xff\xfe AKIAIOSFODNN7EXAMPLE \xc3\x28 suffix"
    out, counts = scrub_bytes(raw)
    assert counts.get("aws-access-key-id") == 1
    assert b"AKIA" not in out

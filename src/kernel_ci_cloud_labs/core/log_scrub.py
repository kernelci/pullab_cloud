"""Redact obvious secrets from text destined for a public S3 object.

The kernel boot console buffer captured by ``launch_vm.capture_console_output``
is uploaded to a public-read S3 bucket and the URL is published to KCIDB.
That buffer is almost always pure kernel boot output (uninteresting from a
secrets standpoint), but two things can leak credentials into it:

  * The EC2 user-data script in ``launch_vm.spawn_vm`` runs with ``set -x``
    under ``logger -t user-data -s 2>/dev/console``, so anything it echoes
    lands in the console buffer. We don't put secrets there today, but a
    future edit easily could.

  * Userland on the test VM is free to write to ``/dev/kmsg`` or
    ``/dev/console``; a misbehaving test that echoes a token to either
    surface would publish it.

Rather than rely on every future contributor remembering this, scrub the
buffer once at the upload boundary. Patterns are deliberately conservative:
they match well-known credential shapes and the obvious ``KEY=VALUE``
spellings, not anything that "looks" like a secret. False negatives are
expected; false positives that mangle legitimate boot output (PCI IDs,
MAC addresses, kernel version strings) are not.

The function returns the scrubbed text and a per-rule counter so the caller
can log how much was redacted without echoing the originals.
"""

__authors__ = ["Denys Fedoryshchenko <nuclearcat@gmail.com>"]
__copyright__ = "Copyright (c) 2026 KernelCI project. All Rights Reserved."
# SPDX-License-Identifier: Apache-2.0


import re
from typing import Dict, Tuple

# Replacement marker that survives grep — ``REDACTED`` alone is too common
# in real output, the bracketed form is not.
REDACTION = "[REDACTED:{kind}]"


def _redactor(kind: str):
    """Build a re.sub replacement that fills in the rule name on substitution."""
    return lambda _m: REDACTION.format(kind=kind)


# Each rule: (name, compiled_regex). Order matters only when patterns can
# overlap — generally bearer/JWT > AWS keys > generic KEY=VALUE so the
# more-specific match wins.
_RULES = [
    # PEM-armored private keys. DOTALL so the body matches across newlines.
    (
        "pem-private-key",
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    # OpenSSH authorized_keys form (single line).
    (
        "ssh-public-key",
        re.compile(r"\bssh-(?:rsa|ed25519|dss|ecdsa-[a-z0-9-]+)\s+[A-Za-z0-9+/=]{40,}"),
    ),
    # JWTs: three base64url segments separated by dots; second segment starts
    # with "eyJ" (the JSON header begins with '{').
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    ),
    # GitHub token families. Length suffixes are the published lower bounds.
    ("github-token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    # AWS access key IDs: AKIA = long-lived user, ASIA = STS temporary.
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    # "Authorization: Bearer ..." / "Bearer eyJ..." style. Group 1 captures
    # the "Bearer " keyword + whitespace so the substitution can put it back
    # — a scrubbed log that still shows the keyword is easier to triage.
    (
        "bearer-token",
        re.compile(r"\b(Bearer\s+)([A-Za-z0-9._\-+/=]{8,})", re.IGNORECASE),
    ),
    # Generic KEY=VALUE / key: value with a secret-shaped name. The value
    # must look credential-ish (>=8 non-space chars, contains some entropy);
    # we don't try to be clever, just non-empty up to whitespace.
    (
        "credential-kv",
        re.compile(
            r"""(?ix)
            \b
            (?: password | passwd | secret | api[_-]?key | access[_-]?key
              | private[_-]?key | session[_-]?token | auth[_-]?token
              | client[_-]?secret )
            \s* [:=] \s*
            ['"]?
            ([^\s'"]{6,})
            ['"]?
            """
        ),
    ),
]


def scrub_text(text: str) -> Tuple[str, Dict[str, int]]:
    """Apply every redaction rule to ``text``.

    Returns the scrubbed text and a ``{rule_name: hits}`` dict (rules with
    zero hits are omitted). The function is pure: same input → same output.

    Empty input returns ``("", {})`` without touching the regex engine.
    """
    if not text:
        return text, {}

    counts: Dict[str, int] = {}
    out = text
    for name, rx in _RULES:
        # Some rules keep part of the match visible for grep-ability:
        #   * credential-kv: keep "KEY=" / "KEY:", redact the value (group 1).
        #   * bearer-token:  keep "Bearer " (group 1), redact the token
        #     (group 2).
        # Everything else replaces the whole match.
        if name == "credential-kv":
            def repl(m, _kind=name):
                return m.group(0).replace(m.group(1), REDACTION.format(kind=_kind))

            out, n = rx.subn(repl, out)
        elif name == "bearer-token":
            def repl_bearer(m, _kind=name):
                return m.group(1) + REDACTION.format(kind=_kind)

            out, n = rx.subn(repl_bearer, out)
        else:
            out, n = rx.subn(_redactor(name), out)
        if n:
            counts[name] = n
    return out, counts


def scrub_bytes(data: bytes, encoding: str = "utf-8") -> Tuple[bytes, Dict[str, int]]:
    """Convenience wrapper for the upload path, which holds the buffer as bytes.

    Decodes with ``errors="replace"`` because boot consoles sometimes carry
    stray non-UTF-8 bytes (early-boot framebuffer noise, partial UART frames)
    and we'd rather scrub a slightly-mangled copy than skip scrubbing.
    """
    text = data.decode(encoding, errors="replace")
    scrubbed, counts = scrub_text(text)
    return scrubbed.encode(encoding, errors="replace"), counts

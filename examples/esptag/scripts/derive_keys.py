#!/usr/bin/env python3
"""Derive the tag's root secrets from its master seed, and emit provisioning files.

Reads the 32-byte master seed produced by gen_seed.py and deterministically
derives the two root secrets the firmware needs:

  - d_0  : 28-byte initial private scalar, a valid secp224r1 scalar in [1, n-1]
  - sk_0 : 32-byte initial symmetric key

Both come from the one seed via HKDF-SHA256 (RFC 5869): the seed is HKDF-Extract-ed
to a PRK, then HKDF-Expand-ed under distinct `info` labels -- so the same seed
always yields the same tag, and the seed is the *only* secret you must protect.
`sk_0` is a single Expand; `d_0` uses rejection sampling over successive Expand
blocks (a 1-byte attempt counter in the info) so the scalar is uniform in [1, n-1]
without modulo bias.

It writes two files, both derived from the same seed:

  - seed.csv (--nvs): an NVS partition CSV consumed by ESP-IDF's partition
    generator (`nvs_create_partition_image` in main/CMakeLists.txt) and flashed
    to the `nvs` partition by `idf.py flash`. This is the build input. Values are
    inline hex (encoding `hex2bin`), so the CSV is self-contained.
  - seed.keys (--keys): a plaintext `d_0=<hex>` / `sk_0=<hex>` key file (KEY=hex
    lines) that fetch_reports.py reads to reconstruct the tag's private keys.

The provisioned secrets are only the two root values; the SK ratchet counter is
not part of them. The firmware keeps the counter in a separate writable NVS
namespace (`esptag_st`) and, with CONFIG_ESPTAG_PERSIST_COUNTER (default on),
resumes from it across reboots. Re-flashing the NVS image rewrites the whole `nvs`
partition, so it wipes `esptag_st` and resets the counter to 0 (a provisioning
event); a plain reboot does not.

WARNING: every output here carries the tag's root secret in plaintext. Treat the
seed, the CSV, and any key file as sensitive and pair on-device storage with
flash/NVS encryption for any real deployment.

DEPLOYMENT NOTE (at-rest / at-runtime secret protection): the root secret
(d_0, sk_0) lives in the `nvs` partition in plaintext and is also resident in
RAM for the device's lifetime (d_0 is needed on every rotation). On a captured
tag it is therefore recoverable by a flash dump (esptool read_flash) or by
halting the CPU over USB-JTAG. This is NOT fixed here: the target ESP32-S3R8
module has no secure element, and the security review accepted this as a known
limitation. A hardened build would need Flash Encryption (release) + Secure Boot
v2 + NVS encryption for this namespace + USB-JTAG disabled via eFuse -- which
binds the secret to one chip but, absent a secure element, still cannot stop a
determined invasive (decap/probe) attack. See the security pass notes.
"""

import argparse
import hashlib
import hmac
import os
import sys
from pathlib import Path

# scripts/ is on sys.path[0] when this file is run directly, so the sibling
# constants module imports without any path juggling.
from esptag_const import D_LEN, KEY_D0, KEY_SK0, NAMESPACE, P224_N, SEED_LEN, SK_LEN

# Repo root (one level up from scripts/). Seed files default to living here so
# the whole toolchain -- gen_seed.py, fetch_reports.py, and the build's
# `${PROJECT_DIR}/seed.csv` -- agrees on their location regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

# HKDF domain-separation labels for the seed -> (sk_0, d_0) derivation. Fixed and
# public; changing them changes every derived tag identity.
HKDF_SALT = b"esptag root keys v1"
INFO_SK0 = b"esptag sk_0"
INFO_D0 = b"esptag d_0"


def hkdf_extract(salt, ikm):
    """RFC 5869 HKDF-Extract: PRK = HMAC-SHA256(salt, IKM). Returns 32 bytes."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def hkdf_expand(prk, info, length):
    """RFC 5869 HKDF-Expand (HMAC-SHA256). Returns `length` bytes."""
    out = b""
    t = b""
    counter = 1
    while len(out) < length:
        t = hmac.new(prk, t + info + bytes([counter]), hashlib.sha256).digest()
        out += t
        counter += 1
    return out[:length]


def derive_roots(seed):
    """Derive (d_0, sk_0) from the 32-byte master seed.

    HKDF-Extract the seed once, then HKDF-Expand under distinct info labels.
    sk_0 is a single 32-byte expand; d_0 is rejection-sampled over 28-byte expand
    blocks (a 1-byte attempt counter appended to the info) to stay uniform in
    [1, n-1] without the modulo bias of reduce-mod-n.
    """
    prk = hkdf_extract(HKDF_SALT, seed)
    sk_0 = hkdf_expand(prk, INFO_SK0, SK_LEN)

    attempt = 0
    while True:
        block = hkdf_expand(prk, INFO_D0 + bytes([attempt]), D_LEN)
        candidate = int.from_bytes(block, "big")
        if 1 <= candidate < P224_N:
            d_0 = candidate.to_bytes(D_LEN, "big")
            break
        # Each draw lands in [1, n-1] with overwhelming probability (n is within
        # ~2^-112 of 2^224), so this loop effectively never iterates.
        attempt += 1
        if attempt > 255:
            sys.exit("d_0 rejection sampling exhausted (impossible; bad seed?)")

    return d_0, sk_0


def load_seed(path):
    """Read the master seed, accepting hex text (gen_seed.py default) or raw 32 B."""
    raw = open(path, "rb").read()
    text = raw.strip()
    # Hex form: 64 hex chars (optionally with surrounding whitespace/newline).
    if len(text) == SEED_LEN * 2:
        try:
            return bytes.fromhex(text.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    if len(raw) == SEED_LEN:
        return raw
    sys.exit(f"{path}: not a {SEED_LEN}-byte seed "
             f"(expected {SEED_LEN*2} hex chars or {SEED_LEN} raw bytes)")


def build_nvs_csv(d_0, sk_0):
    lines = [
        "key,type,encoding,value",
        f"{NAMESPACE},namespace,,",
        f"{KEY_D0},data,hex2bin,{d_0.hex()}",
        f"{KEY_SK0},data,hex2bin,{sk_0.hex()}",
    ]
    return "\n".join(lines) + "\n"


def build_keys_file(d_0, sk_0):
    return f"{KEY_D0}={d_0.hex()}\n{KEY_SK0}={sk_0.hex()}\n"


def write_output(path, content):
    """Write `content` to `path`, or stdout if `path` is '-'. 0600 for files."""
    if path == "-":
        sys.stdout.write(content)
        return
    # Restrictive permissions: this file holds the root secret.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    print(f"wrote {path}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "seed", nargs="?", default=str(REPO_ROOT / "seed.key"),
        help="master seed file from gen_seed.py (default: seed.key at the repo root)",
    )
    parser.add_argument(
        "--nvs", default=str(REPO_ROOT / "seed.csv"), metavar="PATH",
        help="NVS partition CSV output, the build input (default: seed.csv at "
             "the repo root; use - for stdout)",
    )
    parser.add_argument(
        "--keys", default=str(REPO_ROOT / "seed.keys"), metavar="PATH",
        help="plaintext d_0=/sk_0= key file for host tools like fetch_reports.py "
             "(default: seed.keys at the repo root; use - for stdout)",
    )
    args = parser.parse_args(argv)

    seed = load_seed(args.seed)
    d_0, sk_0 = derive_roots(seed)

    write_output(args.nvs, build_nvs_csv(d_0, sk_0))
    write_output(args.keys, build_keys_file(d_0, sk_0))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Generate the tag's 32-byte master seed.

This is the *root* secret of the whole design: a single 32-byte value from which
derive_keys.py deterministically derives both provisioned root secrets
(`d_0`, `sk_0`) -- and, through them, every rotating identity the tag broadcasts.
Keep the two stages separate: this script only produces the master seed; running
it again mints a brand-new tag identity. derive_keys.py turns the seed into the
NVS image and key files the build and host tools consume.

Two sources of seed bytes:

  - Default: the OS CSPRNG (`secrets.token_bytes`), i.e. true randomness. This is
    what a real device should use.

  - --passphrase / --passphrase-stdin: derive the seed deterministically from a
    user-supplied passphrase via HKDF-Extract (HMAC-SHA256 with a fixed
    application salt). Reproducible: the same passphrase always yields the same
    seed, hence the same tag. Handy for tests and for re-provisioning a known
    identity, but only as strong as the passphrase -- a low-entropy phrase makes
    the whole tag brute-forceable. Not for high-value deployments.

The seed is written as a single line of lowercase hex (64 chars + newline), the
same all-hex convention the rest of the tooling uses; derive_keys.py also accepts
a raw 32-byte binary file.

WARNING: the seed file is the tag's root secret in plaintext -- anyone holding it
can reconstruct every key the tag will ever broadcast and decrypt its location
reports. It is written with 0600 permissions; treat it as sensitive and pair
on-device storage with flash/NVS encryption for any real deployment (see the
deployment note in derive_keys.py).
"""

import argparse
import getpass
import hashlib
import hmac
import os
import sys
from pathlib import Path

# scripts/ is on sys.path[0] when this file is run directly, so the sibling
# constants module imports without any path juggling.
from esptag_const import SEED_LEN

# Repo root (one level up from scripts/). Seed files default to living here so
# the whole toolchain -- derive_keys.py, fetch_reports.py, and the build's
# `${PROJECT_DIR}/seed.csv` -- agrees on their location regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent

# Fixed application salt for the passphrase -> seed HKDF-Extract step. Domain-
# separates this use of a user passphrase from any other; it is not itself a
# secret. Override with --salt if you want an extra per-deployment tweak.
DEFAULT_SALT = b"esptag master seed v1"


def hkdf_extract(salt, ikm):
    """RFC 5869 HKDF-Extract: PRK = HMAC-SHA256(salt, IKM). Returns 32 bytes."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output", default=str(REPO_ROOT / "seed.key"),
        help="output seed path (default: seed.key at the repo root; "
             "use - for stdout)",
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument(
        "-p", "--passphrase", metavar="TEXT",
        help="derive the seed deterministically from this passphrase via "
             "HKDF-Extract (reproducible, INSECURE for low-entropy input)",
    )
    src.add_argument(
        "--passphrase-stdin", action="store_true",
        help="like --passphrase but read the passphrase from a prompt / stdin "
             "(keeps it out of the process argument list)",
    )
    parser.add_argument(
        "--salt", default=None, metavar="TEXT",
        help=f"override the HKDF-Extract salt (default: {DEFAULT_SALT.decode()!r}); "
             "only meaningful with a passphrase",
    )
    args = parser.parse_args(argv)

    if args.passphrase is not None or args.passphrase_stdin:
        if args.passphrase_stdin:
            passphrase = getpass.getpass("Passphrase: ")
        else:
            passphrase = args.passphrase
        if not passphrase:
            sys.exit("empty passphrase")
        salt = args.salt.encode() if args.salt is not None else DEFAULT_SALT
        seed = hkdf_extract(salt, passphrase.encode())
    else:
        if args.salt is not None:
            sys.exit("--salt only applies to passphrase-derived seeds")
        seed = os.urandom(SEED_LEN)

    line = seed.hex() + "\n"
    if args.output == "-":
        sys.stdout.write(line)
    else:
        # Restrictive permissions: this file holds the tag's root secret.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(line)
        print(f"wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()

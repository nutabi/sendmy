#!/usr/bin/env python3
"""Generate the 32-byte Unilink ID (UID), the sendmy carrier's root secret.

The UID is the single pre-shared secret of the whole design: the sender and the
receiver both hold it, and it is the HKDF pseudorandom key from which every
carrier is derived (see components/sendmy). It is used directly -- there is no
further key-derivation stage -- so it must have full 256-bit entropy.

The UID is written to uid.hex at the repo root by default. From there,
gen_nvs_csv.py bakes it into the NVS image flashed to the device (namespace
`sendmy`, key `uid`), and the host-side receiver (fetch_reports.py) reads the
same uid.hex to derive the carriers it queries. Re-running this script mints a
brand-new, unrelated UID, invalidating any previously provisioned device.

Two sources of UID bytes:

  - Default: the OS CSPRNG (`os.urandom`), i.e. true randomness. This is what a
    real device should use.

  - --passphrase / --passphrase-stdin: derive the UID deterministically from a
    user-supplied passphrase via HKDF-Extract (HMAC-SHA256 with a fixed
    application salt). Reproducible: the same passphrase always yields the same
    UID, hence the same identity. Handy for tests and for re-provisioning a known
    UID, but only as strong as the passphrase -- a low-entropy phrase makes the
    channel brute-forceable. Not for high-value deployments.

The UID is written as a single line of lowercase hex (64 chars + newline), the
all-hex convention the rest of the tooling (gen_nvs_csv.py) consumes.

WARNING: uid.hex is the shared secret in plaintext -- anyone holding it can
derive every carrier and read the channel. It is written with 0600 permissions;
treat it as sensitive, keep it out of version control, and pair on-device storage
with flash/NVS encryption for any real deployment.
"""

import argparse
import getpass
import hashlib
import hmac
import os
import sys
from pathlib import Path

SEED_LEN = 32
REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SALT = b"espsend super secret seed"

def hkdf_extract(salt, ikm):
    """RFC 5869 HKDF-Extract: PRK = HMAC-SHA256(salt, IKM). Returns 32 bytes."""
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-o", "--output", default=str(REPO_ROOT / "uid.hex"),
        help="output seed path (default: uid.hex at the repo root; "
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

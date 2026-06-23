#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from findmy.keys import HasHashedPublicKey
from findmy.reports import AppleAccount

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
UID_FILE = PROJECT_ROOT / "uid.hex"
ACCOUNT_FILE = SCRIPT_DIR / "account.json"
ANISETTE_LIBS = SCRIPT_DIR / "anisette-libs.bin"

INFO = b"smv1"  # 0x73 0x6D 0x76 0x31, the sendmy HKDF info prefix
CARRIER_LEN = 28

# secp224r1 (NIST P-224) group order n. The carrier scalar must lie in [1, n-1].
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFF16A2E0B8F03E13DD29455C5C2A3D


class Carrier(HasHashedPublicKey):
    def __init__(self, carrier: bytes, payload: int) -> None:
        self._hash = hashlib.sha256(carrier).digest()
        self.payload = payload

    @property
    def hashed_adv_key_bytes(self) -> bytes:
        return self._hash


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869) with HMAC-SHA-256."""
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def build_carrier(uid: bytes, mid: int, payload: int) -> bytes:
    """Derive the 28-byte carrier the firmware advertises for (uid, mid, payload).

    Mirrors sendmy_carrier's compute_carrier: build the HKDF info
    ("smv1" || mid_be32 || payload || attempt), expand it to a 28-byte block,
    interpret that as a big-endian scalar d, and accept the first attempt whose
    d lands in [1, n-1] (rejection sampling; in practice attempt is always 0).
    The carrier is the big-endian x-coordinate of d*G, so it is always a valid
    secp224r1 point and Find My never drops it.
    """
    for attempt in range(256):
        info = INFO + mid.to_bytes(4, "big") + bytes([payload, attempt])
        d = int.from_bytes(hkdf_expand(uid, info, CARRIER_LEN), "big")
        if 1 <= d < N:
            x = ec.derive_private_key(d, ec.SECP224R1()).public_key().public_numbers().x
            return x.to_bytes(CARRIER_LEN, "big")
    raise RuntimeError(
        f"no valid P-224 scalar after 256 attempts (mid={mid}, payload={payload})"
    )


def read_uid() -> bytes:
    if not UID_FILE.exists():
        sys.exit(f"error: {UID_FILE} not found")
    text = UID_FILE.read_text().strip()
    if len(text) != 64:
        sys.exit(f"error: uid.hex must be 64 hex chars (32 bytes), got {len(text)}")
    return bytes.fromhex(text)


def load_account() -> AppleAccount:
    if not ACCOUNT_FILE.exists():
        sys.exit(f"error: {ACCOUNT_FILE} not found (provision a session first)")
    libs = str(ANISETTE_LIBS) if ANISETTE_LIBS.exists() else None
    return AppleAccount.from_json(ACCOUNT_FILE, anisette_libs_path=libs)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover one espsend payload octet for a message_id.",
    )
    parser.add_argument(
        "message_id", nargs="?", type=int, default=0,
        help="message_id to recover (default: 0)",
    )
    args = parser.parse_args()
    if args.message_id < 0 or args.message_id > 0xFFFFFFFF:
        sys.exit("error: message_id must fit in a 32-bit unsigned integer")

    uid = read_uid()

    # 256 candidates: one full carrier derived per possible payload octet.
    candidates = [Carrier(build_carrier(uid, args.message_id, p), p) for p in range(256)]

    account = load_account()
    results = account.fetch_location(candidates)
    if not isinstance(results, dict):
        results = {candidates[0]: results}

    present = [c.payload for c in candidates if results.get(c) is not None]

    if not present:
        print(f"mid={args.message_id}: no carrier present (message lost)", file=sys.stderr)
        sys.exit(1)
    if len(present) > 1:
        hexes = ", ".join(f"0x{p:02x}" for p in present)
        print(f"mid={args.message_id}: warning, {len(present)} carriers present "
              f"({hexes}); reporting first", file=sys.stderr)

    payload = present[0]
    glyph = chr(payload) if 0x20 <= payload < 0x7F else "."
    print(f"mid={args.message_id} payload=0x{payload:02x} ({payload}) '{glyph}'",
          file=sys.stderr)
    print(f"{payload:02x}")  # stdout: the octet, for piping/concatenation


if __name__ == "__main__":
    main()

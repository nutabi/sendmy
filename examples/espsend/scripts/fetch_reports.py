#!/usr/bin/env python3
"""Recover one espsend payload octet from Apple's Find My network.

This is the receiver. For a given message_id it derives all 256 candidate
carriers from the shared Unilink ID (UID), asks Apple whether a Find My report
exists for each, and prints the payload octet of the one that does. The
existence of a carrier is the signal; its identity (the appended payload byte)
is the recovered value.

Carrier derivation (mirrors components/sendmy):

    carrier_id = HKDF-Expand(PRK=uid, info="smv1" || mid_be32, L=27)
    carrier    = carrier_id || payload          # payload in 0x00..0xFF
    lookup     = SHA-256(carrier)               # Find My report key

The UID is read from uid.hex at the project root (64 hex chars = 32 bytes).
Authentication reuses the provisioned local session in scripts/account.json with
the bundled anisette libraries in scripts/anisette-libs.bin -- no login needed.

Usage:
    fetch_reports.py [message_id]      # message_id defaults to 0

Requires: findmy (FindMy.py).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import sys
from pathlib import Path

from findmy.keys import HasHashedPublicKey
from findmy.reports import AppleAccount

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
UID_FILE = PROJECT_ROOT / "uid.hex"
ACCOUNT_FILE = SCRIPT_DIR / "account.json"
ANISETTE_LIBS = SCRIPT_DIR / "anisette-libs.bin"

INFO = b"smv1"  # 0x73 0x6D 0x76 0x31, the sendmy HKDF info prefix
CID_LEN = 27


class Carrier(HasHashedPublicKey):
    """A candidate carrier, queryable by SHA-256 of its 28 bytes."""

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


def carrier_id(uid: bytes, mid: int) -> bytes:
    info = INFO + mid.to_bytes(4, "big")
    return hkdf_expand(uid, info, CID_LEN)


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
    cid = carrier_id(uid, args.message_id)

    # 256 candidates: carrier_id with every possible payload octet appended.
    candidates = [Carrier(cid + bytes([p]), p) for p in range(256)]

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

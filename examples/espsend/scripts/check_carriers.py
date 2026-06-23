#!/usr/bin/env python3
"""Report which espsend carriers land on a valid P-224 point.

A Find My finder can only file a location report for a carrier that is a valid
secp224r1 x-coordinate: it reconstructs the point and encrypts (ECDH) to it.
Carriers that are not valid points are silently dropped -- the message is lost
with no error on the sender side. Because the carrier is a raw HMAC-SHA-256
truncation, only about half of them are valid points (see the "Planned
enhancement" note in components/sendmy_carrier/README.md).

This walks the same carriers the firmware sends (payload = 1 << (mid % 8)) and
flags which mids are actually deliverable, so a "not found" result from
fetch_reports.py can be told apart from a real bug.
"""

import hashlib
import hmac
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
UID_FILE = PROJECT_ROOT / "uid.hex"

INFO = b"smv1"  # 0x73 0x6D 0x76 0x31, the sendmy HKDF info prefix
CARRIER_LEN = 28
PAYLOAD_CYCLE = 8  # espsend sends payload = 1 << (mid % PAYLOAD_CYCLE)

# secp224r1 (NIST P-224) domain parameters
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF000000000000000000000001
A = (P - 3) % P
B = 0xB4050A850C04B3ABF54132565044B0B7D7BFD8BA270B39432355FFB4


def hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    """HKDF-Expand (RFC 5869) with HMAC-SHA-256. Matches fetch_reports.py."""
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def build_carrier(uid: bytes, mid: int, payload: int) -> bytes:
    info = INFO + mid.to_bytes(4, "big") + bytes([payload])
    return hkdf_expand(uid, info, CARRIER_LEN)


def is_valid_x(x: int) -> bool:
    """True iff some point (x, y) lies on secp224r1 (Euler's criterion)."""
    if x >= P:
        return False
    rhs = (pow(x, 3, P) + A * x + B) % P
    return rhs == 0 or pow(rhs, (P - 1) // 2, P) == 1


def read_uid() -> bytes:
    if not UID_FILE.exists():
        sys.exit(f"error: {UID_FILE} not found")
    text = UID_FILE.read_text().strip()
    if len(text) != 64:
        sys.exit(f"error: uid.hex must be 64 hex chars (32 bytes), got {len(text)}")
    return bytes.fromhex(text)


def main() -> None:
    last = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    uid = read_uid()

    valid = 0
    for mid in range(last + 1):
        payload = 1 << (mid % PAYLOAD_CYCLE)
        x = int.from_bytes(build_carrier(uid, mid, payload), "big")
        ok = is_valid_x(x)
        valid += ok
        verdict = "OK      (deliverable)" if ok else "INVALID (dropped)"
        print(f"mid={mid:<3} payload=0x{payload:02x}  P-224 {verdict}")

    print(f"\n{valid}/{last + 1} carriers deliverable (~50% expected)")


if __name__ == "__main__":
    main()

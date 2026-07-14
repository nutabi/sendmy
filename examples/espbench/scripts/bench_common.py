#!/usr/bin/env python3
"""Shared helpers for the espbench host tooling.

Everything the receiver needs to (a) reproduce what a run *should* have sent and
(b) recover what actually landed on the relay lives here, so run_matrix.py and
the manual fetch_reports.py CLI stay thin and agree byte-for-byte with each other
and with the firmware.

The carrier derivation mirrors sendmy_carrier exactly (see build_carrier), and
the payload generators mirror espbench.c exactly (see expected_sequence).
"""

from __future__ import annotations

import hashlib
import hmac
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from findmy.keys import HasHashedPublicKey, KeyPair
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

# Mode integers, matching CONFIG_ESPBENCH_MODE / the MODE_* defines in espbench.c.
MODE_STATIC = 0
MODE_INCREMENTAL = 1
MODE_RANDOM = 2

MODE_NAMES = {"static": MODE_STATIC, "incremental": MODE_INCREMENTAL, "random": MODE_RANDOM}


class Carrier(HasHashedPublicKey):
    """A single candidate carrier, tagged with the payload octet it encodes."""

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


def derive_scalar(uid: bytes, mid: int, payload: int) -> int:
    """Derive the secp224r1 private scalar d the firmware uses for (uid, mid, payload).

    Mirrors sendmy_carrier's compute_carrier: build the HKDF info
    ("smv1" || mid_be32 || payload || attempt), expand it to a 28-byte block,
    interpret that as a big-endian scalar d, and accept the first attempt whose
    d lands in [1, n-1] (rejection sampling; in practice attempt is always 0).
    """
    for attempt in range(256):
        info = INFO + mid.to_bytes(4, "big") + bytes([payload, attempt])
        d = int.from_bytes(hkdf_expand(uid, info, CARRIER_LEN), "big")
        if 1 <= d < N:
            return d
    raise RuntimeError(
        f"no valid P-224 scalar after 256 attempts (mid={mid}, payload={payload})"
    )


def build_carrier(uid: bytes, mid: int, payload: int) -> bytes:
    """Derive the 28-byte carrier the firmware advertises for (uid, mid, payload).

    The carrier is the big-endian x-coordinate of d*G (d from derive_scalar), so
    it is always a valid secp224r1 point and Find My never drops it.
    """
    d = derive_scalar(uid, mid, payload)
    x = ec.derive_private_key(d, ec.SECP224R1()).public_key().public_numbers().x
    return x.to_bytes(CARRIER_LEN, "big")


def keypair_for(uid: bytes, mid: int, payload: int) -> KeyPair:
    """Return the findmy KeyPair for (uid, mid, payload).

    We know the private scalar d (derive_scalar), so we can build the full
    KeyPair, not just the hashed public carrier. Its adv_key_bytes equal
    build_carrier's output and its hashed_adv_key_bytes equal sha256(carrier),
    so the same object serves both to query the relay *and* to decrypt the
    reports that come back (the location body is encrypted to this key).
    """
    return KeyPair(derive_scalar(uid, mid, payload).to_bytes(CARRIER_LEN, "big"))


def xorshift32(state: int) -> int:
    """One step of the firmware's xorshift32 (espbench.c), as a pure function."""
    x = state & 0xFFFFFFFF
    x ^= (x << 13) & 0xFFFFFFFF
    x ^= x >> 17
    x ^= (x << 5) & 0xFFFFFFFF
    return x


def expected_sequence(mode: int, *, mid_base: int, count: int,
                      payload_start: int = 0, seed: int = 1) -> list[tuple[int, int]]:
    """Reproduce the exact (mid, payload) sequence espbench.c emits for a run.

    This is the deterministic ground truth for the "seed+config" half of the
    cross-check; the serial log is the other half. They must agree.
    """
    if mode == MODE_STATIC:
        return [(mid_base, payload_start & 0xFF)]

    seq: list[tuple[int, int]] = []
    rng = seed & 0xFFFFFFFF
    for i in range(count):
        if mode == MODE_RANDOM:
            rng = xorshift32(rng)
            payload = rng & 0xFF
        else:  # MODE_INCREMENTAL
            payload = (payload_start + i) & 0xFF
        seq.append((mid_base + i, payload))
    return seq


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


def _report_record(report, key: KeyPair) -> dict:
    """Flatten one LocationReport into a JSON-serialisable record.

    The timestamp rides in the report envelope and is always available. The
    location body (lat/lon/accuracy/confidence/status) is encrypted to `key`;
    we hold the private scalar, so we decrypt it here. Decryption is best-effort:
    a malformed report keeps its timestamp and records the error rather than
    aborting the fetch.
    """
    # A stable id per physical observation, so repeated polls dedup cleanly. The
    # raw envelope payload is unique per report (it carries the ephemeral key), so
    # hashing it is safe even when two finders decrypt to the same second/spot.
    record = {"id": hashlib.sha256(report.payload).hexdigest()[:16],
              "timestamp": report.timestamp.isoformat()}
    try:
        report.decrypt(key)
        record.update({
            "latitude": report.latitude,
            "longitude": report.longitude,
            "horizontal_accuracy": report.horizontal_accuracy,
            "confidence": report.confidence,
            "status": report.status,
        })
    except Exception as exc:  # noqa: BLE001 - never let one bad report sink the run
        record["decrypt_error"] = repr(exc)
    return record


def fetch_reports_for_mid(account: AppleAccount, uid: bytes, mid: int, *,
                          since_epoch: float | None = None,
                          skew_s: float = 0.0) -> dict[int, list[dict]]:
    """Return {payload: [report records]} for every payload delivered.

    Builds all 256 candidate keys for the mid and pulls their full report
    history from the relay. For each payload octet that produced at least one
    surviving report, the value is the complete list of that carrier's reports --
    every passing device's observation, in chronological order -- not just the
    earliest. Each record carries the observation timestamp plus the decrypted
    location (latitude, longitude, horizontal_accuracy, confidence, status). An
    empty dict means the window was lost; more than one key means a carrier
    collision on the relay.

    Because we derive the private scalar for each candidate (keypair_for), the
    same key both queries the relay and decrypts the reports it returns.

    `since_epoch` (Unix seconds) discards any report observed before
    `since_epoch - skew_s`. A legitimate report can never predate the broadcast
    of its window, so passing the window's send time here rejects stale reports
    left on the relay by an earlier run that reused the same mid (the relay keeps
    reports for seven days). `skew_s` absorbs modest clock differences between the
    host and the observing devices.
    """
    keys = {p: keypair_for(uid, mid, p) for p in range(256)}
    results = account.fetch_location_history(list(keys.values()))
    if not isinstance(results, dict):
        results = {keys[0]: results}

    floor = None if since_epoch is None else (since_epoch - skew_s)
    out: dict[int, list[dict]] = {}
    for payload, key in keys.items():
        reports = sorted(results.get(key) or [], key=lambda r: r.timestamp)
        records = [
            _report_record(r, key)
            for r in reports
            if floor is None or r.timestamp.timestamp() >= floor
        ]
        if records:
            out[payload] = records
    return out

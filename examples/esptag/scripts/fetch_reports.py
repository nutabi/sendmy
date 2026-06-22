#!/usr/bin/env python3
"""Download Apple "Find My" location reports for the tag's rotating identities.

This is the report-fetching counterpart to scan_findmy.py. Where the scanner
confirms the tag's advertisement is heard *locally* over BLE, this script asks
Apple's servers for the crowd-sourced location reports that nearby iPhones have
uploaded for the tag's advertising key -- i.e. where the tag has actually been
seen.

To decrypt those reports you need the *private* scalar behind the advertised
key, not just the public `p_curr` the scanner sees: each report is ECIES-
encrypted to the advertising public key, so only the holder of the matching
private key can recover the latitude/longitude. We reconstruct that private key
straight from the provisioned key file (seed.keys, written by derive_keys.py):

    d_0, sk_0           <- seed.keys (the two root secrets)
    sk_i    = update^i(sk_0)               <- ratchet sk forward i epochs
    (u, v)  = KDF(sk_i, "diversify", 72)
    d_i     = (d_0*u + v) mod n            <- epoch-i private scalar (28-byte BE)
    p_i     = x(d_i * G)                   <- the advertised key (matches firmware)

This mirrors crypto_derive_p() / crypto_update_sk() in main/crypto.c: epoch 0
has sk_curr == sk_0, and each rotation applies one update_sk ratchet step.

ROTATING KEYS: the firmware changes identity every
CONFIG_ESPTAG_ROTATE_INTERVAL_MS, so reports for later epochs live under
p_1, p_2, ... -- this script derives a *range* of epochs and queries them all in
one batched request, attributing each returned report to its epoch. Pick the
range with --start/--epochs, or pass --interval-ms to size the range to Apple's
7-day retention window automatically. A static tag (CONFIG_ESPTAG_ROTATE_ENABLE=n)
is just the default --epochs 1 (epoch 0 only).

Reports are fetched for the last 7 days (Apple's retention window).

Apple account: fetching reports needs an authenticated Apple account plus an
anisette provider (Apple's anti-abuse device attestation). By default this runs
anisette *locally* in-process via the `anisette` library (no Docker/server
needed -- it downloads Apple's libraries on first use); pass --anisette-server to
use a remote anisette server instead. On first run, log in with --email/--password
(you'll be prompted for a 2FA code) and the session is saved to --account-data;
later runs restore from that file and skip login.

Run with the venv in this directory (where findmy is installed):

    scripts/.venv/bin/python scripts/fetch_reports.py --email you@icloud.com
    scripts/.venv/bin/python scripts/fetch_reports.py        # reuse saved session
    scripts/.venv/bin/python scripts/fetch_reports.py --epochs 32      # epochs 0..31
    scripts/.venv/bin/python scripts/fetch_reports.py --interval-ms 900000  # whole 7-day window
    scripts/.venv/bin/python scripts/fetch_reports.py --key-only      # just derive keys, no network

WARNING: seed.keys holds the tag's root secret; this script reads it and derives
the tag's private key in memory. Treat both the seed and the saved account
session (--account-data) as sensitive.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import sys
from pathlib import Path

# Re-exec under this directory's virtualenv so `findmy` and its deps are importable
# regardless of which python launched us. The shebang stays portable
# (`/usr/bin/env python3`); we can't point it at .venv because that path differs
# per checkout, so we locate the venv relative to this file instead. sys.prefix
# equals the venv root once we're inside it, so this is a no-op there.
_VENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
if sys.prefix != _VENV and os.environ.get("_ESPTAG_VENV_REEXEC") != "1":
    _py = os.path.join(_VENV, "bin", "python")
    if os.path.exists(_py):
        os.environ["_ESPTAG_VENV_REEXEC"] = "1"
        os.execv(_py, [_py, os.path.abspath(__file__), *sys.argv[1:]])

try:
    from findmy import KeyPair
    from findmy.reports import (
        AppleAccount,
        LocalAnisetteProvider,
        LoginState,
        RemoteAnisetteProvider,
    )
except ImportError:
    sys.exit("findmy is not installed. Use scripts/.venv/bin/python, "
             "or: pip install findmy")

# scripts/ is on sys.path[0] when this file is run directly (including after the
# venv re-exec above), so the sibling constants module imports cleanly. It is
# stdlib-only, so it loads under either interpreter.
from esptag_const import D_LEN, KEY_D0, KEY_SK0, P224_N, SK_LEN  # noqa: E402

# Apple's location-report retention window.
RETENTION_DAYS = 7


def kdf(z: bytes, info: bytes, out_len: int) -> bytes:
    """SP-800-108-style SHA-256 counter-mode KDF. Mirrors kdf() in crypto.c."""
    out = b""
    counter = 1
    while len(out) < out_len:
        out += hashlib.sha256(z + counter.to_bytes(4, "big") + info).digest()
        counter += 1
    return out[:out_len]


def load_seed(path: Path) -> tuple[bytes, bytes]:
    """Parse seed.keys, returning (d_0, sk_0) as raw big-endian bytes.

    seed.keys is the plaintext key file derive_keys.py writes: one
    `d_0=<hex>` / `sk_0=<hex>` line per root secret.
    """
    values: dict[str, bytes] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, _, hexval = line.partition("=")
        try:
            values[key.strip()] = bytes.fromhex(hexval.strip())
        except ValueError:
            sys.exit(f"{path}: bad hex for {key.strip()!r} (not a valid esptag seed?)")

    try:
        d_0, sk_0 = values[KEY_D0], values[KEY_SK0]
    except KeyError as e:
        sys.exit(f"{path}: missing {e.args[0]!r} line (not a valid esptag seed?)")
    if len(d_0) != D_LEN or len(sk_0) != SK_LEN:
        sys.exit(f"{path}: unexpected key sizes "
                 f"(d_0={len(d_0)}B, sk_0={len(sk_0)}B; want {D_LEN}/{SK_LEN})")
    return d_0, sk_0


def update_sk(sk: bytes) -> bytes:
    """One SK ratchet step: sk_next = KDF(sk, "update", 32). Mirrors crypto_update_sk."""
    return kdf(sk, b"update", SK_LEN)


def derive_private(d_0: bytes, sk: bytes) -> bytes:
    """Return the epoch private scalar d_epoch = (d_0*u + v) mod n (28-byte BE).

    `sk` is sk_curr for the epoch (sk_0 ratcheted `counter` times); this is
    crypto_derive_p's scalar step. At counter 0, sk == sk_0.
    """
    uv = kdf(sk, b"diversify", 72)
    u = int.from_bytes(uv[:36], "big")
    v = int.from_bytes(uv[36:], "big")
    d_epoch = (int.from_bytes(d_0, "big") * u + v) % P224_N
    return d_epoch.to_bytes(D_LEN, "big")


def derive_epoch_keys(d_0: bytes, sk_0: bytes, start: int, count: int):
    """Yield (epoch, private_scalar) for epochs [start, start+count).

    Ratchets sk_0 forward with update_sk, deriving the private scalar at each
    epoch -- the host-side mirror of the firmware advancing its ratchet on every
    rotation. `start` lets you skip ahead without re-deriving from 0 in output.
    """
    sk = sk_0
    for _ in range(start):
        sk = update_sk(sk)
    for epoch in range(start, start + count):
        yield epoch, derive_private(d_0, sk)
        sk = update_sk(sk)


def get_account(args) -> AppleAccount:
    """Restore a saved Apple account session, or log in interactively and save it."""
    account_path = Path(args.account_data)

    if account_path.exists():
        acc = AppleAccount.from_json(account_path)
        if acc.login_state == LoginState.LOGGED_IN:
            print(f"Restored session for {acc.account_name} from {account_path}.")
            return acc
        print(f"Saved session in {account_path} is not logged in; re-authenticating.")
    else:
        if args.anisette_server:
            anisette = RemoteAnisetteProvider(args.anisette_server)
        else:
            # Run anisette in-process; cache the downloaded Apple libs next to
            # the saved session so future first-logins don't re-download them.
            anisette = LocalAnisetteProvider(
                libs_path=account_path.with_name("anisette-libs.bin"))
        acc = AppleAccount(anisette)

    email = args.email or input("Apple ID email: ")
    password = args.password or getpass.getpass("Apple ID password: ")
    state = acc.login(email, password)

    if state == LoginState.REQUIRE_2FA:
        methods = acc.get_2fa_methods()
        for i, m in enumerate(methods):
            label = getattr(m, "phone_number", None) or type(m).__name__
            print(f"  [{i}] {label}")
        choice = methods[int(input("2FA method index: "))]
        choice.request()
        choice.submit(input("2FA code: "))

    acc.to_json(account_path)
    print(f"Logged in as {acc.account_name}; session saved to {account_path}.")
    return acc


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--seed", default=Path(__file__).resolve().parent.parent / "seed.keys",
                        type=Path,
                        help="path to the derive_keys.py key file (default: ../seed.keys next to the repo root)")
    parser.add_argument("--key-only", action="store_true",
                        help="just derive and print the epoch keys; no network")
    parser.add_argument("--start", type=int, default=0, metavar="EPOCH",
                        help="first epoch (rotation counter) to derive (default: 0)")
    parser.add_argument("--epochs", type=int, default=1, metavar="N",
                        help="number of consecutive epochs to derive and query "
                             "(default: 1 = static / epoch-0 only)")
    parser.add_argument("--interval-ms", type=int, default=None, metavar="MS",
                        help="rotation interval (CONFIG_ESPTAG_ROTATE_INTERVAL_MS); when "
                             "given, --epochs is computed to cover the 7-day report window")
    parser.add_argument("--email", default=None,
                        help="Apple ID email (prompted if omitted on first login)")
    parser.add_argument("--password", default=None,
                        help="Apple ID password (prompted if omitted; avoid on shared hosts)")
    parser.add_argument("--anisette-server", default=None, metavar="URL",
                        help="use a remote anisette server for first login "
                             "(e.g. http://localhost:6969); default runs anisette locally")
    parser.add_argument("--account-data",
                        default=Path(__file__).resolve().parent / "account.json",
                        type=Path,
                        help="path to save/restore the Apple session "
                             "(default: account.json next to this script). The "
                             "anisette libs cache is written alongside it.")
    args = parser.parse_args()

    if args.epochs < 1 or args.start < 0:
        sys.exit("--epochs must be >= 1 and --start must be >= 0")

    count = args.epochs
    if args.interval_ms is not None:
        if args.interval_ms < 1:
            sys.exit("--interval-ms must be >= 1")
        # Cover Apple's 7-day retention: one key per rotation interval, +1 for
        # the partial epoch straddling the window edge.
        count = (RETENTION_DAYS * 86_400_000) // args.interval_ms + 1
        print(f"Covering {RETENTION_DAYS}-day window at {args.interval_ms} ms/epoch "
              f"-> {count} epoch(s) from epoch {args.start}.")

    d_0, sk_0 = load_seed(args.seed)
    # Build one KeyPair per epoch, keeping the epoch index for report attribution.
    epoch_of: dict[KeyPair, int] = {}
    keypairs: list[KeyPair] = []
    for epoch, private in derive_epoch_keys(d_0, sk_0, args.start, count):
        kp = KeyPair(private)
        epoch_of[kp] = epoch
        keypairs.append(kp)
        # Only echo per-key detail for a small set; a full window is hundreds of keys.
        if count <= 8:
            print(f"epoch {epoch}: d_epoch={private.hex()} (SECRET)  "
                  f"p={kp.adv_key_bytes.hex()}  addr={kp.mac_address}")
    if count > 8:
        print(f"Derived {count} epoch keys "
              f"({args.start}..{args.start + count - 1}); p_{args.start} "
              f"= {keypairs[0].adv_key_bytes.hex()}.")
    if args.key_only:
        return

    acc = get_account(args)
    try:
        # Passing a list batches all static keys into one request and returns a
        # dict {keypair: [reports]}.
        reports_by_key = acc.fetch_location_history(keypairs)
    finally:
        # AppleAccount.close() is a coroutine even on the sync wrapper; drive it
        # on the account's own event loop rather than leaving it unawaited.
        acc._evt_loop.run_until_complete(acc.close())

    # Flatten to (epoch, report), sorted oldest-first across all epochs.
    flat = [(epoch_of[kp], r) for kp, rs in reports_by_key.items() for r in rs]
    flat.sort(key=lambda er: er[1].timestamp)

    if not flat:
        print(f"\nNo location reports in the last {RETENTION_DAYS} days "
              f"across {count} epoch(s).")
        return

    print(f"\n{len(flat)} report(s) across {count} epoch(s) (oldest first):")
    for epoch, r in flat:
        print(f"  [epoch {epoch}]  {r.timestamp.isoformat()}  "
              f"lat={r.latitude:+.6f} lon={r.longitude:+.6f}  "
              f"±{r.horizontal_accuracy}m  conf={r.confidence}")


if __name__ == "__main__":
    main()

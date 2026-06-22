#!/usr/bin/env python3
"""Scan for nearby Apple "offline finding" (Find My-style) BLE advertisements.

This is the receiver-side counterpart to the esptag firmware: the tag broadcasts
a rotating P-224 advertising key (`p_curr`) framed as an Apple offline-finding
manufacturer payload (`1e ff 4c 00 ...`, see main/ble_adv.c). FindMy.py's BLE
scanner parses that frame and, because the firmware sets the OF length byte to 25
(a "separated" advertisement), reconstructs the full 28-byte advertising public
key from the payload + random address. That lets you confirm the key the host
sees matches the `p_curr` the firmware logs at each rotation.

Run with the venv in this directory (where findmy is installed):

    scripts/.venv/bin/python scripts/scan_findmy.py            # scan 30 s
    scripts/.venv/bin/python scripts/scan_findmy.py -d 0       # until Ctrl-C
    scripts/.venv/bin/python scripts/scan_findmy.py --mac AA:BB:CC:DD:EE:FF
    scripts/.venv/bin/python scripts/scan_findmy.py --key <28-byte-hex>
    scripts/.venv/bin/python scripts/scan_findmy.py --rssi -60   # widen range

By default only close advertisements (RSSI >= -40 dBm) are shown, which keeps the
output to a tag sitting next to the host; lower the threshold to catch weaker
signals. Advertisements with no RSSI reported are always shown.

FindMy.py drives Bleak under the hood, so the usual per-platform BLE permissions
apply (on macOS the running terminal needs Bluetooth permission).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

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
    from findmy.scanner import OfflineFindingScanner, SeparatedOfflineFindingDevice
except ImportError:
    sys.exit("findmy is not installed. Use scripts/.venv/bin/python, "
             "or: pip install findmy")


def _full_key_hex(device) -> str | None:
    """28-byte advertising key (our p_curr) as hex, or None for non-separated frames.

    Only SeparatedOfflineFindingDevice carries the full key; Nearby advertisements
    (other Apple kit in range) expose only a partial key, which we skip.
    """
    if isinstance(device, SeparatedOfflineFindingDevice):
        return device.adv_key_bytes.hex()
    return None


async def scan(duration: float, mac_filter: str | None, key_filter: str | None,
               rssi_floor: float | None) -> None:
    scanner = await OfflineFindingScanner.create()

    if duration > 0:
        print(f"Scanning for offline-finding advertisements for {duration:g}s "
              f"(Ctrl-C to stop early)...")
        gen = scanner.scan_for(duration)
    else:
        print("Scanning for offline-finding advertisements (Ctrl-C to stop)...")
        gen = scanner.scan_for(float("inf"), extend_timeout=True)

    # The tag rotates its BLE address *together* with the key (the address is
    # derived from p_curr[0..5]), so a rotation is a brand-new (address, key) pair
    # — the same address never reappears with a different key. We therefore flag a
    # rotation when a key we've never seen turns up (a new epoch / identity), not
    # when an address's key changes. Repeated adverts of the same epoch (same key)
    # are not flagged.
    seen_keys: set[str] = set()
    seen_macs: set[str] = set()
    count = 0

    async for device in gen:
        mac = device.mac_address.upper()
        key = _full_key_hex(device)
        rssi = getattr(device, "rssi", None)

        if mac_filter and mac != mac_filter:
            continue
        if key_filter and key != key_filter:
            continue
        # Keep advertisements at or above the floor; pass through unknown RSSI.
        if rssi_floor is not None and rssi is not None and rssi < rssi_floor:
            continue

        count += 1
        seen_macs.add(mac)
        new_epoch = key is not None and key not in seen_keys
        rotated = new_epoch and len(seen_keys) > 0  # not the very first key
        if key is not None:
            seen_keys.add(key)

        rssi_str = f" rssi={rssi}" if rssi is not None else ""
        kind = type(device).__name__.replace("OfflineFindingDevice", "")
        if rotated:
            flag = "  <-- new epoch (rotation)"
        elif new_epoch:
            flag = "  <-- first identity"
        else:
            flag = "  (repeat)"

        print(f"[{count:4d}] {mac}{rssi_str}  ({kind})")
        if key is not None:
            print(f"       p_curr = {key}{flag}")
        else:
            print("       (nearby advert — no full key in payload)")

    print(f"\nDone. {count} matching advertisement(s); "
          f"{len(seen_keys)} distinct identit(y/ies); "
          f"{len(seen_macs)} distinct address(es).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-d", "--duration", type=float, default=30.0,
                        help="scan duration in seconds (0 = run until Ctrl-C; default 30)")
    parser.add_argument("--mac", default=None,
                        help="only show advertisements from this BLE address")
    parser.add_argument("--key", default=None,
                        help="only show advertisements whose 28-byte p_curr (hex) matches")
    parser.add_argument("--rssi", type=float, default=-40.0, metavar="DBM",
                        help="only show advertisements at or above this RSSI in dBm "
                             "(default -40; pass a lower value like -70 to widen)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show findmy's own log output, including the benign "
                             "'Invalid OF data length' warnings from neighboring "
                             "Apple devices (suppressed by default)")
    args = parser.parse_args()

    # findmy logs an error for every neighboring Apple device whose 'Nearby'
    # advertisement carries a non-2-byte payload. That noise is unrelated to our
    # tag (which advertises a 25-byte 'Separated' frame), so quiet it by default.
    if not args.verbose:
        logging.getLogger("findmy").setLevel(logging.CRITICAL)

    mac_filter = args.mac.upper() if args.mac else None
    key_filter = args.key.lower().replace(" ", "") if args.key else None

    try:
        asyncio.run(scan(args.duration, mac_filter, key_filter, args.rssi))
    except KeyboardInterrupt:
        print("\nInterrupted.")


if __name__ == "__main__":
    main()

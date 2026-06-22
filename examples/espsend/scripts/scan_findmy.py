#!/usr/bin/env python3
"""Scan for Find My (offline-finding) BLE advertisements and print the 28-byte
advertising key of every "separated" beacon seen.

This is the receive-side counterpart to the espsend firmware: the firmware
advertises a 28-byte carrier as a Find My key, and this script recovers that key
off the air so it can be matched against the carrier IDs the receiver derives.

A "separated" beacon broadcasts its full public key: 6 bytes ride in the BLE
static-random address, 22 in the Apple manufacturer payload, and the 2 missing
MSBs of byte 0 are carried in the payload's key_hi field. FindMy.py's
OfflineFindingScanner reassembles all 28 bytes into `adv_key_bytes`.

Platform note: on macOS, CoreBluetooth does NOT expose the BLE MAC address (it
hands out an opaque per-host UUID instead), so the first six key bytes cannot be
recovered and the printed key will be wrong. Run this on Linux (BlueZ) for
correct results. The script warns when it detects macOS.

Requires: findmy (FindMy.py) and its bleak dependency.
    pip install findmy
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from findmy.scanner import OfflineFindingScanner, SeparatedOfflineFindingDevice


async def scan(timeout: float, *, watch: bool, verbose: bool) -> int:
    scanner = await OfflineFindingScanner.create()

    seen: set[bytes] = set()
    async for device in scanner.scan_for(timeout, extend_timeout=watch):
        # Only "separated" beacons carry a full 28-byte key; "nearby" ones
        # advertise a partial key and are skipped.
        if not isinstance(device, SeparatedOfflineFindingDevice):
            continue

        key = device.adv_key_bytes
        if key in seen:
            continue
        seen.add(key)

        if verbose:
            print(
                f"{device.mac_address}  rssi={device.rssi:>4}dBm  "
                f"status=0x{device.status:02x}  {key.hex()}",
                flush=True,
            )
        else:
            print(key.hex(), flush=True)

    return 0 if seen else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scan Find My advertisements and print the 28-byte key(s).",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=10.0,
        help="scan duration in seconds (default: 10)",
    )
    parser.add_argument(
        "-w",
        "--watch",
        action="store_true",
        help="keep scanning, extending the timeout as long as devices appear",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="also print MAC address, RSSI, and status byte",
    )
    args = parser.parse_args()

    if sys.platform == "darwin":
        print(
            "warning: macOS hides the BLE MAC address; the first 6 key bytes "
            "will be incorrect. Use Linux for accurate keys.",
            file=sys.stderr,
        )

    try:
        rc = asyncio.run(scan(args.timeout, watch=args.watch, verbose=args.verbose))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()

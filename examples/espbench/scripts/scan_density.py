#!/usr/bin/env python3
"""Measure nearby Apple-device density as a Find My relay-finder proxy.

Passively BLE-scans for Apple Continuity advertisements (manufacturer id 0x004C)
and counts distinct devices, classified by Continuity message type. The count of
devices emitting "Nearby Info" (type 0x10) is the finder-density signal: those
are active iPhones/iPads/Macs that can relay a Find My broadcast. Find My beacons
(type 0x12 — AirTags, lost devices, *and our own espbench ESP32*) are reported
separately and are NOT finders.

macOS note: CoreBluetooth hides the hardware MAC and hands out a per-host
CoreBluetooth UUID instead. Apple devices also rotate their identifier roughly
every 15 min, so distinct-device counts are only meaningful within a short
window (the default 10 s snapshot is well inside that).

Usage:
  # one snapshot
  python scan_density.py

  # snapshot counting only strong (near) devices
  python scan_density.py --window 10 --rssi-min -70

  # log a density time-series alongside a matrix run
  python scan_density.py --watch --interval 60 --out results/<run>/density.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakScanner

APPLE_COMPANY_ID = 0x004C

# Apple Continuity message types (first byte of the 0x004C payload).
CONTINUITY_TYPES = {
    0x02: "iBeacon",
    0x05: "AirDrop",
    0x06: "HomeKit",
    0x07: "ProximityPairing",   # AirPods et al.
    0x08: "HeySiri",
    0x09: "AirPlayTarget",
    0x0A: "AirPrint",
    0x0C: "Handoff",
    0x0D: "TetheringSource",
    0x0E: "TetheringTarget",
    0x0F: "NearbyAction",
    0x10: "NearbyInfo",         # active iPhone/iPad/Mac -> relay-capable finder
    0x12: "FindMy",             # offline-finding beacon (AirTag / our ESP32)
    0x16: "Unknown16",
}

FINDER_TYPE = 0x10   # NearbyInfo == an active, relay-capable Apple device
BEACON_TYPE = 0x12   # FindMy beacon (not a finder)


async def snapshot(window_s: float, rssi_min: int | None) -> dict:
    """Scan for ``window_s`` seconds; return a density summary."""
    # address -> {"rssi": best_rssi, "types": set(type_bytes)}
    devices: dict[str, dict] = defaultdict(lambda: {"rssi": -999, "types": set()})

    def on_detect(dev, adv):
        payload = (adv.manufacturer_data or {}).get(APPLE_COMPANY_ID)
        if not payload:
            return
        rssi = adv.rssi if adv.rssi is not None else -999
        if rssi_min is not None and rssi < rssi_min:
            return
        entry = devices[dev.address]
        entry["rssi"] = max(entry["rssi"], rssi)
        entry["types"].add(payload[0])

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    await asyncio.sleep(window_s)
    await scanner.stop()

    type_counts: dict[int, int] = defaultdict(int)
    finders = beacons = 0
    for entry in devices.values():
        for t in entry["types"]:
            type_counts[t] += 1
        if FINDER_TYPE in entry["types"]:
            finders += 1
        if BEACON_TYPE in entry["types"]:
            beacons += 1

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_s": window_s,
        "rssi_min": rssi_min,
        "apple_total": len(devices),
        "finders": finders,          # type 0x10 NearbyInfo
        "beacons": beacons,          # type 0x12 FindMy
        "type_counts": dict(type_counts),
    }


def format_snapshot(s: dict) -> str:
    named = {CONTINUITY_TYPES.get(t, f"0x{t:02x}"): n
             for t, n in sorted(s["type_counts"].items())}
    near = "" if s["rssi_min"] is None else f" (rssi>={s['rssi_min']})"
    return (f"{s['timestamp']}  finders={s['finders']}  "
            f"apple_total={s['apple_total']}  beacons={s['beacons']}{near}\n"
            f"    by type: {named}")


CSV_FIELDS = ["timestamp", "window_s", "rssi_min", "apple_total",
              "finders", "beacons"]


def append_csv(path: Path, s: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        if new:
            w.writeheader()
        w.writerow({k: s[k] for k in CSV_FIELDS})


async def main_async(args) -> None:
    out = Path(args.out) if args.out else None
    while True:
        s = await snapshot(args.window, args.rssi_min)
        print(format_snapshot(s), flush=True)
        if out:
            append_csv(out, s)
        if not args.watch:
            break
        await asyncio.sleep(max(0.0, args.interval - args.window))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--window", type=float, default=10.0,
                   help="scan duration per snapshot, seconds (default 10)")
    p.add_argument("--rssi-min", type=int, default=None,
                   help="only count devices at/above this RSSI (e.g. -70 for near)")
    p.add_argument("--watch", action="store_true",
                   help="keep sampling every --interval seconds")
    p.add_argument("--interval", type=float, default=60.0,
                   help="seconds between snapshot starts in --watch mode (default 60)")
    p.add_argument("--out", type=Path, default=None,
                   help="append each snapshot to this CSV")
    args = p.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)


if __name__ == "__main__":
    main()

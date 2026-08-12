#!/usr/bin/env python3
"""Recover espbench payload octets for a range of message-ids.

A manual counterpart to run_matrix.py's fetch stage: given a starting mid and a
count, it brute-forces all 256 candidate carriers per mid against the relay and
prints which payload (if any) was delivered. Handy for spot-checking a run
without the full harness, or for re-fetching an old run (the relay keeps reports
for seven days).

    # first run logs in and saves the session to account.json
    scripts/.venv/bin/python scripts/fetch_reports.py --mid-base 0 --count 16
    scripts/.venv/bin/python scripts/fetch_reports.py --mid-base 1000  # single mid
"""

from __future__ import annotations

import argparse
import sys

import bench_common as bc


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mid-base", type=int, default=0,
                        help="first message-id to recover (default: 0)")
    parser.add_argument("--count", type=int, default=1,
                        help="how many consecutive mids to recover (default: 1)")
    args = parser.parse_args()

    if args.mid_base < 0 or args.mid_base + args.count - 1 > 0xFFFFFFFF:
        sys.exit("error: mid range must fit in a 32-bit unsigned integer")
    if args.count < 1:
        sys.exit("error: --count must be >= 1")

    uid = bc.read_uid()
    account = bc.load_account()

    delivered = 0
    for mid in range(args.mid_base, args.mid_base + args.count):
        reports = bc.fetch_reports_for_mid(account, uid, mid)
        if not reports:
            print(f"mid={mid}: no carrier present (window lost)", file=sys.stderr)
            continue
        delivered += 1
        if len(reports) > 1:
            hexes = ", ".join(f"0x{p:02x}" for p in sorted(reports))
            print(f"mid={mid}: warning, {len(reports)} carriers present ({hexes})",
                  file=sys.stderr)
        for payload in sorted(reports):
            recs = reports[payload]  # chronological
            first_seen = recs[0]["timestamp"]
            last_seen = recs[-1]["timestamp"]
            loc = next((f"{r['latitude']:.5f},{r['longitude']:.5f}"
                        for r in recs if "latitude" in r), "n/a")
            print(f"mid={mid} payload=0x{payload:02x} ({payload}) reports={len(recs)} "
                  f"first_seen={first_seen} last_seen={last_seen} loc={loc}")

    print(f"delivered {delivered}/{args.count} windows", file=sys.stderr)
    sys.exit(0 if delivered else 1)


if __name__ == "__main__":
    main()

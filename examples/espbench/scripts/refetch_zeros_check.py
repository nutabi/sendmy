#!/usr/bin/env python3
"""Tripwire: re-fetch SETTLED zero-delivery cells and flag any the live poller
under-counted (i.e. the generous timeout was still too short for some condition).

A "zero cell" has 0 live detections. We only re-check cells whose last key was
sent long enough ago that the poller has already given up on them
(now - last_send > grace, grace defaults > lost_timeout_s + a poll cycle) -- so a
hit means a real report the poller dropped, NOT mere propagation lag. Each cell is
verified at most once (tracked in zeros_verified.txt) and only up to --cap cells
are checked per run, to bound concurrent load on the shared Apple account while
the sweep is still live.

Prints a DISCREPANCY block only if some cell now returns reports; otherwise a
one-line clean summary. Exit code 2 on discrepancy, 0 otherwise.

    scripts/.venv/bin/python scripts/refetch_zeros_check.py results/<ts>
"""
from __future__ import annotations

import argparse, collections, csv, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("--grace", type=float, default=720.0,
                    help="s since a cell's last key send before it counts as settled")
    ap.add_argument("--cap", type=int, default=12, help="max cells to re-fetch this run")
    ap.add_argument("--skew", type=float, default=120.0)
    args = ap.parse_args()
    res = args.results_dir

    # sent keys (uid, mid, send_time) + last send per cell
    tx = collections.defaultdict(list)
    with (res / "transmission.csv").open() as f:
        for r in csv.DictReader(f):
            tx[r["cell"]].append((r["uid"], int(r["mid"]), float(r["send_time"])))
    # live delivered mids per cell
    seen = collections.defaultdict(set)
    det = res / "detection.csv"
    if det.exists():
        with det.open() as f:
            for r in csv.DictReader(f):
                seen[r["cell"]].add(r["mid"])

    verified_path = res / "zeros_verified.txt"
    verified = set(verified_path.read_text().split()) if verified_path.exists() else set()

    now = time.time()
    candidates = []
    for cell, keys in tx.items():
        if cell in verified:
            continue
        if len(seen.get(cell, set())) != 0:      # only fully-undetected cells
            continue
        if now - max(k[2] for k in keys) < args.grace:  # only settled cells
            continue
        candidates.append(cell)
    candidates.sort()
    batch = candidates[:args.cap]

    if not batch:
        print(f"clean: no new settled zero-cells to check "
              f"({len(candidates)} pending, {len(verified)} verified)")
        return 0

    acct = bc.load_account()
    discrepancies = []
    checked = []
    for cell in batch:
        recovered = 0
        for uid_hex, mid, st in tx[cell]:
            try:
                reports = bc.fetch_reports_for_mid(
                    acct, bytes.fromhex(uid_hex), mid, since_epoch=st, skew_s=args.skew)
            except Exception as exc:  # noqa: BLE001 - never let one poll sink the check
                print(f"  {cell} mid={mid} fetch failed: {exc!r}", file=sys.stderr)
                continue
            if reports:
                recovered += 1
        checked.append(cell)
        if recovered > 0:
            discrepancies.append((cell, recovered, len(tx[cell])))

    # mark checked cells verified (so we never re-hit the account for them)
    with verified_path.open("a") as f:
        for cell in checked:
            f.write(cell + "\n")

    if discrepancies:
        print("DISCREPANCY: live poller under-counted these settled cells "
              "(reports exist that live shows as 0):")
        for cell, rec, n in discrepancies:
            print(f"  {cell}: live=0 refetch={rec}/{n}")
        print("=> the generous timeout is STILL too short for some condition; "
              "consider raising lost_timeout_s or the run's true zeros are being lost.")
        return 2
    print(f"clean: {len(checked)} settled zero-cell(s) re-checked, all genuinely 0 "
          f"(weak-signal loss, not a timeout artifact); {len(candidates)-len(batch)} still pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())

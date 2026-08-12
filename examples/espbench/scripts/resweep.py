#!/usr/bin/env python3
"""Offline ground-truth resweep of a finished run's deliverability.

Re-fetches every transmitted key (uid, mid) directly from the relay and counts a
key delivered iff at least one report survives. Unlike the live poller this has no
queue cap and no adaptive timeout, so it is not subject to the under-count those
cause (see the README note); it is the authoritative deliverability measure. Run
it after a run has settled -- and re-run it later to pick up late-propagating
reports (weak-signal keys can keep arriving for minutes).

    scripts/.venv/bin/python scripts/resweep.py results/<ts>            # whole run
    scripts/.venv/bin/python scripts/resweep.py results/<ts> --index 45-119
    scripts/.venv/bin/python scripts/resweep.py results/<ts> -o sweep.csv

Writes `cell,delivered,total` (default results/<ts>/resweep.csv). Feed one or more
of these CSVs to `analyze2d.py --resweep` for the surface + significance tests;
combining runs is just passing several CSVs (later ones win per cell).
"""
from __future__ import annotations

import argparse, collections, csv, re, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_common as bc

IDX_RE = re.compile(r"^s(\d+)_")


def parse_index_range(s: str) -> tuple[int, int]:
    lo, _, hi = s.partition("-")
    return int(lo), int(hi or lo)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", type=Path)
    ap.add_argument("-o", "--out", type=Path,
                    help="output CSV (default <results_dir>/resweep.csv)")
    ap.add_argument("--index", help="only cells whose sNNN index is in LO-HI")
    ap.add_argument("--skew", type=float, default=120.0,
                    help="clock-skew grace subtracted from a key's send_time floor")
    args = ap.parse_args()
    res = args.results_dir
    out = args.out or (res / "resweep.csv")
    lo, hi = parse_index_range(args.index) if args.index else (None, None)

    tx = collections.defaultdict(list)  # cell -> [(uid_hex, mid, send_time)]
    with (res / "transmission.csv").open() as f:
        for r in csv.DictReader(f):
            m = IDX_RE.match(r["cell"])
            if lo is not None and m and not (lo <= int(m.group(1)) <= hi):
                continue
            tx[r["cell"]].append((r["uid"], int(r["mid"]), float(r["send_time"])))
    cells = sorted(tx, key=lambda c: (int(IDX_RE.match(c).group(1)) if IDX_RE.match(c) else 0, c))
    total_keys = sum(len(v) for v in tx.values())
    print(f"resweep {res.name}: {len(cells)} cells, {total_keys} keys -> {out}", flush=True)

    acct = bc.load_account()
    out.write_text("cell,delivered,total\n")
    t0 = time.time(); done = 0
    with out.open("a") as fh:
        for i, cell in enumerate(cells):
            delivered = 0
            for uid_hex, mid, st in tx[cell]:
                try:
                    rep = bc.fetch_reports_for_mid(
                        acct, bytes.fromhex(uid_hex), mid,
                        since_epoch=st, skew_s=args.skew)
                except Exception as exc:  # noqa: BLE001 - never let one key sink the sweep
                    print(f"  {cell} mid={mid} fetch failed: {exc!r}", file=sys.stderr, flush=True)
                    rep = None
                if rep:
                    delivered += 1
                done += 1
            fh.write(f"{cell},{delivered},{len(tx[cell])}\n"); fh.flush()
            el = time.time() - t0; rate = done / el if el else 0
            eta = (total_keys - done) / rate / 60 if rate else 0
            print(f"[{i+1}/{len(cells)}] {cell}: {delivered}/{len(tx[cell])} "
                  f"({done}/{total_keys} keys, eta {eta:.1f}m)", flush=True)
    print(f"done in {(time.time()-t0)/60:.1f}m -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

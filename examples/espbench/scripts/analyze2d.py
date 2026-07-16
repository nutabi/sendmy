#!/usr/bin/env python3
"""2D (TX-power x rotation) deliverability / throughput surface for a run.

Groups cells named `sNNN_p{|dBm|}_r{sec}` back into experimental conditions,
pools the reps, and reports deliverability and delivered-throughput (deliv /
rotation) per condition with bootstrap CIs over cells (respecting the fact that a
cell's keys are clustered). `--final` also runs permutation tests for the main
effects of rotation and TX power.

    scripts/.venv/bin/python scripts/analyze2d.py [results/<ts>] [--final]

Delivered = distinct (cell, mid) with >=1 report (the live poller count). Valid as
true deliverability only under a generous, non-adaptive poll timeout.

For authoritative deliverability, feed one or more ground-truth resweep CSVs
(`cell,delivered,total`, produced by resweep.py) instead of reading a run's live
counts. Combining runs is just passing several CSVs; a later CSV overrides an
earlier one per cell (so a fresher resweep of the same cells wins):

    scripts/.venv/bin/python scripts/analyze2d.py --resweep a.csv --resweep b.csv --final
"""
from __future__ import annotations

import argparse, csv, random, re, sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CELL_RE = re.compile(r"^s\d+_p(\d+)_r(\d+)$")
random.seed(1)


def latest_run() -> Path | None:
    runs = sorted((ROOT / "results").glob("*/"), key=lambda p: p.name)
    return runs[-1] if runs else None


def load(run: Path):
    """-> {(tx, rot): [(delivered, sent) per cell]}, plus overall counts."""
    sent = defaultdict(int); seen = defaultdict(set)
    with (run / "transmission.csv").open() as f:
        for r in csv.DictReader(f):
            sent[r["cell"]] += 1
    # Deliverability = (cell, mid) present in detection.csv OR deliverability.csv
    # (the fast-tier + slow-tier union). Either file may be absent.
    for name in ("detection.csv", "deliverability.csv"):
        path = run / name
        if path.exists():
            with path.open() as f:
                for r in csv.DictReader(f):
                    seen[r["cell"]].add(r["mid"])
    cond = defaultdict(list)
    for cell, n in sent.items():
        m = CELL_RE.match(cell)
        if not m:
            continue
        tx = -int(m.group(1)); rot = int(m.group(2))
        cond[(tx, rot)].append((len(seen.get(cell, ())), n))
    return cond


def load_resweep(files):
    """Ground-truth resweep CSVs (`cell,delivered,total`) -> same cond shape as
    load(). Later files override earlier per cell, so a fresher resweep wins and
    combining runs is just passing several CSVs."""
    per_cell = {}  # cell -> (delivered, total); last write wins
    for path in files:
        with Path(path).open() as f:
            for r in csv.DictReader(f):
                per_cell[r["cell"]] = (int(r["delivered"]), int(r["total"]))
    cond = defaultdict(list)
    for cell, (d, n) in per_cell.items():
        m = CELL_RE.match(cell)
        if not m:
            continue
        cond[(-int(m.group(1)), int(m.group(2)))].append((d, n))
    return cond


def boot_ci(cells, f, iters=4000):
    """Bootstrap 95% CI of statistic f(resampled cells). cells: list of tuples."""
    if not cells:
        return (0.0, 0.0)
    n = len(cells); vals = []
    for _ in range(iters):
        samp = [cells[random.randrange(n)] for _ in range(n)]
        vals.append(f(samp))
    vals.sort()
    return (vals[int(0.025 * iters)], vals[int(0.975 * iters)])


def rate(cells):
    d = sum(c[0] for c in cells); s = sum(c[1] for c in cells)
    return d / s if s else 0.0


def perm_main_effect(cond, axis, iters=20000):
    """Permutation test: does deliverability differ across levels of `axis`
    (0=tx, 1=rot), pooling over the other axis? Cell-level; statistic = between-
    level SS of per-cell fractions."""
    cells = []  # (level, frac)
    for (tx, rot), lst in cond.items():
        lvl = tx if axis == 0 else rot
        for d, s in lst:
            if s:
                cells.append((lvl, d / s))
    if len({c[0] for c in cells}) < 2:
        return None
    grand = sum(c[1] for c in cells) / len(cells)
    def ssb(labels):
        by = defaultdict(list)
        for (_, v), l in zip(cells, labels):
            by[l].append(v)
        return sum(len(v) * (sum(v) / len(v) - grand) ** 2 for v in by.values())
    labels = [c[0] for c in cells]
    obs = ssb(labels)
    perm = labels[:]; ge = 0
    for _ in range(iters):
        random.shuffle(perm)
        if ssb(perm) >= obs - 1e-12:
            ge += 1
    return (ge + 1) / (iters + 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", nargs="?", type=Path)
    ap.add_argument("--resweep", action="append", metavar="CSV",
                    help="ground-truth resweep CSV (repeatable; later wins per cell)")
    ap.add_argument("--final", action="store_true")
    args = ap.parse_args()

    if args.resweep:
        cond = load_resweep(args.resweep)
        label = "+".join(Path(p).name for p in args.resweep) + " (ground-truth resweep)"
    else:
        run = args.results_dir or latest_run()
        if not run or not run.exists():
            sys.exit("no results dir")
        cond = load(run)
        label = f"{run.name}  (live poller)"
    if not cond:
        sys.exit(f"{label}: no p/r-named cells")
    txs = sorted({tx for tx, _ in cond})
    rots = sorted({rot for _, rot in cond})

    tot_d = sum(c[0] for l in cond.values() for c in l)
    tot_s = sum(c[1] for l in cond.values() for c in l)
    print(f"# {label}  delivered {tot_d}/{tot_s} "
          f"({100*tot_d/tot_s:.1f}%)  conditions {len(cond)}/{len(txs)*len(rots)}")

    print("\n## deliverability %  (rows=TX dBm, cols=rotation s; [95% CI])")
    print(f"{'TX':>5} " + "".join(f"{f'r{r}':>18}" for r in rots))
    for tx in txs:
        row = f"{tx:>5} "
        for rot in rots:
            cells = cond.get((tx, rot))
            if not cells:
                row += f"{'-':>18}"; continue
            p = 100 * rate(cells)
            lo, hi = boot_ci(cells, lambda c: 100 * rate(c))
            row += f"{f'{p:.0f} [{lo:.0f}-{hi:.0f}]':>18}"
        print(row)

    print("\n## delivered throughput (bytes/min = deliv*60/rotation_s); * = best rotation per TX")
    print(f"{'TX':>5} " + "".join(f"{f'r{r}':>12}" for r in rots))
    for tx in txs:
        thr = {rot: rate(cond[(tx, rot)]) * 60 / rot
               for rot in rots if (tx, rot) in cond}
        best = max(thr, key=thr.get) if thr else None
        row = f"{tx:>5} "
        for rot in rots:
            if rot not in thr:
                row += f"{'-':>12}"; continue
            mark = "*" if rot == best else " "
            row += f"{f'{thr[rot]:.1f}{mark}':>12}"
        print(row)

    if args.final:
        print("\n## main-effect permutation tests (cell-level)")
        p_rot = perm_main_effect(cond, 1)
        p_tx = perm_main_effect(cond, 0)
        print(f"rotation effect (pooled over TX): p={p_rot}")
        print(f"TX-power effect (pooled over rot): p={p_tx}")


if __name__ == "__main__":
    main()

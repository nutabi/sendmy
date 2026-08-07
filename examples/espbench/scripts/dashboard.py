#!/usr/bin/env python3
"""Live dashboard for a running (or finished) espbench matrix.

Watches a results dir and redraws a delivered/sent grid grouped by the cell-name
axes, plus overall progress and the latest near-finder density. Cell names are
`sNNN_<axisA>[_<axisB>]` (e.g. `s009_p21_r04` -> rows p21, cols r04; or the older
`s000_u06` -> a single row of update-interval columns), so the same view works
for the 2D TX-power x rotation sweep and the 1D interval sweeps.

    scripts/.venv/bin/python scripts/dashboard.py                 # latest run, refresh 5s
    scripts/.venv/bin/python scripts/dashboard.py results/<ts> -i 2
    scripts/.venv/bin/python scripts/dashboard.py --once          # single render (for `watch`)

Delivered = distinct (cell, mid) that produced >=1 report in detection.csv, i.e.
the live poller count. Under a generous (non-adaptive) timeout that tracks true
deliverability; under an aggressive one it is a floor (see the README note).
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^s\d+_(.+)$")

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
def color(pct: float) -> str:
    if pct >= 90: return "\033[32m"   # green
    if pct >= 70: return "\033[33m"   # yellow
    if pct >= 40: return "\033[35m"   # magenta
    return "\033[31m"                 # red


RUN_TS_RE = re.compile(r"(\d{8}T\d{6}Z)$")


def latest_run() -> Path | None:
    """Most recent run dir, by the trailing UTC stamp.

    Run dirs are `<matrix-label>_<stamp>` (older ones are a bare `<stamp>`), so
    sorting by name would order by label first and pick the alphabetically last
    matrix rather than the newest run. Sort on the stamp, falling back to mtime
    for a dir that does not carry one.
    """
    def key(p: Path) -> tuple[int, str | float]:
        m = RUN_TS_RE.search(p.name)
        return (1, m.group(1)) if m else (0, p.stat().st_mtime)

    runs = sorted((ROOT / "results").glob("*/"), key=key)
    return runs[-1] if runs else None


def axes(cell: str) -> tuple[str, str]:
    """(row, col) axis labels from the cell name. One axis -> row is ''."""
    m = NAME_RE.match(cell)
    parts = m.group(1).split("_") if m else [cell]
    return (parts[0], parts[1]) if len(parts) >= 2 else ("", parts[0])


def load(run: Path):
    sent = defaultdict(int)                 # cell -> keys sent
    seen = defaultdict(set)                  # cell -> {mid delivered}
    tx_path = run / "transmission.csv"
    last_cell = None
    if tx_path.exists():
        with tx_path.open() as f:
            for r in csv.DictReader(f):
                sent[r["cell"]] += 1
                last_cell = r["cell"]
    # Delivered = (cell, mid) in detection.csv OR deliverability.csv (fast + slow
    # tier union). Either file may be absent (old runs, or slow tier disabled).
    for name in ("detection.csv", "deliverability.csv"):
        path = run / name
        if path.exists():
            with path.open() as f:
                for r in csv.DictReader(f):
                    seen[r["cell"]].add(r["mid"])
    return sent, seen, last_cell


def density(run: Path) -> str:
    p = run / "density.csv"
    if not p.exists():
        return "n/a"
    rows = p.read_text().splitlines()
    if len(rows) < 2:
        return "n/a"
    hdr = rows[0].split(",")
    last = dict(zip(hdr, rows[-1].split(",")))
    near = last.get("finders", "?")
    rssi = last.get("rssi_min") or "all"
    ts = last.get("timestamp", "")[11:19]
    return f"{near} near finders (rssi>={rssi}) @ {ts}"


def total_cells(run: Path) -> int | None:
    import json
    p = run / "cells.json"
    if p.exists():
        try:
            return len(json.loads(p.read_text())["cells"])
        except Exception:
            return None
    return None


def render(run: Path) -> str:
    sent, seen, last_cell = load(run)
    rows_set, cols_set = set(), set()
    grid_s = defaultdict(int)
    grid_d = defaultdict(int)
    for cell, ns in sent.items():
        rlab, clab = axes(cell)
        rows_set.add(rlab); cols_set.add(clab)
        grid_s[(rlab, clab)] += ns
        grid_d[(rlab, clab)] += len(seen.get(cell, ()))
    rows = sorted(rows_set); cols = sorted(cols_set)

    out = []
    tot = total_cells(run)
    done = len(sent)
    ts = f"{done}/{tot}" if tot else str(done)
    tot_s = sum(sent.values()); tot_d = sum(len(v) for v in seen.values())
    pct = 100 * tot_d / tot_s if tot_s else 0
    out.append(f"{BOLD}espbench live{RESET}  {run.name}   cells {ts}   "
               f"delivered {tot_d}/{tot_s} ({color(pct)}{pct:.1f}%{RESET})")
    out.append(f"{DIM}density:{RESET} {density(run)}"
               + (f"   {DIM}current:{RESET} {last_cell}" if last_cell else ""))
    out.append("")

    # grid: rows down, cols across; each = delivered/sent (pct%)
    cw = 13
    header = " " * 6 + "".join(f"{c:>{cw}}" for c in cols)
    out.append(BOLD + header + RESET)
    for rlab in rows:
        line = f"{rlab:<6}"
        for clab in cols:
            s = grid_s[(rlab, clab)]; d = grid_d[(rlab, clab)]
            if s == 0:
                line += f"{DIM}{'-':>{cw}}{RESET}"
            else:
                p = 100 * d / s
                cell = f"{d}/{s} {p:.0f}%"
                line += f"{color(p)}{cell:>{cw}}{RESET}"
        # row marginal
        rs = sum(grid_s[(rlab, c)] for c in cols)
        rd = sum(grid_d[(rlab, c)] for c in cols)
        rp = 100 * rd / rs if rs else 0
        line += f"  {DIM}|{RESET} {color(rp)}{rp:4.0f}%{RESET}"
        out.append(line)
    # column marginals
    out.append("")
    foot = f"{DIM}{'col%':<6}{RESET}"
    for clab in cols:
        cs = sum(grid_s[(r, clab)] for r in rows)
        cd = sum(grid_d[(r, clab)] for r in rows)
        cp = 100 * cd / cs if cs else 0
        s = f"{cp:.0f}%"
        foot += f"{color(cp)}{s:>{cw}}{RESET}"
    out.append(foot)
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results_dir", nargs="?", type=Path)
    ap.add_argument("-i", "--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    run = args.results_dir or latest_run()
    if not run or not run.exists():
        sys.exit("no results dir found (pass one explicitly)")

    if args.once:
        print(render(run)); return
    try:
        while True:
            sys.stdout.write("\033[2J\033[H")   # clear + home
            sys.stdout.write(render(run) + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

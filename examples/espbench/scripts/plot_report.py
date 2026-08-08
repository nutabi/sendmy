#!/usr/bin/env python3
"""Generate the report figure set from run artifacts.

Writes SVG (vector, for the report) and PNG (200 dpi, for slides) into
report/assets/. Re-runnable: every figure is derived from the run directories
under results/, except run A's live-vs-offline numbers, which are transcribed
from the README because that run's artifacts were not retained.

    scripts/.venv/bin/python scripts/plot_report.py
"""

import csv
import json
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
OUT = ROOT / "report" / "assets"

RUN_ADV = RESULTS / "advertising_20260717T163332Z"
RUN_FAC = RESULTS / "dwell_isobroadcast_20260806T155051Z"
RUN_COMB = RESULTS / "advsweep_20260808T012343Z"
RUN_COMB2 = RESULTS / "advsweep2_20260808T111327Z"
RUN_BC = [RESULTS / "broadcasts_20260807T133206Z",
          RESULTS / "broadcasts_resume_20260807T142927Z"]

# -- palette (validated light-mode instance; see dataviz references/palette.md) --
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
S1 = "#2a78d6"  # categorical slot 1 (blue)
S2 = "#eb6834"  # categorical slot 2 (orange)
BLUE_250 = "#86b6ef"  # ordinal light step, >= 2:1 on the light surface
RED = "#e34948"  # diverging pole opposite blue


def style():
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
        "font.size": 10,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": AXIS,
        "axes.linewidth": 1.0,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelcolor": INK_2,
        "ytick.labelcolor": INK_2,
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "svg.fonttype": "none",
    })


def frame(ax, grid_axis="y"):
    """Hairline solid grid, recessive axes, no top/right spines."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(True, axis=grid_axis, color=GRID, linewidth=1.0, linestyle="-")
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def save(fig, name):
    OUT.mkdir(parents=True, exist_ok=True)
    for ext, kw in (("svg", {}), ("png", {"dpi": 200})):
        fig.savefig(OUT / f"{name}.{ext}", bbox_inches="tight", **kw)
    plt.close(fig)
    print(f"wrote {name}.svg / .png")


# ---------------------------------------------------------------- data loading

def propagation(run):
    """Per-key propagation delay: send -> first fetchable report, seconds."""
    out = []
    for cell in json.load(open(run / "summary.json")):
        for key in cell["detail"]:
            v = [r["propagation_latency_s"]
                 for reports in key["reports"].values() for r in reports
                 if r.get("propagation_latency_s") is not None]
            if v:
                out.append(min(v))
    return sorted(out)


def factorial_cells():
    """Run E: {(dwell_s, broadcasts_per_key): [cell, ...]}."""
    g = defaultdict(list)
    for cell in json.load(open(RUN_FAC / "summary.json")):
        m = re.search(r"d(\d+)b(\d+)$", cell["name"])
        g[(int(m.group(1)), int(m.group(2)))].append(cell)
    return g


def cell_propagation(cell):
    v = []
    for key in cell["detail"]:
        p = [r["propagation_latency_s"]
             for reports in key["reports"].values() for r in reports
             if r.get("propagation_latency_s") is not None]
        if p:
            v.append(min(p))
    return v


def resweep_by(run, pattern):
    g = defaultdict(lambda: [0, 0])
    for r in csv.DictReader(open(run / "resweep.csv")):
        m = re.search(pattern, r["cell"])
        g[int(m.group(1))][0] += int(r["delivered"])
        g[int(m.group(1))][1] += int(r["total"])
    return g


# -------------------------------------------------------------------- figures

def fig_propagation_cdf():
    """Job: distribution of a continuous delay, one series. Form: step CDF."""
    e = propagation(RUN_FAC)
    total = sum(len(json.load(open(RUN_FAC / "summary.json"))[i]["detail"])
                for i in range(len(json.load(open(RUN_FAC / "summary.json")))))
    fig, ax = plt.subplots(figsize=(7.4, 4.2))
    frame(ax, grid_axis="both")

    y = [100 * (i + 1) / len(e) for i in range(len(e))]
    ax.step(e, y, where="post", color=S1, linewidth=2.0,
            solid_joinstyle="round", solid_capstyle="round")

    for pct, lab in ((50, "median"), (90, "p90")):
        v = e[min(int(pct / 100 * len(e)), len(e) - 1)]
        ax.plot([v], [pct], "o", color=S1, markersize=8,
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
        ax.annotate(f"{lab} {v:.0f} s", (v, pct), textcoords="offset points",
                    xytext=(10, -14), color=INK_2, fontsize=9)

    miss = 100 * (total - len(e)) / total
    ax.annotate(f"{miss:.0f}% of keys were never timed by the poller — they are\n"
                f"absent from this curve, and they are the slow ones,\n"
                f"so the true distribution lies to the right of it",
                xy=(0.985, 0.06), xycoords="axes fraction", ha="right",
                color=MUTED, fontsize=9)

    ax.set_xlim(0, 620)
    ax.set_ylim(0, 101)
    ax.set_xlabel("propagation delay: transmit to first fetchable report (s)")
    ax.set_ylabel("keys observed (cumulative %)")
    ax.set_title("The channel delivers in minutes, with a long tail",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate(f"run E · {len(e)} of {total} keys timed · poller patience 600 s",
                xy=(0, -0.24), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig5-propagation-cdf")


def _comb_data():
    """Run 4: delivered rate keyed by (broadcasts, adv_ms)."""
    g = defaultdict(lambda: [0, 0])
    for r in csv.DictReader(open(RUN_COMB / "resweep.csv")):
        m = re.match(r"s\d+_b(\d)_a(\d+)$", r["cell"])
        if not m:
            continue
        k = (int(m.group(1)), int(m.group(2)))
        g[k][0] += int(r["delivered"])
        g[k][1] += int(r["total"])
    return g


def fig_adv_comb():
    """Job: magnitude across an ordered level, two identities. Form: line+marker."""
    g = _comb_data()
    advs = sorted({k[1] for k in g})
    fig, ax = plt.subplots(figsize=(7.8, 4.3))
    frame(ax)

    for a in advs:                       # locked levels behind the data
        if a % 300 == 0:
            ax.axvspan(a - 34, a + 34, color=GRID, linewidth=0, zorder=0)

    for bc, colour in ((4, S2), (6, S1)):
        y = [100 * g[(bc, a)][0] / g[(bc, a)][1] for a in advs]
        ax.plot(advs, y, "-", color=colour, linewidth=2.0, zorder=2)
        ax.plot(advs, y, "o", color=colour, markersize=8, linestyle="none",
                markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)
        ax.annotate(f"{bc} broadcasts", (advs[-1], y[-1]),
                    textcoords="offset points", xytext=(8, -3), ha="left",
                    color=colour, fontsize=10, fontweight="bold")

    ax.set_xticks(advs)
    ax.set_xticklabels([str(a) for a in advs])
    for lab in ax.get_xticklabels():
        if int(lab.get_text()) % 300 == 0:
            lab.set_color(INK)
            lab.set_fontweight("bold")
    ax.set_ylim(50, 103)
    ax.set_xlim(150, 1520)
    ax.set_xlabel("advertising interval (ms)")
    ax.set_ylabel("deliverability (%)")
    ax.annotate("multiples of 300 ms", xy=(900, 53), ha="center",
                color=INK_2, fontsize=9)
    ax.set_title("Delivery collapses at every advertising interval that is a multiple of 300 ms",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate("run 4 · 3,360 keys · −24 dBm · locked levels 69.3% vs 92.7% elsewhere, p = 0.00005",
                xy=(0, -0.24), xycoords="axes fraction", color=MUTED, fontsize=9)
    ax.legend(handles=[Line2D([], [], color=S2, linewidth=2.4, label="4 broadcasts per key"),
                       Line2D([], [], color=S1, linewidth=2.4, label="6 broadcasts per key")],
              frameon=False, loc="lower right", fontsize=9)
    save(fig, "fig2-adv-comb")


def _run6(bc_filter=None):
    """Run 6 cells as (broadcasts, adv, delivered, total)."""
    out = []
    for r in csv.DictReader(open(RUN_COMB2 / "resweep.csv")):
        m = re.match(r"s\d+_b(\d+)_a(\d+)$", r["cell"])
        if not m:
            continue
        bc, adv = int(m.group(1)), int(m.group(2))
        if bc_filter and bc != bc_filter:
            continue
        out.append((bc, adv, int(r["delivered"]), int(r["total"])))
    return out


def fig_phase_response():
    """Job: magnitude across an ordered level. Form: lollipop, zero base."""
    from math import gcd
    g = defaultdict(lambda: [0, 0])
    lev = defaultdict(set)
    for bc, adv, d, t in _run6(bc_filter=4):
        ph = 300 // gcd(adv, 300)
        g[ph][0] += d; g[ph][1] += t; lev[ph].add(adv)
    phases = sorted(g)
    pct = [100 * g[k][0] / g[k][1] for k in phases]

    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    frame(ax)
    x = range(len(phases))
    ax.vlines(x, 0, pct, color=GRID, linewidth=2.0)
    ax.plot(x, pct, "o", color=S1, markersize=10,
            markeredgecolor=SURFACE, markeredgewidth=2, linestyle="none")
    for i, (k, v) in enumerate(zip(phases, pct)):
        ax.annotate(f"{v:.1f}%", (i, v), textcoords="offset points",
                    xytext=(0, 12), ha="center", color=INK_2, fontsize=10,
                    fontweight="bold")
        ax.annotate("adv " + ", ".join(str(a) for a in sorted(lev[k])) + " ms",
                    (i, 3), ha="center", color=MUTED, fontsize=9)

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{k}" for k in phases])
    ax.set_ylim(0, 100)
    ax.set_xlabel("distinct phases the advertiser visits in the scan cycle,  300 / gcd(adv, 300)")
    ax.set_ylabel("deliverability (%)")
    ax.set_title("Delivery is set by how many scan-cycle phases the advertiser reaches",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate("run 6 · all four levels within one run, at 4 broadcasts per key · "
                "+12.4 points 6-phase over 2-phase, p = 0.0001",
                xy=(0, -0.24), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig3-phase-response")


def fig_escape_curve():
    """Job: two identities across an ordered level. Form: line + marker."""
    g = defaultdict(lambda: [0, 0])
    for bc, adv, d, t in _run6():
        if adv in (500, 600) and bc in (4, 8, 16):
            g[(adv, bc)][0] += d; g[(adv, bc)][1] += t
    bcs = [4, 8, 16]
    x = list(range(len(bcs)))
    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    frame(ax)
    for adv, colour, lab in ((500, S1, "adv 500 ms — 3 phases"),
                             (600, S2, "adv 600 ms — 1 phase (locked)")):
        y = [100 * g[(adv, b)][0] / g[(adv, b)][1] for b in bcs]
        ax.plot(x, y, "-", color=colour, linewidth=2.0)
        ax.plot(x, y, "o", color=colour, markersize=9, linestyle="none",
                markeredgecolor=SURFACE, markeredgewidth=2)
        for i, v in enumerate(y):
            ax.annotate(f"{v:.1f}%", (i, v), textcoords="offset points",
                        xytext=(0, 11 if adv == 500 else -20), ha="center",
                        color=INK_2, fontsize=9)
    ax.set_xticks(x); ax.set_xticklabels([str(b) for b in bcs])
    ax.set_xlim(-0.3, len(bcs) - 0.7)
    ax.set_ylim(35, 108)
    ax.set_xlabel("broadcasts per key")
    ax.set_ylabel("deliverability (%)")
    ax.set_title("More broadcasts do not rescue a locked advertising interval",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate("run 6 · −24 dBm · the control reaches ceiling by 8 broadcasts; "
                "the locked interval is still 22 points short at 16 (p = 0.001)",
                xy=(0, -0.24), xycoords="axes fraction", color=MUTED, fontsize=9)
    ax.legend(handles=[Line2D([], [], color=S1, linewidth=2.4, label="adv 500 ms — 3 phases"),
                       Line2D([], [], color=S2, linewidth=2.4, label="adv 600 ms — 1 phase (locked)")],
              frameon=False, loc="lower right", fontsize=9)
    save(fig, "fig4-escape-curve")


def fig_broadcast_delivery():
    """Job: magnitude across an ordered level, two identities. Form: line+marker."""
    g = defaultdict(lambda: [0, 0])
    for run in RUN_BC:
        for r in csv.DictReader(open(run / "resweep.csv")):
            m = re.match(r"s\d+_p([pn])(\d+)_b(\d+)$", r["cell"])
            if not m:
                continue
            dbm = (1 if m.group(1) == "p" else -1) * int(m.group(2))
            k = (dbm, int(m.group(3)))
            g[k][0] += int(r["delivered"])
            g[k][1] += int(r["total"])
    bcs = sorted({k[1] for k in g})
    x = list(range(len(bcs)))

    fig, ax = plt.subplots(figsize=(7.0, 4.2))
    frame(ax)
    for dbm, colour, lab in ((9, S1, "+9 dBm"), (-24, S2, "−24 dBm")):
        y = [100 * g[(dbm, b)][0] / g[(dbm, b)][1] for b in bcs]
        ax.plot(x, y, "-", color=colour, linewidth=2.0)
        ax.plot(x, y, "o", color=colour, markersize=8, linestyle="none",
                markeredgecolor=SURFACE, markeredgewidth=2)
        # direct-label where the series are furthest apart, not where they converge
        ax.annotate(lab, (x[1], y[1]), textcoords="offset points",
                    xytext=(6, 10 if dbm > 0 else -18), ha="left", color=colour,
                    fontsize=10, fontweight="bold")
        ax.annotate(f"{y[0]:.1f}%", (x[0], y[0]), textcoords="offset points",
                    xytext=(0, 10 if dbm > 0 else -16), ha="center",
                    color=INK_2, fontsize=9)

    ax.set_xticks(x)
    ax.set_xticklabels([str(b) for b in bcs])
    ax.set_xlim(-0.35, len(bcs) - 0.75)
    ax.set_ylim(35, 104)
    ax.set_xlabel("broadcasts per key")
    ax.set_ylabel("deliverability (%)")
    ax.set_title("Broadcast count drives delivery; transmit power sets where it saturates",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate("run 2 · 1,980 keys · dwell pinned at 4,000 ms · +18.1 points per doubling at −24 dBm",
                xy=(0, -0.24), xycoords="axes fraction", color=MUTED, fontsize=9)
    ax.legend(handles=[Line2D([], [], color=S1, linewidth=2.4, label="+9 dBm"),
                       Line2D([], [], color=S2, linewidth=2.4, label="−24 dBm")],
              frameon=False, loc="lower right", fontsize=9)
    save(fig, "fig1-broadcast-delivery")


def fig_factorial():
    """Job: before/after per item across two identities. Form: dumbbell."""
    g = factorial_cells()
    dwells = [4, 8, 16]
    med = {k: st.median([p for c in cells for p in cell_propagation(c)])
           for k, cells in g.items()}

    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    frame(ax, grid_axis="x")
    for i, d in enumerate(dwells):
        lo, hi = med[(d, 20)], med[(d, 5)]
        ax.plot([lo, hi], [i, i], color=GRID, linewidth=2.0, zorder=1)
        ax.plot([hi], [i], "o", color=S2, markersize=9, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.plot([lo], [i], "o", color=S1, markersize=9, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=2)

    ax.set_yticks(range(len(dwells)))
    ax.set_yticklabels([f"{d} s" for d in dwells])
    ax.set_ylim(-0.6, len(dwells) - 0.4)
    ax.set_xlim(0, 240)
    ax.set_xlabel("median propagation delay (s)")
    ax.set_ylabel("dwell per key")
    ax.set_title("More broadcasts per key buy latency, not delivery",
                 color=INK, fontsize=12, pad=12, loc="left")
    handles = [Line2D([], [], marker="o", linestyle="none", color=c, markersize=9,
                      markeredgecolor=SURFACE, markeredgewidth=2, label=l)
               for c, l in ((S2, "5 broadcasts per key"), (S1, "20 broadcasts per key"))]
    leg = ax.legend(handles=handles, frameon=False, loc="lower right", fontsize=9.5)
    for t in leg.get_texts():
        t.set_color(INK_2)
    ax.annotate("run E · deliverability was 100% in five of six conditions and "
                "99.7% in the sixth (1,799/1,800 keys overall)",
                xy=(0, -0.34), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig6-factorial-dumbbell")


def fig_live_vs_offline():
    """Job: before/after per item, one hue two shades. Form: dumbbell.

    Run A's artifacts were not retained; these are the published figures from
    the harness README (§ 'Update-interval throughput sweep').
    """
    dwell = ["6 s", "10 s", "14 s", "18 s"]
    live = [34.7, 37.2, 43.3, 44.8]
    true = [98.2, 99.8, 100.0, 100.0]

    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    frame(ax, grid_axis="x")
    for i, (lo, hi) in enumerate(zip(live, true)):
        ax.plot([lo, hi], [i, i], color=GRID, linewidth=2.0, zorder=1)
        ax.plot([lo], [i], "o", color=BLUE_250, markersize=9, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=2)
        ax.plot([hi], [i], "o", color=S1, markersize=9, zorder=3,
                markeredgecolor=SURFACE, markeredgewidth=2)
    ax.annotate("the live poller's apparent\ntrend runs this way",
                xy=(34.7, 0.06), xytext=(52, 0.48), color=MUTED, fontsize=9,
                ha="center", va="center",
                arrowprops=dict(arrowstyle="->", color=MUTED, linewidth=1.0,
                                connectionstyle="arc3,rad=-0.25"))

    ax.set_yticks(range(len(dwell)))
    ax.set_yticklabels(dwell)
    ax.set_ylim(-0.6, len(dwell) - 0.4)
    ax.set_xlim(25, 104)
    ax.set_xlabel("deliverability (%)")
    ax.set_ylabel("dwell per key")
    ax.set_title("Live polling under-reports delivery — and the bias tracks the variable",
                 color=INK, fontsize=12, pad=34, loc="left")
    handles = [Line2D([], [], marker="o", linestyle="none", color=c, markersize=9,
                      markeredgecolor=SURFACE, markeredgewidth=2, label=l)
               for c, l in ((BLUE_250, "live poller"), (S1, "offline resweep (ground truth)"))]
    # every row's connector spans the plot width, so the legend goes outside it
    leg = ax.legend(handles=handles, frameon=False, ncol=2, fontsize=9.5,
                    loc="lower left", bbox_to_anchor=(0, 1.01))
    for t in leg.get_texts():
        t.set_color(INK_2)
    ax.annotate("run A · 2,400 keys · figures as published in the harness README",
                xy=(0, -0.34), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig8-live-vs-offline")


def fig_paired_diffs():
    """Job: polarity against a baseline. Form: diverging lollipop."""
    g = factorial_cells()
    by_rep = defaultdict(dict)
    for k, cells in g.items():
        for cell in cells:
            rep = int(re.match(r"s(\d+)", cell["name"]).group(1)) // 6
            v = cell_propagation(cell)
            if v:
                by_rep[rep][k] = st.median(v)

    diffs = []
    for rep, d in by_rep.items():
        for dw in (4, 8, 16):
            a, b = d.get((dw, 5)), d.get((dw, 20))
            if a is not None and b is not None:
                diffs.append(a - b)
    diffs.sort()
    pos = sum(1 for x in diffs if x > 0)

    fig, ax = plt.subplots(figsize=(7.4, 3.8))
    frame(ax)
    x = range(len(diffs))
    for i, v in enumerate(diffs):
        ax.vlines(i, 0, v, color=S1 if v > 0 else RED, linewidth=2.4)
    ax.axhline(0, color=AXIS, linewidth=1.0)
    ax.axhline(st.mean(diffs), color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)))
    ax.annotate(f"mean {st.mean(diffs):+.0f} s", (0, st.mean(diffs)),
                textcoords="offset points", xytext=(2, 8), ha="left",
                color=INK_2, fontsize=9)

    ax.set_xticks([])
    ax.set_xlabel(f"{len(diffs)} replicate-matched cell pairs, sorted")
    ax.set_ylabel("median propagation, 5 − 20 broadcasts (s)")
    ax.set_title("Paired within replicate, the broadcast effect survives overnight drift",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate(f"run E · {pos} of {len(diffs)} pairs favour 20 broadcasts "
                f"(above zero) · sign test p = 0.049",
                xy=(0, -0.26), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig7-paired-differences")


if __name__ == "__main__":
    style()
    fig_adv_comb()           # figure 1
    fig_broadcast_delivery() # figure 6
    fig_phase_response()     # figure 7
    fig_escape_curve()       # figure 8
    fig_propagation_cdf()    # figure 2
    fig_factorial()          # figure 3
    fig_paired_diffs()       # figure 4
    fig_live_vs_offline()    # figure 5

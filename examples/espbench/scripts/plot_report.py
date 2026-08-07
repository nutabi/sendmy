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
    """Job: distribution of a continuous delay. Form: step CDF, 2 series."""
    e, c = propagation(RUN_FAC), propagation(RUN_ADV)
    fig, ax = plt.subplots(figsize=(7.4, 4.2))
    frame(ax, grid_axis="both")

    for data, color, label in ((c, S2, "Run C — advertising sweep (censored)"),
                               (e, S1, "Run E — dwell factorial")):
        y = [100 * (i + 1) / len(data) for i in range(len(data))]
        ax.step(data, y, where="post", color=color, linewidth=2.0,
                solid_joinstyle="round", solid_capstyle="round", label=label)

    med = st.median(e)
    ax.plot([med], [50], "o", color=S1, markersize=8,
            markeredgecolor=SURFACE, markeredgewidth=2, zorder=5)
    ax.annotate(f"median {med:.0f} s", (med, 50), textcoords="offset points",
                xytext=(10, -14), color=INK_2, fontsize=9)

    cut = max(c)
    ax.axvline(cut, color=MUTED, linewidth=1.0, linestyle=(0, (4, 3)), zorder=1)
    ax.annotate("run C poller cut-off\n(300 s patience)", (cut, 22),
                textcoords="offset points", xytext=(-8, 0), ha="right",
                color=MUTED, fontsize=9)

    ax.set_xlim(0, 620)
    ax.set_ylim(0, 101)
    ax.set_xlabel("propagation delay: transmit to first fetchable report (s)")
    ax.set_ylabel("keys observed (cumulative %)")
    ax.set_title("The channel delivers in minutes, with a long tail",
                 color=INK, fontsize=12, pad=12, loc="left")
    leg = ax.legend(frameon=False, loc="lower right", fontsize=9.5)
    for t in leg.get_texts():
        t.set_color(INK_2)
    save(fig, "fig2-propagation-cdf")


def fig_advertising():
    """Job: magnitude across an ordered level. Form: lollipop (non-zero base)."""
    g = resweep_by(RUN_ADV, r"a(\d+)$")
    levels = sorted(g)
    pct = [100 * g[k][0] / g[k][1] for k in levels]
    bcasts = [8000 // k for k in levels]

    fig, ax = plt.subplots(figsize=(7.4, 4.0))
    frame(ax)
    x = range(len(levels))
    ax.vlines(x, 98.5, pct, color=GRID, linewidth=2.0)
    ax.plot(x, pct, "o", color=S1, markersize=9,
            markeredgecolor=SURFACE, markeredgewidth=2, linestyle="none")

    for i, p in enumerate(pct):
        if p < 100:
            ax.annotate(f"{p:.2f}%", (i, p), textcoords="offset points",
                        xytext=(0, -18), ha="center", color=INK_2, fontsize=9)

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{k}\n{b}×" for k, b in zip(levels, bcasts)])
    ax.set_ylim(98.4, 100.15)
    ax.set_xlabel("advertising interval (ms)  ·  broadcasts per key")
    ax.set_ylabel("deliverability (%)")
    ax.set_title("Cutting the radio duty cycle 40× costs one point of delivery",
                 color=INK, fontsize=12, pad=12, loc="left")
    ax.annotate("run C · 2,400 keys · dwell fixed at 8 s · y-axis starts at 98.4%",
                xy=(0, -0.30), xycoords="axes fraction", color=MUTED, fontsize=9)
    save(fig, "fig1-advertising-sweep")


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
    save(fig, "fig3-factorial-dumbbell")


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
    save(fig, "fig5-live-vs-offline")


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
    save(fig, "fig4-paired-differences")


if __name__ == "__main__":
    style()
    fig_advertising()        # figure 1
    fig_propagation_cdf()    # figure 2
    fig_factorial()          # figure 3
    fig_paired_diffs()       # figure 4
    fig_live_vs_offline()    # figure 5

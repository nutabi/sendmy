#!/usr/bin/env python3
"""Offline analysis of an espbench run's three time-series.

A run records three append-only series *faithfully* while it happens --
`transmission.csv` (what we put on air and when), `detection.csv` (every relay
observation the poller ever fetched, with its first-fetched stamp and decrypted
location), and `density.csv` (the nearby-finder count over time) -- and defers
*all* interpretation to here. Every delivery metric is a join over those series:

  - deliverability   -- did the detection series contain a mid's expected payload
                        within a chosen window of its transmission (a query-time
                        window, re-derivable -- see `--deliver-window-s`).
  - discovery latency -- first observation timestamp - send time (tx <-> det).
  - propagation latency -- first_fetched_at - observation timestamp (det alone).
  - report volume / redundancy -- detections per delivered key.

Because recording is separated from interpretation, `summary.csv`,
`summary.json` and each `<cell>/result.json` / `<cell>/timeseries.csv` are
*derived artifacts*: this script rebuilds them from the CSVs without re-running
the experiment or touching Apple's servers. `run_matrix.py` calls `analyze_run`
at the end of a run; you can also re-derive by hand over any results directory,
optionally with a different deliverability window:

    python analyze.py results/<run> --deliver-window-s 300

`cells.json` (written by the harness) carries the run's config plus each cell's
config-derived expected `(mid, payload)` sequence -- the canonical window list --
so the analysis is robust to a dropped serial line in `transmission.csv`.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path


# --------------------------------------------------------------------------- #
# Series readers
# --------------------------------------------------------------------------- #

def read_csv(path: Path) -> list[dict]:
    """Read a series CSV into a list of row dicts (empty list if absent)."""
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_cells(path: Path) -> dict:
    """Load cells.json: run config/meta plus every cell's expected sequence."""
    return json.loads(path.read_text())


def _num(value: str | None, cast):
    """Cast a possibly-empty CSV cell to a number, or None when blank."""
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _rec_from_row(row: dict) -> dict:
    """Rebuild one detection record from a detection.csv row.

    Recomputes propagation latency (first_fetched_at - observation timestamp)
    from the stored stamps, so it never depends on live state.
    """
    obs = row["obs_timestamp"]
    ff = _num(row.get("first_fetched_at"), float)
    prop = None
    if ff is not None and obs:
        prop = max(0.0, ff - datetime.fromisoformat(obs).timestamp())
    return {
        "id": row["report_id"],
        "timestamp": obs,
        "first_fetched_at": ff,
        "propagation_latency_s": prop,
        "latitude": _num(row.get("latitude"), float),
        "longitude": _num(row.get("longitude"), float),
        "horizontal_accuracy": _num(row.get("horizontal_accuracy"), float),
        "confidence": _num(row.get("confidence"), int),
        "status": _num(row.get("status"), int),
    }


# --------------------------------------------------------------------------- #
# Per-cell derivation
# --------------------------------------------------------------------------- #

def analyze_cell(cell: dict, tx_rows: list[dict], det_rows: list[dict], *,
                 deliver_window_s: float | None, skew_s: float,
                 polled: bool) -> dict:
    """Derive one cell's statistics by joining its transmission + detection rows.

    `tx_rows` and `det_rows` are already filtered to this cell. The expected
    `(mid, payload)` sequence (config-derived ground truth) drives the window
    list; a detection counts toward a window only if its observation timestamp
    falls in the deliverability window `[send_time - skew, send_time + window]`
    (`deliver_window_s=None` leaves the upper bound open -- "ever delivered").
    """
    expected = [(int(m), int(p)) for m, p in cell["expected"]]
    mode = cell["mode"]

    # When each window went on air (host wall-clock), keyed by mid.
    send_at: dict[int, float] = {}
    for r in tx_rows:
        send_at.setdefault(int(r["mid"]), float(r["send_time"]))

    # Detections grouped by (mid, payload), deduped by report id, chronological.
    by_key: dict[tuple[int, int], dict[str, dict]] = {}
    for r in det_rows:
        key = (int(r["mid"]), int(r["payload"]))
        by_key.setdefault(key, {})[r["report_id"]] = _rec_from_row(r)

    def records_for(mid: int, payload: int, send_time: float | None) -> list[dict]:
        """Chronological detections for a carrier, filtered to the deliver window."""
        recs = sorted(by_key.get((mid, payload), {}).values(),
                      key=lambda rec: rec["timestamp"])
        if send_time is None:
            return recs
        lo = send_time - skew_s
        hi = None if deliver_window_s is None else send_time + deliver_window_s
        out = []
        for rec in recs:
            ts = datetime.fromisoformat(rec["timestamp"]).timestamp()
            if ts < lo or (hi is not None and ts > hi):
                continue
            out.append(rec)
        return out

    windows = []
    delivered = correct = 0
    latencies: list[float] = []
    report_counts: list[int] = []
    propagations: list[float] = []

    for mid, payload_exp in expected:
        floor = send_at.get(mid)
        # Every payload ever seen on this mid (collisions included), windowed.
        payloads_seen = {p for (m, p) in by_key if m == mid}
        reports = {p: records_for(mid, p, floor) for p in payloads_seen}
        reports = {p: recs for p, recs in reports.items() if recs}

        for recs in reports.values():
            for rec in recs:
                if rec["propagation_latency_s"] is not None:
                    propagations.append(rec["propagation_latency_s"])

        present = set(reports)
        is_delivered = bool(present)
        is_correct = payload_exp in present
        delivered += is_delivered
        correct += is_correct

        exp_reports = reports.get(payload_exp, [])
        report_counts.append(len(exp_reports))

        # Discovery latency: first observation of the expected payload minus its
        # send time. Censored (null) when the window was never seen or the send
        # time is unknown -- never guessed.
        latency = observed_at = None
        if exp_reports and floor is not None:
            observed_at = exp_reports[0]["timestamp"]
            latency = max(0.0, datetime.fromisoformat(observed_at).timestamp() - floor)
            latencies.append(latency)

        first_seen = exp_reports[0]["timestamp"] if exp_reports else None
        last_seen = exp_reports[-1]["timestamp"] if exp_reports else None
        span_s = None
        if first_seen and last_seen:
            span_s = (datetime.fromisoformat(last_seen).timestamp()
                      - datetime.fromisoformat(first_seen).timestamp())

        windows.append({
            "mid": mid,
            "payload_expected": payload_exp,
            "delivered": is_delivered,
            "correct": is_correct,
            "present": sorted(present),
            "discovery_latency_s": latency,
            "observed_at": observed_at,
            "report_count": len(exp_reports),
            "first_seen": first_seen,
            "last_seen": last_seen,
            "observation_span_s": span_s,
            "reports": {str(p): recs for p, recs in sorted(reports.items())},
        })

    n = len(expected)
    # Send duration: wall time on air, first tx to a full update interval after
    # the last (matching the firmware's own end-of-run delay).
    tx_times = sorted(send_at.values())
    if tx_times:
        if mode == "static":
            send_seconds = int(cell.get("duration_ms", 0)) / 1000.0
        else:
            send_seconds = ((tx_times[-1] - tx_times[0])
                            + int(cell.get("update_interval_ms", 0)) / 1000.0)
    else:
        send_seconds = float("nan")

    bytes_sent = n
    bytes_delivered = delivered
    lat_n = len(latencies)
    seen_counts = [c for c in report_counts if c > 0]
    reports_total = sum(report_counts)

    return {
        "name": cell["name"],
        "mode": mode,
        "adv_interval_ms": int(cell.get("adv_interval_ms", 0)),
        "update_interval_ms": int(cell.get("update_interval_ms", 0)),
        "tx_power_dbm": cell.get("tx_power_dbm"),
        "mid_base": cell["mid_base"],
        "windows": n,
        "delivered": delivered,
        "correct": correct,
        "deliverability": delivered / n if n else float("nan"),
        "correctness": correct / n if n else float("nan"),
        "bytes_sent": bytes_sent,
        "bytes_delivered": bytes_delivered,
        "send_seconds": send_seconds,
        "throughput_bps_offered": (bytes_sent / send_seconds) if send_seconds else float("nan"),
        "throughput_bps_delivered": (bytes_delivered / send_seconds) if send_seconds else float("nan"),
        "latency_n": lat_n,
        "latency_min_s": min(latencies) if lat_n else None,
        "latency_median_s": statistics.median(latencies) if lat_n else None,
        "latency_mean_s": statistics.fmean(latencies) if lat_n else None,
        "latency_max_s": max(latencies) if lat_n else None,
        # True observation totals (continuous polling) vs a single fetch's capped
        # ~8-report tail. Without polling, treat report counts as a floor.
        "polled": polled,
        "reports_total": reports_total,
        "reports_per_delivered_mean": statistics.fmean(seen_counts) if seen_counts else None,
        "reports_per_delivered_median": statistics.median(seen_counts) if seen_counts else None,
        "reports_per_delivered_max": max(seen_counts) if seen_counts else None,
        "propagation_n": len(propagations),
        "propagation_min_s": min(propagations) if propagations else None,
        "propagation_median_s": statistics.median(propagations) if propagations else None,
        "propagation_mean_s": statistics.fmean(propagations) if propagations else None,
        "propagation_max_s": max(propagations) if propagations else None,
        "deliver_window_s": deliver_window_s,
        "detail": windows,
    }


# --------------------------------------------------------------------------- #
# Output writers
# --------------------------------------------------------------------------- #

SUMMARY_COLS = [
    "name", "mode", "adv_interval_ms", "update_interval_ms", "tx_power_dbm",
    "mid_base", "windows", "delivered", "correct", "deliverability",
    "correctness", "bytes_sent", "bytes_delivered", "send_seconds",
    "throughput_bps_offered", "throughput_bps_delivered",
    "latency_n", "latency_min_s", "latency_median_s", "latency_mean_s",
    "latency_max_s", "polled", "reports_total", "reports_per_delivered_mean",
    "reports_per_delivered_median", "reports_per_delivered_max",
    "propagation_n", "propagation_min_s", "propagation_median_s",
    "propagation_mean_s", "propagation_max_s",
]


def write_summary(results_dir: Path, summary: list[dict]) -> None:
    with (results_dir / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLS)
        writer.writeheader()
        for row in summary:
            writer.writerow({k: row[k] for k in SUMMARY_COLS})
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def write_timeseries(path: Path, result: dict) -> None:
    """Flatten every retained observation to a plottable CSV (one row per report)."""
    cols = ["mid", "payload", "timestamp", "propagation_latency_s", "latitude",
            "longitude", "horizontal_accuracy", "confidence", "status", "id"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for window in result["detail"]:
            mid = window["mid"]
            for payload_str, records in window["reports"].items():
                payload = int(payload_str)
                for rec in records:
                    writer.writerow({"mid": mid, "payload": payload, **rec})


# --------------------------------------------------------------------------- #
# Run-level driver
# --------------------------------------------------------------------------- #

def analyze_run(results_dir: Path, *, deliver_window_s: float | None = None,
                uid_by_cell: dict[str, str] | None = None) -> list[dict]:
    """Join a run's three series into summary + per-cell derived artifacts.

    Reads `cells.json`, `transmission.csv` and `detection.csv` under
    `results_dir`; writes each `<cell>/result.json`, `<cell>/timeseries.csv`, and
    the run-level `summary.csv` / `summary.json`. Returns the summary rows.
    """
    meta = load_cells(results_dir / "cells.json")
    cells = meta["cells"]
    polled = bool(meta.get("poll", False))
    skew_s = float(meta.get("skew_s", 0.0))
    if deliver_window_s is None:
        deliver_window_s = meta.get("deliver_window_s")

    tx_all = read_csv(results_dir / "transmission.csv")
    det_all = read_csv(results_dir / "detection.csv")
    tx_by_cell: dict[str, list[dict]] = {}
    det_by_cell: dict[str, list[dict]] = {}
    for r in tx_all:
        tx_by_cell.setdefault(r["cell"], []).append(r)
    for r in det_all:
        det_by_cell.setdefault(r["cell"], []).append(r)

    summary: list[dict] = []
    for cell in cells:
        name = cell["name"]
        tx_rows = tx_by_cell.get(name, [])
        det_rows = det_by_cell.get(name, [])
        result = analyze_cell(cell, tx_rows, det_rows,
                              deliver_window_s=deliver_window_s,
                              skew_s=skew_s, polled=polled)
        # Recover the cell's uid for the artifact (from the series or an override).
        uid = None
        if uid_by_cell and name in uid_by_cell:
            uid = uid_by_cell[name]
        elif tx_rows:
            uid = tx_rows[0].get("uid")
        elif det_rows:
            uid = det_rows[0].get("uid")
        if uid is not None:
            result["uid"] = uid

        cell_dir = results_dir / name
        cell_dir.mkdir(exist_ok=True)
        (cell_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        write_timeseries(cell_dir / "timeseries.csv", result)
        summary.append(result)

    if summary:
        write_summary(results_dir, summary)
    return summary


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("results_dir", type=Path, help="a run directory under results/")
    p.add_argument("--deliver-window-s", type=float, default=None,
                   help="count a window delivered only if observed within this "
                        "many seconds of its send time (default: no upper bound)")
    args = p.parse_args()

    if not (args.results_dir / "cells.json").exists():
        sys.exit(f"error: {args.results_dir / 'cells.json'} not found "
                 "(is this a run directory?)")
    summary = analyze_run(args.results_dir, deliver_window_s=args.deliver_window_s)
    for row in summary:
        lat = (f"latency median {row['latency_median_s']:.1f}s (n={row['latency_n']})"
               if row["latency_n"] else "latency n/a")
        print(f"{row['name']}: deliverability {row['deliverability']:.1%}, "
              f"correctness {row['correctness']:.1%}, {lat}")
    print(f"\nsummary -> {args.results_dir / 'summary.csv'}")


if __name__ == "__main__":
    main()

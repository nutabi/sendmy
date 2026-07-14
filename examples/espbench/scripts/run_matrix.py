#!/usr/bin/env python3
"""Automate an espbench experiment matrix end to end.

For each cell of the matrix this harness:

  1. writes an sdkconfig fragment for the cell's parameters,
  2. builds and flashes the firmware (idf.py build flash),
  3. resets the board and captures the serial log of the whole run,
  4. cross-checks the serial log against the deterministic sequence the config
     implies (the "both" ground truth),
  5. waits a settle period for the relay to file its reports,
  6. fetches every mid and recovers the delivered payloads,
  7. computes deliverability / correctness / throughput and writes the results.

It must be launched from an ESP-IDF-activated shell (so `idf.py` is on PATH and
the toolchain env is set) with the board connected to this machine. See the
README for the matrix file format.

    scripts/.venv/bin/python scripts/run_matrix.py matrix.json --port /dev/tty.usbmodem1101

Dependencies: findmy, cryptography, pyserial.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import serial  # pyserial
except ImportError:
    sys.exit("error: pyserial is required (pip install pyserial)")

import bench_common as bc

# Line patterns emitted by espbench.c. The ESP log prefix ("I (1234) espbench:")
# precedes each, so we search rather than match.
import re

TX_RE = re.compile(r"tx mid=(\d+) payload=0x([0-9a-fA-F]{2}) t=(\d+)")
DONE_RE = re.compile(r"done count=(\d+) mid_base=(\d+)")


# --------------------------------------------------------------------------- #
# Matrix parsing and mid allocation
# --------------------------------------------------------------------------- #

def resolve_cells(matrix: dict) -> list[dict]:
    """Normalise every cell and assign each a disjoint mid range.

    Cells that do not pin their own mid_base get one allocated after the
    previous cell's range (plus a gap), so no two cells ever advertise the same
    carrier on the shared relay/uid.
    """
    gap = int(matrix.get("mid_gap", 100))
    next_base = int(matrix.get("mid_start", 0))
    poll_default = bool(matrix.get("poll", False))
    poll_iv_default = float(matrix.get("poll_interval_s", 60))
    cells = []

    for i, raw in enumerate(matrix["cells"]):
        cell = dict(raw)
        mode_name = cell["mode"]
        if mode_name not in bc.MODE_NAMES:
            sys.exit(f"error: cell {i}: unknown mode {mode_name!r}")
        cell["mode_int"] = bc.MODE_NAMES[mode_name]
        cell.setdefault("name", f"cell{i:02d}_{mode_name}")
        cell.setdefault("payload_start", 0)
        cell.setdefault("seed", 1)

        if mode_name == "static":
            cell["count"] = 1
            if int(cell.get("duration_ms", 0)) <= 0:
                sys.exit(f"error: cell {cell['name']}: static cells need duration_ms > 0 "
                         "so the harness knows when the run ends")
            span = 1
        else:
            if "count" not in cell or "update_interval_ms" not in cell:
                sys.exit(f"error: cell {cell['name']}: {mode_name} cells need "
                         "count and update_interval_ms")
            span = int(cell["count"])

        if "mid_base" in cell:
            cell["mid_base"] = int(cell["mid_base"])
        else:
            cell["mid_base"] = next_base
            next_base += span + gap

        cell["expected"] = bc.expected_sequence(
            cell["mode_int"], mid_base=cell["mid_base"], count=cell["count"],
            payload_start=int(cell["payload_start"]), seed=int(cell["seed"]))

        cell["poll"] = bool(cell.get("poll", poll_default))
        cell["poll_interval_s"] = float(cell.get("poll_interval_s", poll_iv_default))
        # For non-static cells each key is on air for only one update interval, so
        # a poll interval longer than half of it risks sampling a window just once
        # (no better than a single fetch). Static cells hold one key for the whole
        # run, so any interval accumulates -- no warning there.
        if (cell["poll"] and mode_name != "static"
                and cell["poll_interval_s"] > int(cell["update_interval_ms"]) / 2000):
            print(f"warning: cell {cell['name']}: poll_interval_s="
                  f"{cell['poll_interval_s']:.0f} exceeds half the update interval "
                  f"({int(cell['update_interval_ms']) / 2000:.0f}s); windows may be "
                  "polled only once", file=sys.stderr)

        cells.append(cell)

    return cells


# --------------------------------------------------------------------------- #
# Build / flash
# --------------------------------------------------------------------------- #

def new_uid_hex(project_dir: Path) -> bytes:
    """Mint a fresh 32-byte UID and write it to the project's uid.hex.

    Each cell gets its own random UID so its carriers are fully independent of
    every other cell (and of any earlier run): even identical (mid, payload)
    pairs derive different carriers under different UIDs, so nothing collides on
    the shared relay. The build bakes uid.hex into the NVS image, so this must
    run before build_and_flash. The harness owns uid.hex while it runs; the last
    cell's UID is left in place on exit.
    """
    uid = os.urandom(32)
    path = project_dir / "uid.hex"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(uid.hex() + "\n")
    return uid


def write_fragment(cell: dict, path: Path) -> None:
    """Emit the sdkconfig fragment that pins this cell's parameters."""
    mode_sym = {
        bc.MODE_STATIC: "CONFIG_ESPBENCH_MODE_STATIC",
        bc.MODE_INCREMENTAL: "CONFIG_ESPBENCH_MODE_INCREMENTAL",
        bc.MODE_RANDOM: "CONFIG_ESPBENCH_MODE_RANDOM",
    }[cell["mode_int"]]

    lines = [
        f"{mode_sym}=y",
        f"CONFIG_ESPBENCH_ADV_INTERVAL_MS={int(cell['adv_interval_ms'])}",
        f"CONFIG_ESPBENCH_MID_BASE={cell['mid_base']}",
        f"CONFIG_ESPBENCH_PAYLOAD_START={int(cell['payload_start'])}",
    ]
    if cell["mode_int"] == bc.MODE_STATIC:
        lines.append(f"CONFIG_ESPBENCH_STATIC_DURATION_MS={int(cell['duration_ms'])}")
    else:
        lines.append(f"CONFIG_ESPBENCH_UPDATE_INTERVAL_MS={int(cell['update_interval_ms'])}")
        lines.append(f"CONFIG_ESPBENCH_MSG_COUNT={int(cell['count'])}")
    if cell["mode_int"] == bc.MODE_RANDOM:
        lines.append(f"CONFIG_ESPBENCH_RANDOM_SEED=0x{int(cell['seed']):x}")

    path.write_text("\n".join(lines) + "\n")


def build_and_flash(project_dir: Path, build_dir: Path, fragment: Path,
                    port: str, idf_py: str) -> None:
    """Rebuild for the current fragment and flash the board.

    Deleting the build's sdkconfig forces IDF to regenerate it from
    sdkconfig.defaults plus our fragment, so each cell's parameters actually take
    effect. Object files are cached, so only config-dependent sources recompile.
    """
    sdkconfig = build_dir / "sdkconfig"
    if sdkconfig.exists():
        sdkconfig.unlink()

    defaults = f"{project_dir / 'sdkconfig.defaults'};{fragment}"
    cmd = [
        idf_py,
        "-C", str(project_dir),
        "-B", str(build_dir),
        "-p", port,
        "-D", f"SDKCONFIG={sdkconfig}",
        "-D", f"SDKCONFIG_DEFAULTS={defaults}",
        "build", "flash",
    ]
    print(f"  building & flashing ({fragment.name}) ...", flush=True)
    # Capture the idf.py build/flash output and surface it only on failure, so
    # normal runs show test output + errors without the ninja build spam.
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    if proc.returncode != 0:
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
        raise subprocess.CalledProcessError(proc.returncode, cmd, proc.stdout)


# --------------------------------------------------------------------------- #
# Serial capture
# --------------------------------------------------------------------------- #

def capture_run(port: str, baud: int, timeout_s: float,
                on_tx=None) -> tuple[list[dict], list[str]]:
    """Reset the board and record its serial log until the done marker.

    Returns (observed, raw_lines) where observed is a list of
    {mid, payload, device_ms, host_time} dicts for each tx line seen.

    `on_tx(mid, host_time)`, if given, is called the instant each window goes on
    air, so a concurrent poller can track which mid is currently advertising.
    """
    observed: list[dict] = []
    raw: list[str] = []

    with serial.Serial(port, baud, timeout=1) as ser:
        # esp32 auto-reset: pulse EN (RTS) to restart from boot so we catch the
        # whole run, including the "run start" banner.
        ser.setDTR(False)
        ser.setRTS(True)
        time.sleep(0.1)
        ser.setRTS(False)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            line = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            raw.append(line)
            m = TX_RE.search(line)
            if m:
                mid = int(m.group(1))
                host_time = time.time()
                observed.append({
                    "mid": mid,
                    "payload": int(m.group(2), 16),
                    "device_ms": int(m.group(3)),
                    "host_time": host_time,
                })
                if on_tx is not None:
                    on_tx(mid, host_time)
                print(f"    tx mid={m.group(1)} payload=0x{m.group(2)}", flush=True)
                continue
            if DONE_RE.search(line):
                print("    run done", flush=True)
                return observed, raw

    print("    warning: capture timed out before the done marker", file=sys.stderr)
    return observed, raw


def reconcile(cell: dict, observed: list[dict]) -> list[str]:
    """Compare the serial log against the deterministic expected sequence."""
    warnings: list[str] = []
    exp = cell["expected"]
    obs_pairs = [(o["mid"], o["payload"]) for o in observed]

    if obs_pairs != exp:
        warnings.append(
            f"serial log ({len(obs_pairs)} windows) disagrees with the config-derived "
            f"sequence ({len(exp)} windows); trusting config for fetch")
        exp_set = set(exp)
        for pair in obs_pairs:
            if pair not in exp_set:
                warnings.append(f"  observed unexpected window mid={pair[0]} payload=0x{pair[1]:02x}")
    return warnings


# --------------------------------------------------------------------------- #
# Polling (beat Apple's ~8-most-recent-per-key cap)
# --------------------------------------------------------------------------- #
#
# A single fetch returns at most ~8 recent reports per key -- a hard server-side
# cap (fetch_raw_reports already asks for the full 7-day window). So a key that
# is observed many times, especially a long static soak, only ever surfaces its
# most recent tail. To recover the real time-series we fetch repeatedly *during*
# the cell and union the results by report id.
#
# Only the poller talks to Apple while a cell runs (serial capture does no
# network I/O), so there is never concurrent access to the findmy account.

class ReportStore:
    """Thread-safe union of report records across polls, keyed by (mid, payload)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[int, int], dict[str, dict]] = {}

    def merge(self, mid: int, reports: dict[int, list[dict]]) -> None:
        """Fold one fetch's {payload: [records]} into the accumulated union.

        The first fetch that returns a given report id stamps it with
        `first_fetched_at` (host wall-clock); later re-fetches of the same id keep
        that original stamp. That first-seen time, minus the report's own
        (observation) timestamp, is the server-ingestion / propagation latency.
        """
        now = time.time()
        with self._lock:
            for payload, records in reports.items():
                bucket = self._data.setdefault((mid, payload), {})
                for rec in records:
                    prev = bucket.get(rec["id"])
                    if prev is not None and "first_fetched_at" in prev:
                        rec["first_fetched_at"] = prev["first_fetched_at"]
                    else:
                        rec.setdefault("first_fetched_at", now)
                    bucket[rec["id"]] = rec  # de-dup: same observation overwrites

    def window(self, mid: int) -> dict[int, list[dict]]:
        """Return {payload: [records]} for a mid, each list in chronological order."""
        with self._lock:
            out: dict[int, list[dict]] = {}
            for (m, payload), bucket in self._data.items():
                if m == mid:
                    out[payload] = sorted(bucket.values(), key=lambda r: r["timestamp"])
            return out

    def total(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._data.values())


class Poller(threading.Thread):
    """Background thread that unions reports for the currently-active mid(s).

    The firmware advertises one key at a time, so at any instant only the active
    window's key (plus a couple just-retired ones still settling) is producing
    reports. Polling just those keeps each poll to a request or two, not a sweep
    of every mid. `get_active()` returns (mids, floors) from the serial thread.
    """

    def __init__(self, account, uid: bytes, store: ReportStore, *,
                 interval_s: float, skew_s: float, get_active, stop_event) -> None:
        super().__init__(daemon=True)
        self._account = account
        self._uid = uid
        self._store = store
        self._interval = max(1.0, interval_s)
        self._skew = skew_s
        self._get_active = get_active
        self._stop = stop_event

    def poll_once(self) -> None:
        mids, floors = self._get_active()
        for mid in mids:
            try:
                reports = bc.fetch_reports_for_mid(
                    self._account, self._uid, mid,
                    since_epoch=floors.get(mid), skew_s=self._skew)
                self._store.merge(mid, reports)
            except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the run
                print(f"    poll: mid={mid} fetch failed: {exc!r}", file=sys.stderr, flush=True)

    def run(self) -> None:
        # wait() returns True the moment we are asked to stop, ending the loop.
        while not self._stop.wait(self._interval):
            self.poll_once()


# --------------------------------------------------------------------------- #
# Fetch + stats
# --------------------------------------------------------------------------- #

def evaluate(cell: dict, observed: list[dict], account, uid: bytes, *,
             skew_s: float,
             store: ReportStore | None = None, verbose: bool = True) -> dict:
    """Compute the cell's statistics, incl. discovery latency and report volume.

    When `store` is given (poll mode), each window's reports come from the
    accumulated union built during the run, so counts are the true observation
    totals rather than a single fetch's ~8-report tail. Otherwise every mid is
    fetched once here (the classic single-shot path). `verbose` prints one line
    per window; the post-run re-sweep re-evaluates quietly.
    """
    exp = cell["expected"]
    # When each window went on air (host wall-clock), keyed by mid. Doubles as the
    # staleness floor that rejects reports left by an earlier run on the same mid.
    send_at = {o["mid"]: o["host_time"] for o in observed}
    windows = []
    delivered = correct = 0
    latencies: list[float] = []
    report_counts: list[int] = []  # reports for the expected payload, per window
    # Propagation (ingestion) latency: first time we could *fetch* a report minus
    # the report's own observation timestamp. Server-pipeline delay, distinct from
    # discovery latency (tx -> observed). Only defined in poll mode.
    propagations: list[float] = []

    for mid, payload_exp in exp:
        floor = send_at.get(mid)
        if store is not None:
            reports = store.window(mid)
        else:
            reports = bc.fetch_reports_for_mid(account, uid, mid, since_epoch=floor, skew_s=skew_s)
        for recs in reports.values():
            for rec in recs:
                ff = rec.get("first_fetched_at")
                if ff is not None:
                    prop = max(0.0, ff - datetime.fromisoformat(rec["timestamp"]).timestamp())
                    rec["propagation_latency_s"] = prop
                    propagations.append(prop)
        present = set(reports)
        is_delivered = bool(present)
        is_correct = payload_exp in present
        delivered += is_delivered
        correct += is_correct

        exp_reports = reports.get(payload_exp, [])
        report_counts.append(len(exp_reports))

        # Discovery latency: first observation minus when we put it on air. Only
        # defined when the correct payload came back and we know its send time;
        # otherwise it is censored (null), never guessed. Records are already in
        # chronological order, so the first one is the earliest observation.
        latency = None
        observed_at = None
        if exp_reports and floor is not None:
            observed_at = exp_reports[0]["timestamp"]
            latency = max(0.0, datetime.fromisoformat(observed_at).timestamp() - floor)
            latencies.append(latency)

        # Observation span: first to last time this key was seen. With polling
        # this is the real on-air coverage window (esp. for the static soak);
        # with a single fetch it is just the spread of the ~8-report tail.
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
            # Full per-observation history for every payload seen on this mid
            # (keys stringified for JSON); includes collisions, if any.
            "reports": {str(p): recs for p, recs in sorted(reports.items())},
        })
        if verbose:
            status = "correct" if is_correct else ("delivered" if is_delivered else "LOST")
            lat_str = f" latency={latency:.1f}s" if latency is not None else ""
            cnt_str = f" reports={len(exp_reports)}" if exp_reports else ""
            print(f"    mid={mid} expect=0x{payload_exp:02x} -> {status} "
                  f"present={[f'0x{p:02x}' for p in sorted(present)]}{lat_str}{cnt_str}", flush=True)

    n = len(exp)
    # Send duration: wall time on air, from the first tx to a full update
    # interval after the last (matching the firmware's own end-of-run delay).
    if observed:
        first = observed[0]["host_time"]
        last = observed[-1]["host_time"]
        if cell["mode_int"] == bc.MODE_STATIC:
            send_seconds = int(cell["duration_ms"]) / 1000.0
        else:
            send_seconds = (last - first) + int(cell["update_interval_ms"]) / 1000.0
    else:
        send_seconds = float("nan")

    bytes_sent = n  # one octet per window
    bytes_delivered = delivered

    # Discovery-latency aggregates over the windows that were actually seen; lost
    # windows are censored out (see the null latency above), not counted as slow.
    lat_n = len(latencies)
    # Report-volume aggregates: how many times each delivered window was actually
    # observed by the finder network (a coverage/density signal, distinct from
    # whether it was delivered at all). Lost windows are excluded from the ratios.
    seen_counts = [c for c in report_counts if c > 0]
    reports_total = sum(report_counts)
    return {
        "name": cell["name"],
        "mode": cell["mode"],
        "adv_interval_ms": int(cell["adv_interval_ms"]),
        "update_interval_ms": int(cell.get("update_interval_ms", 0)),
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
        # Whether report volume is a true total (poll mode) or a single fetch's
        # capped ~8-report tail. Without polling, treat the counts as a floor.
        "polled": store is not None,
        "reports_total": reports_total,
        "reports_per_delivered_mean": statistics.fmean(seen_counts) if seen_counts else None,
        "reports_per_delivered_median": statistics.median(seen_counts) if seen_counts else None,
        "reports_per_delivered_max": max(seen_counts) if seen_counts else None,
        # Propagation (observed -> queryable) latency across every retained report.
        "propagation_n": len(propagations),
        "propagation_min_s": min(propagations) if propagations else None,
        "propagation_median_s": statistics.median(propagations) if propagations else None,
        "propagation_mean_s": statistics.fmean(propagations) if propagations else None,
        "propagation_max_s": max(propagations) if propagations else None,
        "detail": windows,
    }


def estimate_timeout(cell: dict) -> float:
    """How long to wait for the run's done marker, with boot + slack margin."""
    boot_margin = 20.0
    if cell["mode_int"] == bc.MODE_STATIC:
        run_s = int(cell["duration_ms"]) / 1000.0
    else:
        # count windows plus the firmware's trailing update-interval delay.
        run_s = (int(cell["count"]) + 1) * int(cell["update_interval_ms"]) / 1000.0
    return run_s + boot_margin + 30.0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("matrix", type=Path, help="matrix JSON file")
    parser.add_argument("--port", help="serial port (overrides matrix 'port')")
    parser.add_argument("--baud", type=int, default=115200, help="serial baud (default 115200)")
    parser.add_argument("--no-flash", action="store_true",
                        help="skip build/flash; capture a run already flashed on the board")
    parser.add_argument("--no-fetch", action="store_true",
                        help="capture the run but skip the relay fetch/stats stage")
    parser.add_argument("--no-poll", action="store_true",
                        help="disable in-run polling even if the matrix enables it "
                             "(fall back to a single fetch per mid)")
    parser.add_argument("--no-density", action="store_true",
                        help="skip the background Apple-device density logger")
    parser.add_argument("--no-resweep", action="store_true",
                        help="skip the post-run re-sweep for late/propagating reports")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved cells and mid allocation, then exit")
    args = parser.parse_args()

    matrix = json.loads(args.matrix.read_text())
    cells = resolve_cells(matrix)

    project_dir = Path(matrix.get("project_dir", bc.PROJECT_ROOT)).resolve()
    build_dir = Path(matrix.get("build_dir", project_dir / "build")).resolve()
    idf_py = matrix.get("idf_py", "idf.py")
    port = args.port or matrix.get("port")
    settle_s = float(matrix.get("settle_seconds", 120))
    skew_s = float(matrix.get("report_floor_skew_s", 120))

    if args.dry_run:
        for c in cells:
            print(f"{c['name']}: mode={c['mode']} adv={c['adv_interval_ms']}ms "
                  f"mid_base={c['mid_base']} windows={len(c['expected'])}")
        return

    if not port:
        sys.exit("error: no serial port (pass --port or set 'port' in the matrix)")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_dir = Path(matrix.get("results_dir", bc.SCRIPT_DIR / "results")) / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"results -> {results_dir}")

    # In --no-flash mode we cannot re-provision the board, so every cell shares
    # whatever UID is already flashed (the project uid.hex). When flashing, each
    # cell mints its own UID just before its build (below).
    shared_uid = bc.read_uid() if args.no_flash else None
    account = None if args.no_fetch else bc.load_account()

    # Background Apple-device density logger. Samples the nearby finder count for
    # the whole run into density.csv, so deliverability can later be regressed on
    # the actual local density (see scan_density.py). Its own stdout goes to
    # density.log to keep this console to test output + errors.
    density_proc = density_log = None
    if matrix.get("density", True) and not args.no_density and not args.dry_run:
        d_interval = matrix.get("density_interval_s", 60)
        dcmd = [sys.executable, str(bc.SCRIPT_DIR / "scan_density.py"), "--watch",
                "--interval", str(d_interval),
                "--window", str(matrix.get("density_window_s", 10)),
                "--out", str(results_dir / "density.csv")]
        rssi_min = matrix.get("density_rssi_min")
        if rssi_min is not None:
            dcmd += ["--rssi-min", str(int(rssi_min))]
        density_log = (results_dir / "density.log").open("w")
        density_proc = subprocess.Popen(dcmd, stdout=density_log,
                                        stderr=subprocess.STDOUT)
        print(f"density -> {results_dir / 'density.csv'} (every {d_interval}s)")

    # Per-cell state kept for the post-run re-sweep (poll mode only): the report
    # store to fold late reports into, plus what evaluate needs to recompute.
    resweep_ctx: list[dict] = []

    def process_cell(cell: dict) -> dict | None:
        """Flash, capture, settle, and evaluate one cell.

        Returns the cell's result dict, or None in --no-fetch mode. Raises on any
        failure so the caller can defer the cell to the retry pass.
        """
        cell_dir = results_dir / cell["name"]
        cell_dir.mkdir(exist_ok=True)

        if not args.no_flash:
            uid = new_uid_hex(project_dir)
            # Record this cell's UID so its mids can be re-fetched later (secret;
            # results/ is gitignored). A retry mints a fresh UID and reflashes.
            (cell_dir / "uid.hex").write_text(uid.hex() + "\n")
            build_dir.mkdir(parents=True, exist_ok=True)
            fragment = build_dir / "bench_cell.conf"
            write_fragment(cell, fragment)
            build_and_flash(project_dir, build_dir, fragment, port, idf_py)
        else:
            uid = shared_uid

        # In poll mode a background thread unions reports for the active mid(s)
        # throughout the run, beating Apple's ~8-report cap. The serial thread
        # only publishes which mid is on air; the poller does all the fetching.
        poll = cell["poll"] and not args.no_fetch and not args.no_poll
        store = None
        poller = None
        stop_event = None
        if poll:
            store = ReportStore()
            active = {"mids": [], "floor": {}}
            active_lock = threading.Lock()
            stop_event = threading.Event()

            def on_tx(mid: int, host_time: float) -> None:
                with active_lock:
                    active["floor"][mid] = host_time
                    active["mids"].append(mid)

            def get_active():
                with active_lock:
                    # the current key plus the one just retired (still settling)
                    recent = list(dict.fromkeys(active["mids"][-2:]))
                    return recent, dict(active["floor"])

            poller = Poller(account, uid, store, interval_s=cell["poll_interval_s"],
                            skew_s=skew_s, get_active=get_active, stop_event=stop_event)
            print(f"  polling every {cell['poll_interval_s']:.0f}s during the run",
                  flush=True)
            poller.start()

        on_tx_cb = on_tx if poll else None
        observed, raw = capture_run(port, args.baud, estimate_timeout(cell), on_tx=on_tx_cb)
        (cell_dir / "serial.log").write_text("\n".join(raw) + "\n")

        for w in reconcile(cell, observed):
            print(f"  warning: {w}", file=sys.stderr)

        if args.no_fetch:
            if poller is not None:
                stop_event.set()
                poller.join(timeout=30)
            return None

        # Keep polling through the settle window -- the last key lingers on air and
        # reports keep landing -- then stop and do one final full sweep so every
        # window is represented at least at single-fetch quality.
        print(f"  settling {settle_s:.0f}s for relay reports ...", flush=True)
        time.sleep(settle_s)
        if poller is not None:
            stop_event.set()
            poller.join(timeout=30)
            print("  final sweep of all mids ...", flush=True)
            send_at = {o["mid"]: o["host_time"] for o in observed}
            for mid, _ in cell["expected"]:
                try:
                    reports = bc.fetch_reports_for_mid(
                        account, uid, mid, since_epoch=send_at.get(mid), skew_s=skew_s)
                    store.merge(mid, reports)
                except Exception as exc:  # noqa: BLE001
                    print(f"    final sweep: mid={mid} failed: {exc!r}",
                          file=sys.stderr, flush=True)

        result = evaluate(cell, observed, account, uid,
                          skew_s=skew_s, store=store)
        result["uid"] = uid.hex()
        (cell_dir / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        write_timeseries(cell_dir / "timeseries.csv", result)
        # A successful (re)run supersedes any earlier failure marker.
        (cell_dir / "error.txt").unlink(missing_ok=True)
        lat = (f"latency median {result['latency_median_s']:.1f}s (n={result['latency_n']})"
               if result["latency_n"] else "latency n/a")
        if result["reports_per_delivered_median"] is not None:
            tag = "" if result["polled"] else " (capped ~8/fetch; poll for true counts)"
            rpd = (f", {result['reports_per_delivered_median']:.0f} reports/window median "
                   f"({result['reports_total']} total){tag}")
        else:
            rpd = ""
        print(f"  => deliverability {result['deliverability']:.1%}, "
              f"correctness {result['correctness']:.1%}, "
              f"{result['throughput_bps_delivered']:.4f} B/s delivered, {lat}{rpd}", flush=True)
        if store is not None:
            resweep_ctx.append({"cell": cell, "uid": uid, "observed": observed,
                                "store": store, "cell_dir": cell_dir})
        return result

    summary: list[dict] = []
    deferred: list[dict] = []

    try:
        # First pass: run every cell. A cell that fails (a transient flash/USB
        # glitch or a network blip during fetch) is deferred, not abandoned, so an
        # unattended run keeps going and comes back to it at the end.
        for cell in cells:
            print(f"\n=== {cell['name']} (mid_base={cell['mid_base']}, "
                  f"{len(cell['expected'])} windows) ===", flush=True)
            try:
                result = process_cell(cell)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the run alive
                print(f"  ERROR: cell {cell['name']} failed: {exc!r}; deferring to a "
                      "retry pass after the other cells", file=sys.stderr, flush=True)
                deferred.append(cell)
                continue
            if result is not None:
                summary.append(result)

        # Retry pass: return to each deferred cell once, now that everything else
        # is done. A second failure is recorded and given up on.
        for cell in deferred:
            print(f"\n=== RETRY {cell['name']} (deferred after an earlier failure) ===",
                  flush=True)
            try:
                result = process_cell(cell)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR: cell {cell['name']} failed again: {exc!r}; giving up",
                      file=sys.stderr, flush=True)
                (results_dir / cell["name"] / "error.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n")
                continue
            if result is not None:
                summary.append(result)

        # Post-run re-sweep: ingestion lag can exceed a cell's settle window, so a
        # report that a finder relayed shows up as LOST at capture time and only
        # becomes queryable minutes later (exactly what the smoke test hit). Re-fetch
        # every polled cell's mids at geometric delays, folding late reports into
        # each store; `first_fetched_at` then gives their propagation latency. Retired
        # keys are off-air by now, so these sweeps are clean (no 8-cap tail-shift).
        resweep = matrix.get("resweep_minutes", [1, 3, 6, 10])
        if resweep_ctx and resweep and not args.no_resweep:
            print(f"\n=== post-run re-sweep at {resweep} min "
                  f"({len(resweep_ctx)} cells) ===", flush=True)
            prev = 0.0
            for i, mark in enumerate(resweep, 1):
                wait = max(0.0, (float(mark) - prev) * 60.0)
                prev = float(mark)
                print(f"  re-sweep {i}/{len(resweep)}: waiting {wait:.0f}s ...", flush=True)
                time.sleep(wait)
                new_ids = 0
                for ctx in resweep_ctx:
                    send_at = {o["mid"]: o["host_time"] for o in ctx["observed"]}
                    before = ctx["store"].total()
                    for mid, _ in ctx["cell"]["expected"]:
                        try:
                            reports = bc.fetch_reports_for_mid(
                                account, ctx["uid"], mid,
                                since_epoch=send_at.get(mid), skew_s=skew_s)
                            ctx["store"].merge(mid, reports)
                        except Exception as exc:  # noqa: BLE001
                            print(f"    re-sweep: {ctx['cell']['name']} mid={mid} "
                                  f"failed: {exc!r}", file=sys.stderr, flush=True)
                    new_ids += ctx["store"].total() - before
                print(f"    +{new_ids} new report(s) this sweep", flush=True)
            # Re-evaluate every re-swept cell with the enriched store and rewrite.
            idx_by_name = {r["name"]: i for i, r in enumerate(summary)}
            for ctx in resweep_ctx:
                result = evaluate(ctx["cell"], ctx["observed"], account, ctx["uid"],
                                  skew_s=skew_s, store=ctx["store"], verbose=False)
                result["uid"] = ctx["uid"].hex()
                (ctx["cell_dir"] / "result.json").write_text(json.dumps(result, indent=2) + "\n")
                write_timeseries(ctx["cell_dir"] / "timeseries.csv", result)
                if result["name"] in idx_by_name:
                    summary[idx_by_name[result["name"]]] = result
            print("  re-sweep complete; deliverability + propagation updated",
                  flush=True)
    finally:
        if density_proc is not None:
            density_proc.terminate()
            try:
                density_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                density_proc.kill()
            density_log.close()

    if summary:
        # Keep summary rows in matrix order even when some cells came via retry.
        order = {c["name"]: i for i, c in enumerate(cells)}
        summary.sort(key=lambda r: order.get(r["name"], len(cells)))
        write_summary(results_dir, summary)
        print(f"\nsummary -> {results_dir / 'summary.csv'}")
    if deferred:
        names = ", ".join(c["name"] for c in deferred)
        print(f"note: {len(deferred)} cell(s) needed a retry pass: {names}",
              file=sys.stderr)


def write_summary(results_dir: Path, summary: list[dict]) -> None:
    cols = ["name", "mode", "adv_interval_ms", "update_interval_ms",
            "mid_base", "windows", "delivered", "correct", "deliverability",
            "correctness", "bytes_sent", "bytes_delivered", "send_seconds",
            "throughput_bps_offered", "throughput_bps_delivered",
            "latency_n", "latency_min_s", "latency_median_s", "latency_mean_s",
            "latency_max_s", "polled", "reports_total", "reports_per_delivered_mean",
            "reports_per_delivered_median", "reports_per_delivered_max",
            "propagation_n", "propagation_min_s", "propagation_median_s",
            "propagation_mean_s", "propagation_max_s"]
    with (results_dir / "summary.csv").open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        for row in summary:
            writer.writerow({k: row[k] for k in cols})
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def write_timeseries(path: Path, result: dict) -> None:
    """Flatten every retained observation to a plottable CSV.

    One row per report across all windows: the raw time-series that polling makes
    meaningful (most valuable for the static soak). Encrypted-only reports still
    contribute their timestamp; location columns are blank for those.
    """
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


if __name__ == "__main__":
    main()

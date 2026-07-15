#!/usr/bin/env python3
"""Automate an espbench experiment matrix end to end.

The harness records three *independent, append-only time-series* during a run and
defers all interpretation to an offline join (see analyze.py):

  1. transmission -- one row per key put on air (uid, mid, payload, send_time),
     straight off the serial `tx` line;
  2. detection -- one row per (key, report) a single continuous poller ever sees
     (uid, mid, payload, report_id, obs_timestamp, first_fetched_at, decrypted
     lat/lon/accuracy/confidence/status), deduped by report id;
  3. density -- the nearby-finder count over time (scan_density.py -> density.csv).

The board is flashed **once** with a generic, reconfigurable firmware; each cell's
parameters (mode, intervals, mid range, and a fresh per-cell UID) are then pushed
to the running device over serial as a `run key=val ...` command, and the device
replies with the same `tx`/`done` log lines as before. Per cell it: mints a fresh
UID, sends the run command, captures the serial log until the done marker, and
cross-checks the log against the config-derived sequence. Enqueuing each key into
the poller the instant it starts transmitting -- and polling every not-yet-done key
each cycle for the whole run -- is what makes propagation latency real: an early
cell's reports keep being fetched while later cells run, so a report is first-seen
when it truly becomes queryable, not at some end-of-run sweep.

Deliverability / latency / redundancy are NOT computed live; they are derived at
the end (and re-derivable any time) by analyze.py joining the three series. So
summary.csv / result.json are derived artifacts.

It must be launched from an ESP-IDF-activated shell (so `idf.py` is on PATH and
the toolchain env is set) with the board connected. See the README for the matrix
file format.

    scripts/.venv/bin/python run_matrix.py matrix.smoke.json --port /dev/tty.usbmodem1101

Run the merged-in offline unit tests (no hardware/network) with `--test`, or
`python -m pytest run_matrix.py`.

Dependencies: findmy, cryptography, pyserial.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
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

# The low-level helpers stay in scripts/; this runner now lives at the espbench
# root, so put scripts/ on the path before importing them.
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

import analyze
import bench_common as bc

# Line patterns emitted by espbench.c. The ESP log prefix ("I (1234) espbench:")
# precedes each, so we search rather than match.
TX_RE = re.compile(r"tx mid=(\d+) payload=0x([0-9a-fA-F]{2}) t=(\d+)")
DONE_RE = re.compile(r"done count=(\d+) mid_base=(\d+)")
# Printed once by the firmware after NimBLE sync, when it is ready to accept
# `run` commands. The host waits for this before sending the first cell.
READY_RE = re.compile(r"espbench: ready")


# --------------------------------------------------------------------------- #
# Config: producer-grouped blocks with backward-compatible flat fallbacks
# --------------------------------------------------------------------------- #

def detection_config(matrix: dict) -> dict:
    """Resolve the detection (poll) producer's config.

    Grouped under a `detection` block; falls back to the old flat top-level keys
    so existing matrices keep working. `poll_interval_s` stays coarse (~30 s,
    under the ~60 s relay cadence) to protect the single account's rate limit.
    """
    block = matrix.get("detection", {})

    def pick(key, default):
        return block.get(key, matrix.get(key, default))

    return {
        "poll": bool(pick("poll", False)),
        "poll_interval_s": float(pick("poll_interval_s", 30.0)),
        # Detections to log per key before dropping it from the poll queue;
        # <= 0 means unbounded (for a static-soak time-series). Propagation is
        # captured on the first detection regardless.
        "detections_before_remove": int(pick("detections_before_remove", 1)),
        # Base patience for an *unseen* key before we stop spending fetches on it.
        # This is a polling-efficiency knob, NOT a deliverability verdict.
        "lost_timeout_s": float(pick("lost_timeout_s", 300.0)),
        # Queue size at/under which the full timeout applies; above it the
        # effective timeout shrinks to bound the fetch rate (see adaptive_timeout).
        "queue_soft_cap": int(pick("queue_soft_cap", 8)),
    }


def density_config(matrix: dict) -> dict:
    """Resolve the density producer's config (grouped `density` block or flat)."""
    block = matrix.get("density")
    # Historically `density` was a bool toggle; keep that meaning when it is one.
    if isinstance(block, bool):
        enabled, block = block, {}
    elif isinstance(block, dict):
        enabled = bool(block.get("enabled", True))
    else:
        enabled = True
        block = {}

    def pick(key, flat_key, default):
        return block.get(key, matrix.get(flat_key, default))

    return {
        "enabled": enabled,
        "interval_s": float(pick("interval_s", "density_interval_s", 60)),
        "window_s": float(pick("window_s", "density_window_s", 10)),
        "rssi_min": pick("rssi_min", "density_rssi_min", None),
    }


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
        # TX power is optional; a cell may pin its own, else inherit a
        # matrix-wide default, else None (leave the controller default alone).
        cell.setdefault("tx_power_dbm", matrix.get("tx_power_dbm"))

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

        cells.append(cell)

    return cells


def cells_manifest(cells: list[dict], det_cfg: dict, skew_s: float,
                   deliver_window_s: float | None, run_id: str) -> dict:
    """Build the cells.json manifest analyze.py joins against.

    Carries the run's config plus each cell's config-derived expected sequence
    (the canonical window list) and metadata -- but no uid or GPS, so it is a
    pure schedule of what should have been sent. The uid lives in the (secret)
    transmission series instead.
    """
    return {
        "run_id": run_id,
        "poll": det_cfg["poll"],
        "poll_interval_s": det_cfg["poll_interval_s"],
        "detections_before_remove": det_cfg["detections_before_remove"],
        "lost_timeout_s": det_cfg["lost_timeout_s"],
        "queue_soft_cap": det_cfg["queue_soft_cap"],
        "skew_s": skew_s,
        "deliver_window_s": deliver_window_s,
        "cells": [{
            "name": c["name"],
            "mode": c["mode"],
            "adv_interval_ms": int(c["adv_interval_ms"]),
            "update_interval_ms": int(c.get("update_interval_ms", 0)),
            "duration_ms": int(c.get("duration_ms", 0)),
            "count": int(c["count"]),
            "mid_base": c["mid_base"],
            "tx_power_dbm": c.get("tx_power_dbm"),
            "expected": [[m, p] for m, p in c["expected"]],
        } for c in cells],
    }


# --------------------------------------------------------------------------- #
# Build / flash
# --------------------------------------------------------------------------- #

def flash_once(project_dir: Path, build_dir: Path, port: str, idf_py: str) -> None:
    """Build and flash the generic reconfigurable firmware, once for the whole run.

    Every cell is configured at runtime over serial (see build_command and the
    firmware's `run` parser), so there is a single build/flash up front rather
    than the old per-cell reflash. No sdkconfig fragment and no baked-in UID.
    """
    cmd = [
        idf_py,
        "-C", str(project_dir),
        "-B", str(build_dir),
        "-p", port,
        "build", "flash",
    ]
    print("building & flashing espbench (once) ...", flush=True)
    _run_build(cmd)


def build_command(cell: dict, uid: bytes) -> str:
    """Compose the `run key=val ...` line the firmware parses for one cell.

    Mirrors espbench.c's parser: uid/mode/mid are required; the rest map to the
    cell's runtime parameters. The line is newline-terminated so the device's
    fgets() sees a complete command.
    """
    parts = [
        "run",
        f"uid={uid.hex()}",
        f"mode={cell['mode']}",
        f"mid={cell['mid_base']}",
        f"adv_ms={int(cell['adv_interval_ms'])}",
    ]
    if cell["mode_int"] == bc.MODE_STATIC:
        parts.append(f"dur_ms={int(cell['duration_ms'])}")
        parts.append(f"pay={int(cell['payload_start'])}")
    else:
        parts.append(f"upd_ms={int(cell['update_interval_ms'])}")
        parts.append(f"count={int(cell['count'])}")
        if cell["mode_int"] == bc.MODE_RANDOM:
            parts.append(f"seed={int(cell['seed'])}")
        else:  # incremental
            parts.append(f"pay={int(cell['payload_start'])}")
    if cell.get("tx_power_dbm") is not None:
        parts.append(f"txdbm={int(cell['tx_power_dbm'])}")
    return " ".join(parts) + "\n"


def park_board(port: str, esptool: str = "esptool.py") -> None:
    """Leave the board halted in the ROM bootloader so it stops advertising.

    After a cell's `done` marker the firmware keeps advertising its last carrier
    until the next command or reset. To stop the radio at the end of a run we
    reset the chip *into* the ROM serial bootloader (`--before default_reset`)
    and then decline the final reset (`--after no_reset`): the application never
    starts, so NimBLE never comes up and nothing is advertised until the next
    reset/power-cycle. `chip_id` is just a trivial op to hang the reset behaviour
    on. Best-effort: a failure here only means the board keeps beaconing, so we
    warn, not abort. The caller must close the serial port first (esptool needs
    it).
    """
    cmd = [esptool, "--port", port, "--before", "default_reset",
           "--after", "no_reset", "chip_id"]
    print("parking board in bootloader (stops advertising) ...", flush=True)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout)
        print("warning: could not park the board; it may keep advertising. "
              "Unplug it or hold BOOT + tap RESET to stop.", file=sys.stderr)


def _run_build(cmd: list[str]) -> None:
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

def reset_board(ser: serial.Serial) -> None:
    """esp32 auto-reset: pulse EN (RTS) to restart the board from boot.

    Done once before the cell loop so the firmware comes up clean and prints its
    ready marker; individual cells are then driven by serial commands, no reset.
    """
    ser.setDTR(False)
    ser.setRTS(True)
    time.sleep(0.1)
    ser.setRTS(False)


def wait_ready(ser: serial.Serial, timeout_s: float) -> bool:
    """Read the serial log until the firmware's ready marker, or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        if READY_RE.search(line):
            return True
    return False


def capture_run(ser: serial.Serial, timeout_s: float, command: str,
                on_tx=None) -> tuple[list[dict], list[str]]:
    """Send one cell's run command and record the serial log until the done marker.

    Returns (observed, raw_lines) where observed is a list of
    {mid, payload, device_ms, host_time} dicts for each tx line seen.

    `on_tx(mid, payload, host_time)`, if given, is called the instant each window
    goes on air, so the transmission series and the detection poller both learn of
    the key at its true send time.
    """
    observed: list[dict] = []
    raw: list[str] = []

    # Drop any leftover chatter (e.g. the previous cell's last-carrier logs) so
    # stale tx lines are never misattributed to this cell, then issue the command.
    ser.reset_input_buffer()
    ser.write(command.encode())
    ser.flush()

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        raw.append(line)
        m = TX_RE.search(line)
        if m:
            mid = int(m.group(1))
            payload = int(m.group(2), 16)
            host_time = time.time()
            observed.append({
                "mid": mid,
                "payload": payload,
                "device_ms": int(m.group(3)),
                "host_time": host_time,
            })
            if on_tx is not None:
                on_tx(mid, payload, host_time)
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
            f"sequence ({len(exp)} windows); trusting config for analysis")
        exp_set = set(exp)
        for pair in obs_pairs:
            if pair not in exp_set:
                warnings.append(f"  observed unexpected window mid={pair[0]} payload=0x{pair[1]:02x}")
    return warnings


def estimate_timeout(cell: dict) -> float:
    """How long to wait for the run's done marker, with a slack margin.

    The board boots once up front (wait_ready), so no per-cell boot margin is
    needed; a cell command runs on the already-live firmware.
    """
    if cell["mode_int"] == bc.MODE_STATIC:
        run_s = int(cell["duration_ms"]) / 1000.0
    else:
        # count windows plus the firmware's trailing update-interval delay.
        run_s = (int(cell["count"]) + 1) * int(cell["update_interval_ms"]) / 1000.0
    return run_s + 30.0


# --------------------------------------------------------------------------- #
# Series writers (transmission + detection producers)
# --------------------------------------------------------------------------- #

class SeriesWriter:
    """Thread-safe, append-only CSV writer for one time-series producer.

    Rows are flushed as written so a series survives a crash mid-run and stays
    readable by a concurrent analysis. Both the serial thread (transmission) and
    the poller thread (detection) append through their own instance.
    """

    def __init__(self, path: Path, fields: list[str]) -> None:
        self._lock = threading.Lock()
        self._fields = fields
        self._fh = path.open("w", newline="")
        self._writer = csv.DictWriter(self._fh, fieldnames=fields,
                                      extrasaction="ignore")
        self._writer.writeheader()
        self._fh.flush()

    def append(self, row: dict) -> None:
        with self._lock:
            self._writer.writerow(row)
            self._fh.flush()

    def close(self) -> None:
        with self._lock:
            self._fh.close()


TRANSMISSION_FIELDS = ["cell", "uid", "mid", "payload", "send_time"]
DETECTION_FIELDS = ["cell", "uid", "mid", "payload", "report_id", "obs_timestamp",
                    "first_fetched_at", "latitude", "longitude",
                    "horizontal_accuracy", "confidence", "status"]


# --------------------------------------------------------------------------- #
# Detection producer: one continuous poller for the whole run
# --------------------------------------------------------------------------- #
#
# A single fetch returns at most ~8 recent reports per key -- a hard server-side
# cap. And a report can become queryable minutes after its key stopped
# advertising, so to measure propagation faithfully we must keep fetching a key
# long after its window ends. One poller runs for the entire run, holding a queue
# of not-yet-done keys and fetching every one of them each cycle. Keys are
# enqueued the instant they start transmitting (on_tx) so nothing is missed and
# propagation carries no window-length bias. Only the poller talks to Apple, so
# there is never concurrent account access.

def adaptive_timeout(base_s: float, queue_size: int, soft_cap: int) -> float:
    """Effective lost-timeout for an unseen key, shrinking as the backlog grows.

    Full `base_s` while the queue fits `soft_cap` (high density empties it fast);
    above that, shrink inversely with queue size so unseen keys are dropped
    sooner and the steady-state fetch rate stays bounded on the single account.
    This is a polling-efficiency knob only -- deliverability is decided offline
    with whatever window is chosen, so an aggressive timeout can never false-LOST
    a real-but-slow delivery; it only means fewer detection rows.
    """
    if queue_size <= soft_cap or soft_cap <= 0:
        return base_s
    return base_s * soft_cap / queue_size


class QueueEntry:
    """One key the poller is still fetching, carrying its own uid."""

    __slots__ = ("cell", "uid", "mid", "payload", "send_time", "seen", "detections")

    def __init__(self, cell: str, uid: bytes, mid: int, payload: int,
                 send_time: float) -> None:
        self.cell = cell
        self.uid = uid
        self.mid = mid
        self.payload = payload
        self.send_time = send_time
        self.seen: set[str] = set()   # report ids already logged for this key
        self.detections = 0


class DetectionPoller(threading.Thread):
    """The run's single detection producer.

    Maintains a queue of `QueueEntry` keys (each with its own uid). Every
    `interval_s` it fetches all queued keys, appends each newly-seen report to the
    detection series (stamping `first_fetched_at` = now), and then drops keys that
    have reached `detections_before_remove` or -- while still unseen -- the
    adaptive lost-timeout. A key still delivering keeps its slot until its
    detection budget is spent, so a static soak (unbounded budget) is polled for
    the whole run.
    """

    def __init__(self, account, writer: SeriesWriter, *, interval_s: float,
                 detections_before_remove: int, lost_timeout_s: float,
                 queue_soft_cap: int, skew_s: float, stop_event) -> None:
        super().__init__(daemon=True)
        self._account = account
        self._writer = writer
        self._interval = max(1.0, interval_s)
        self._cap = detections_before_remove
        self._timeout = lost_timeout_s
        self._soft_cap = queue_soft_cap
        self._skew = skew_s
        self._stop = stop_event
        self._lock = threading.Lock()
        self._queue: list[QueueEntry] = []

    def enqueue(self, cell: str, uid: bytes, mid: int, payload: int,
                send_time: float) -> None:
        with self._lock:
            self._queue.append(QueueEntry(cell, uid, mid, payload, send_time))

    def queue_size(self) -> int:
        with self._lock:
            return len(self._queue)

    def _fetch_entry(self, entry: QueueEntry) -> None:
        """Fetch one key and append any newly-seen reports to the detection series."""
        try:
            reports = bc.fetch_reports_for_mid(
                self._account, entry.uid, entry.mid,
                since_epoch=entry.send_time, skew_s=self._skew)
        except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the run
            print(f"    poll: {entry.cell} mid={entry.mid} fetch failed: {exc!r}",
                  file=sys.stderr, flush=True)
            return
        now = time.time()
        uid_hex = entry.uid.hex()
        for payload, records in reports.items():
            for rec in records:
                # Stop at the detection budget (0 = unbounded) so a single fetch
                # never logs more than detections_before_remove rows for a key.
                if self._cap > 0 and entry.detections >= self._cap:
                    return
                rid = rec["id"]
                if rid in entry.seen:
                    continue
                entry.seen.add(rid)
                entry.detections += 1
                self._writer.append({
                    "cell": entry.cell,
                    "uid": uid_hex,
                    "mid": entry.mid,
                    "payload": payload,
                    "report_id": rid,
                    "obs_timestamp": rec["timestamp"],
                    "first_fetched_at": now,
                    "latitude": rec.get("latitude"),
                    "longitude": rec.get("longitude"),
                    "horizontal_accuracy": rec.get("horizontal_accuracy"),
                    "confidence": rec.get("confidence"),
                    "status": rec.get("status"),
                })

    def poll_once(self) -> None:
        with self._lock:
            entries = list(self._queue)
        for entry in entries:
            self._fetch_entry(entry)
        # Drop finished keys. `detections_before_remove <= 0` means unbounded
        # (a soak time-series). An unseen key is dropped once it outlives the
        # (queue-adaptive) timeout -- efficiency only, never a verdict.
        with self._lock:
            eff = adaptive_timeout(self._timeout, len(self._queue), self._soft_cap)
            now = time.time()
            keep = []
            for entry in self._queue:
                if self._cap > 0 and entry.detections >= self._cap:
                    continue
                if entry.detections == 0 and now - entry.send_time > eff:
                    continue
                keep.append(entry)
            self._queue = keep

    def run(self) -> None:
        # wait() returns True the moment we are asked to stop, ending the loop.
        while not self._stop.wait(self._interval):
            self.poll_once()


def single_sweep(account, writer: SeriesWriter, tx_records: list[dict], *,
                 skew_s: float) -> None:
    """--no-poll fallback: one fetch per transmitted key into the detection series.

    Deliverability is fine from a single post-run sweep (it answers yes/no
    completely); only propagation timing needs the continuous poller, so this
    keeps the no-poll path useful. `first_fetched_at` is just now, so propagation
    is not meaningful here.
    """
    seen: set[str] = set()
    seen_keys: set[tuple[bytes, int]] = set()
    for tx in tx_records:
        key = (tx["uid"], tx["mid"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        try:
            reports = bc.fetch_reports_for_mid(
                account, tx["uid"], tx["mid"], since_epoch=tx["send_time"], skew_s=skew_s)
        except Exception as exc:  # noqa: BLE001
            print(f"    sweep: {tx['cell']} mid={tx['mid']} failed: {exc!r}",
                  file=sys.stderr, flush=True)
            continue
        now = time.time()
        uid_hex = tx["uid"].hex()
        for payload, records in reports.items():
            for rec in records:
                if rec["id"] in seen:
                    continue
                seen.add(rec["id"])
                writer.append({
                    "cell": tx["cell"], "uid": uid_hex, "mid": tx["mid"],
                    "payload": payload, "report_id": rec["id"],
                    "obs_timestamp": rec["timestamp"], "first_fetched_at": now,
                    "latitude": rec.get("latitude"), "longitude": rec.get("longitude"),
                    "horizontal_accuracy": rec.get("horizontal_accuracy"),
                    "confidence": rec.get("confidence"), "status": rec.get("status"),
                })


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    # `--test` runs the merged-in offline unit tests (no hardware/network) and
    # exits, so the matrix positional is not required for it.
    if "--test" in sys.argv:
        _run_all()
        return

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("matrix", type=Path, help="matrix JSON file")
    parser.add_argument("--port", help="serial port (overrides matrix 'port')")
    parser.add_argument("--baud", type=int, default=115200, help="serial baud (default 115200)")
    parser.add_argument("--no-flash", action="store_true",
                        help="skip build/flash; capture a run already flashed on the board")
    parser.add_argument("--no-fetch", action="store_true",
                        help="capture the run but skip the detection/analysis stage")
    parser.add_argument("--no-poll", action="store_true",
                        help="disable the continuous poller; do one post-run sweep "
                             "per key into the detection series instead")
    parser.add_argument("--no-density", action="store_true",
                        help="skip the background Apple-device density logger")
    parser.add_argument("--deliver-window-s", type=float, default=None,
                        help="deliverability window for the offline analysis "
                             "(default: matrix value, else no upper bound)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved cells and mid allocation, then exit")
    parser.add_argument("--no-park", action="store_true",
                        help="do not leave the board halted in the bootloader at "
                             "the end (it will keep advertising its last carrier)")
    args = parser.parse_args()

    matrix = json.loads(args.matrix.read_text())
    cells = resolve_cells(matrix)
    det_cfg = detection_config(matrix)
    dens_cfg = density_config(matrix)

    project_dir = Path(matrix.get("project_dir", bc.PROJECT_ROOT)).resolve()
    build_dir = Path(matrix.get("build_dir", project_dir / "build")).resolve()
    idf_py = matrix.get("idf_py", "idf.py")
    port = args.port or matrix.get("port")
    settle_s = float(matrix.get("settle_seconds", 120))
    skew_s = float(matrix.get("report_floor_skew_s", 120))
    deliver_window_s = (args.deliver_window_s if args.deliver_window_s is not None
                        else matrix.get("deliver_window_s"))

    if args.dry_run:
        for c in cells:
            print(f"{c['name']}: mode={c['mode']} adv={c['adv_interval_ms']}ms "
                  f"mid_base={c['mid_base']} windows={len(c['expected'])}")
        poll = det_cfg["poll"] and not args.no_poll
        print(f"detection: poll={poll} interval={det_cfg['poll_interval_s']:.0f}s "
              f"detections_before_remove={det_cfg['detections_before_remove']} "
              f"lost_timeout_s={det_cfg['lost_timeout_s']:.0f} "
              f"queue_soft_cap={det_cfg['queue_soft_cap']}")
        return

    if not port:
        sys.exit("error: no serial port (pass --port or set 'port' in the matrix)")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results_dir = Path(matrix.get("results_dir", bc.PROJECT_ROOT / "results")) / run_id
    results_dir.mkdir(parents=True, exist_ok=True)
    print(f"results -> {results_dir}")

    # cells.json: the analysis manifest (config + config-derived window lists,
    # no secrets). Written up front so analyze.py can run over a partial series.
    (results_dir / "cells.json").write_text(json.dumps(
        cells_manifest(cells, det_cfg, skew_s, deliver_window_s, run_id),
        indent=2) + "\n")

    # Flash the generic firmware once, before opening the serial port (esptool
    # needs the port). Every cell is then configured at runtime over serial.
    if not args.no_flash:
        flash_once(project_dir, build_dir, port, idf_py)

    # One serial connection for the whole capture phase. Boot the firmware once
    # and wait for its ready marker; each cell is then driven by a command with
    # no reset in between. Opened before the writers/poller/density logger so a
    # bad port fails fast without leaving them running.
    ser = serial.Serial(port, args.baud, timeout=1)
    print("resetting board, waiting for ready ...", flush=True)
    reset_board(ser)
    if not wait_ready(ser, 30.0):
        print("warning: did not see the ready marker within 30s; sending commands "
              "anyway", file=sys.stderr)

    account = None if args.no_fetch else bc.load_account()

    poll = det_cfg["poll"] and not args.no_fetch and not args.no_poll

    # Time-series writers. Both carry uid (and detection carries GPS), so both are
    # gitignored under results/; only summary.csv + density.csv are tracked.
    tx_writer = SeriesWriter(results_dir / "transmission.csv", TRANSMISSION_FIELDS)
    det_writer = (SeriesWriter(results_dir / "detection.csv", DETECTION_FIELDS)
                  if not args.no_fetch else None)
    # In-memory transmission log, needed for the --no-poll single sweep.
    tx_records: list[dict] = []
    tx_lock = threading.Lock()

    # Background Apple-device density logger. Samples the nearby finder count for
    # the whole run into density.csv (see scan_density.py); stdout -> density.log.
    density_proc = density_log = None
    if dens_cfg["enabled"] and not args.no_density:
        dcmd = [sys.executable, str(bc.SCRIPT_DIR / "scan_density.py"), "--watch",
                "--interval", str(dens_cfg["interval_s"]),
                "--window", str(dens_cfg["window_s"]),
                "--out", str(results_dir / "density.csv")]
        if dens_cfg["rssi_min"] is not None:
            dcmd += ["--rssi-min", str(int(dens_cfg["rssi_min"]))]
        density_log = (results_dir / "density.log").open("w")
        density_proc = subprocess.Popen(dcmd, stdout=density_log,
                                        stderr=subprocess.STDOUT)
        print(f"density -> {results_dir / 'density.csv'} "
              f"(every {dens_cfg['interval_s']:.0f}s)")

    # The single detection poller for the whole run.
    poller = stop_event = None
    if poll:
        stop_event = threading.Event()
        poller = DetectionPoller(
            account, det_writer, interval_s=det_cfg["poll_interval_s"],
            detections_before_remove=det_cfg["detections_before_remove"],
            lost_timeout_s=det_cfg["lost_timeout_s"],
            queue_soft_cap=det_cfg["queue_soft_cap"], skew_s=skew_s,
            stop_event=stop_event)
        print(f"detection: polling every {det_cfg['poll_interval_s']:.0f}s "
              f"(detections_before_remove={det_cfg['detections_before_remove']}, "
              f"lost_timeout_s={det_cfg['lost_timeout_s']:.0f})", flush=True)
        poller.start()

    def process_cell(cell: dict) -> None:
        """Send one cell's command, capture, and record its transmissions.

        Mints a fresh per-cell UID (kept in RAM on the device, so no reflash),
        enqueues each key into the poller, and appends the transmission row the
        instant it goes on air. Raises on any failure so the caller can defer the
        cell to the retry pass.
        """
        cell_dir = results_dir / cell["name"]
        cell_dir.mkdir(exist_ok=True)

        # Every cell (including a retry) gets its own random UID, so even
        # identical (mid, payload) pairs derive independent carriers. Record it
        # so the cell's mids can be re-fetched later (secret; results/ gitignored).
        uid = os.urandom(32)
        (cell_dir / "uid.hex").write_text(uid.hex() + "\n")

        def on_tx(mid: int, payload: int, host_time: float) -> None:
            tx_writer.append({"cell": cell["name"], "uid": uid.hex(), "mid": mid,
                              "payload": payload, "send_time": host_time})
            with tx_lock:
                tx_records.append({"cell": cell["name"], "uid": uid, "mid": mid,
                                   "payload": payload, "send_time": host_time})
            if poller is not None:
                poller.enqueue(cell["name"], uid, mid, payload, host_time)

        command = build_command(cell, uid)
        observed, raw = capture_run(ser, estimate_timeout(cell), command, on_tx=on_tx)
        (cell_dir / "serial.log").write_text("\n".join(raw) + "\n")

        for w in reconcile(cell, observed):
            print(f"  warning: {w}", file=sys.stderr)

        # A successful capture supersedes any earlier failure marker.
        (cell_dir / "error.txt").unlink(missing_ok=True)
        print(f"  captured {len(observed)} window(s)"
              + ("" if poller is None else "; enqueued for polling"), flush=True)

    deferred: list[dict] = []

    try:
        # First pass: run every cell. A cell that fails (a transient flash/USB
        # glitch) is deferred, not abandoned, so an unattended run keeps going.
        # The poller keeps fetching earlier cells' keys throughout.
        for cell in cells:
            print(f"\n=== {cell['name']} (mid_base={cell['mid_base']}, "
                  f"{len(cell['expected'])} windows) ===", flush=True)
            try:
                process_cell(cell)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the run alive
                print(f"  ERROR: cell {cell['name']} failed: {exc!r}; deferring to a "
                      "retry pass after the other cells", file=sys.stderr, flush=True)
                deferred.append(cell)
                continue

        # Retry pass: return to each deferred cell once. A second failure is
        # recorded and given up on.
        for cell in deferred:
            print(f"\n=== RETRY {cell['name']} (deferred after an earlier failure) ===",
                  flush=True)
            try:
                process_cell(cell)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"  ERROR: cell {cell['name']} failed again: {exc!r}; giving up",
                      file=sys.stderr, flush=True)
                (results_dir / cell["name"] / "error.txt").write_text(
                    f"{type(exc).__name__}: {exc}\n")
                continue

        # Final drain: keep polling until the queue empties (every key hit its
        # detection budget or timed out) or the drain cap elapses. The cap is at
        # least `lost_timeout_s`, so the last cell's key gets the same patience as
        # any other -- a short `settle_seconds` must not cut it off before its
        # reports (and their propagation) can land. The loop still exits the moment
        # the queue empties, so a generous cap costs nothing once everything is in.
        if poller is not None:
            drain_cap_s = max(settle_s, det_cfg["lost_timeout_s"])
            print(f"\n=== draining poll queue (<= {drain_cap_s:.0f}s) ===", flush=True)
            deadline = time.time() + drain_cap_s
            while time.time() < deadline and poller.queue_size() > 0:
                time.sleep(min(det_cfg["poll_interval_s"], max(0.0, deadline - time.time())))
            stop_event.set()
            poller.join(timeout=30)
            print("  poller stopped", flush=True)
        elif not args.no_fetch:
            # --no-poll: single post-run sweep after a settle for reports to land.
            print(f"\n=== settling {settle_s:.0f}s then single sweep ===", flush=True)
            time.sleep(settle_s)
            with tx_lock:
                snapshot = list(tx_records)
            single_sweep(account, det_writer, snapshot, skew_s=skew_s)
    finally:
        # Close the serial port before park_board (esptool needs it).
        ser.close()
        tx_writer.close()
        if det_writer is not None:
            det_writer.close()
        if density_proc is not None:
            density_proc.terminate()
            try:
                density_proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                density_proc.kill()
            density_log.close()

    if deferred:
        names = ", ".join(c["name"] for c in deferred)
        print(f"note: {len(deferred)} cell(s) needed a retry pass: {names}",
              file=sys.stderr)

    # Offline analysis: join the three series into summary + per-cell artifacts.
    if not args.no_fetch:
        print("\n=== analysing series ===", flush=True)
        uid_by_cell = {c: recs[0]["uid"].hex() for c, recs in
                       _group_uids(tx_records).items()}
        summary = analyze.analyze_run(results_dir, deliver_window_s=deliver_window_s,
                                      uid_by_cell=uid_by_cell)
        for row in summary:
            lat = (f"latency median {row['latency_median_s']:.1f}s (n={row['latency_n']})"
                   if row["latency_n"] else "latency n/a")
            print(f"  {row['name']}: deliverability {row['deliverability']:.1%}, "
                  f"correctness {row['correctness']:.1%}, "
                  f"{row['throughput_bps_delivered']:.4f} B/s delivered, {lat}",
                  flush=True)
        if summary:
            print(f"\nsummary -> {results_dir / 'summary.csv'}")

    # Halt the board so it stops beaconing its last carrier (see park_board).
    if not args.no_park and matrix.get("park_on_finish", True):
        park_board(port, matrix.get("esptool", "esptool.py"))


def _group_uids(tx_records: list[dict]) -> dict[str, list[dict]]:
    """Map each cell name to its transmission records (for the uid override)."""
    out: dict[str, list[dict]] = {}
    for r in tx_records:
        out.setdefault(r["cell"], []).append(r)
    return out


# =========================================================================== #
# Offline unit tests (merged from the former scripts/test_run_matrix.py)
# =========================================================================== #
#
# These exercise the recording + analysis logic without hardware or the network:
# the poller's fetch is monkeypatched to serve synthetic report records, so no
# Apple account is touched. Run with:
#
#     scripts/.venv/bin/python run_matrix.py --test
#     # or, via pytest:
#     scripts/.venv/bin/python -m pytest run_matrix.py -q


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _record(rid: str, obs_epoch: float, payload_hint: int = 0) -> dict:
    """A synthetic decrypted report record shaped like bench_common produces."""
    return {
        "id": rid,
        "timestamp": _iso(obs_epoch),
        "latitude": 1.2924 + payload_hint * 1e-6,
        "longitude": 103.772,
        "horizontal_accuracy": 50.0,
        "confidence": 2,
        "status": 0,
    }


class FakeAccount:
    """Stands in for AppleAccount; never touches the network."""


def install_fake_fetch(monkey_reports):
    """Patch bench_common.fetch_reports_for_mid to serve a scripted table.

    `monkey_reports` maps (uid_hex, mid) -> {payload: [records]}. Applies the
    `since_epoch`/`skew_s` floor exactly like the real helper, so the timeout /
    window logic is exercised against realistic filtering.
    """
    def fake(account, uid, mid, *, since_epoch=None, skew_s=0.0):
        table = monkey_reports.get((uid.hex(), mid), {})
        floor = None if since_epoch is None else since_epoch - skew_s
        out = {}
        for payload, recs in table.items():
            kept = [r for r in recs
                    if floor is None
                    or datetime.fromisoformat(r["timestamp"]).timestamp() >= floor]
            if kept:
                out[payload] = kept
        return out
    original = bc.fetch_reports_for_mid
    bc.fetch_reports_for_mid = fake
    return original


def test_adaptive_timeout_full_below_cap():
    assert adaptive_timeout(300.0, queue_size=1, soft_cap=8) == 300.0
    assert adaptive_timeout(300.0, queue_size=8, soft_cap=8) == 300.0


def test_adaptive_timeout_shrinks_with_backlog():
    # At 2x the soft cap the effective timeout should halve; strictly decreasing.
    assert adaptive_timeout(300.0, queue_size=16, soft_cap=8) == 150.0
    assert adaptive_timeout(300.0, queue_size=32, soft_cap=8) == 75.0
    big = [adaptive_timeout(300.0, q, 8) for q in (8, 12, 24, 48)]
    assert big == sorted(big, reverse=True)


class RecordingWriter:
    """Captures appended rows instead of writing a file."""

    def __init__(self):
        self.rows = []
        self._lock = threading.Lock()

    def append(self, row):
        with self._lock:
            self.rows.append(dict(row))

    def close(self):
        pass


def _make_poller(writer, reports, **kw):
    install_fake_fetch(reports)
    stop = threading.Event()
    cfg = dict(interval_s=1.0, detections_before_remove=1, lost_timeout_s=300.0,
               queue_soft_cap=8, skew_s=120.0, stop_event=stop)
    cfg.update(kw)
    poller = DetectionPoller(FakeAccount(), writer, **cfg)
    return poller, stop


def test_enqueue_on_tx_and_per_key_uid_fetch():
    # Two cells, two distinct uids and mids; each key must be fetched under its
    # own uid, and its detection row carries that uid.
    uid_a = bytes([0xAA] * 32)
    uid_b = bytes([0xBB] * 32)
    now = time.time()
    reports = {
        (uid_a.hex(), 10): {5: [_record("a1", now)]},
        (uid_b.hex(), 20): {7: [_record("b1", now)]},
    }
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, reports, detections_before_remove=1)
    poller.enqueue("cellA", uid_a, 10, 5, now)
    poller.enqueue("cellB", uid_b, 20, 7, now)
    poller.poll_once()

    by_id = {r["report_id"]: r for r in writer.rows}
    assert by_id["a1"]["uid"] == uid_a.hex() and by_id["a1"]["mid"] == 10
    assert by_id["b1"]["uid"] == uid_b.hex() and by_id["b1"]["mid"] == 20
    # first_fetched_at stamped on the fetch that first returned each id.
    assert by_id["a1"]["first_fetched_at"] >= now
    # Both keys hit detections_before_remove=1, so the queue drains.
    assert poller.queue_size() == 0


def test_detections_before_remove_bounds_rows_then_drops():
    uid = bytes([0x11] * 32)
    now = time.time()
    # Three distinct observations available for the same key.
    reports = {(uid.hex(), 3): {0: [_record("r1", now), _record("r2", now + 1),
                                    _record("r3", now + 2)]}}
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, reports, detections_before_remove=2)
    poller.enqueue("c", uid, 3, 0, now)

    poller.poll_once()
    # Logs up to the budget of 2 in the first cycle, then the key is dropped.
    assert len(writer.rows) == 2
    assert poller.queue_size() == 0
    # A second cycle adds nothing (key gone from the queue).
    poller.poll_once()
    assert len(writer.rows) == 2


def test_unbounded_budget_keeps_key_and_dedups():
    uid = bytes([0x22] * 32)
    now = time.time()
    table = {0: [_record("r1", now)]}
    reports = {(uid.hex(), 4): table}
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, reports, detections_before_remove=0,
                             lost_timeout_s=10_000)
    poller.enqueue("c", uid, 4, 0, now)

    poller.poll_once()
    assert len(writer.rows) == 1 and poller.queue_size() == 1  # unbounded -> stays
    # A new observation appears; the old id must not be re-logged.
    table[0].append(_record("r2", now + 1))
    poller.poll_once()
    ids = [r["report_id"] for r in writer.rows]
    assert ids == ["r1", "r2"] and poller.queue_size() == 1


def test_first_fetched_at_stamped_once():
    uid = bytes([0x33] * 32)
    now = time.time()
    reports = {(uid.hex(), 9): {0: [_record("r1", now)]}}
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, reports, detections_before_remove=5,
                             lost_timeout_s=10_000)
    poller.enqueue("c", uid, 9, 0, now)
    poller.poll_once()
    stamp = writer.rows[0]["first_fetched_at"]
    time.sleep(0.02)
    poller.poll_once()  # id already seen -> no new row, stamp unchanged
    assert len(writer.rows) == 1 and writer.rows[0]["first_fetched_at"] == stamp


def test_unseen_key_dropped_after_timeout_no_detection():
    uid = bytes([0x44] * 32)
    now = time.time()
    reports = {}  # nothing ever returned -> the key stays unseen
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, reports, lost_timeout_s=50)
    # Enqueue with a send_time already past the timeout.
    poller.enqueue("c", uid, 1, 0, now - 100)
    poller.poll_once()
    assert writer.rows == [] and poller.queue_size() == 0


def test_adaptive_shrink_drops_backlogged_unseen_keys():
    # A large backlog of unseen keys, all older than the shrunk timeout but
    # younger than the base timeout, must be dropped once the queue exceeds cap.
    uid = bytes([0x55] * 32)
    now = time.time()
    writer = RecordingWriter()
    poller, _ = _make_poller(writer, {}, lost_timeout_s=300, queue_soft_cap=4)
    # 40 keys aged 100s: effective timeout = 300 * 4 / 40 = 30s < 100s -> drop all.
    for i in range(40):
        poller.enqueue("c", uid, 100 + i, 0, now - 100)
    poller.poll_once()
    assert poller.queue_size() == 0
    # With the same age but a small queue, the full 300s timeout keeps the key.
    poller.enqueue("c", uid, 999, 0, now - 100)
    poller.poll_once()
    assert poller.queue_size() == 1


def test_poller_thread_runs_and_drains():
    uid = bytes([0x66] * 32)
    now = time.time()
    reports = {(uid.hex(), 7): {0: [_record("r1", now)]}}
    writer = RecordingWriter()
    poller, stop = _make_poller(writer, reports, interval_s=0.05)
    poller.start()
    poller.enqueue("c", uid, 7, 0, now)
    deadline = time.time() + 5
    while poller.queue_size() > 0 and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    poller.join(timeout=5)
    assert [r["report_id"] for r in writer.rows] == ["r1"]


def _cell(name="c", mid_base=10, expected=None):
    return {
        "name": name, "mode": "incremental", "adv_interval_ms": 1000,
        "update_interval_ms": 60000, "duration_ms": 0, "count": 1,
        "mid_base": mid_base,
        "expected": expected if expected is not None else [[mid_base, 5]],
    }


def _tx_row(cell, uid, mid, payload, send_time):
    return {"cell": cell, "uid": uid, "mid": mid, "payload": payload,
            "send_time": send_time}


def _det_row(cell, uid, mid, payload, rid, obs_epoch, ff_epoch):
    return {"cell": cell, "uid": uid, "mid": mid, "payload": payload,
            "report_id": rid, "obs_timestamp": _iso(obs_epoch),
            "first_fetched_at": ff_epoch, "latitude": "1.29", "longitude": "103.77",
            "horizontal_accuracy": "50", "confidence": "2", "status": "0"}


def test_analyze_deliverability_latency_propagation():
    send = 1_000_000.0
    cell = _cell(expected=[[10, 5]])
    tx = [_tx_row("c", "aa", 10, 5, send)]
    # observed 60s after send; first fetched 90s later -> propagation 30s.
    det = [_det_row("c", "aa", 10, 5, "r1", send + 60, send + 90),
           _det_row("c", "aa", 10, 5, "r2", send + 62, send + 92)]
    res = analyze.analyze_cell(cell, tx, det, deliver_window_s=None,
                               skew_s=120.0, polled=True)
    assert res["delivered"] == 1 and res["correct"] == 1
    assert res["deliverability"] == 1.0
    assert abs(res["latency_median_s"] - 60.0) < 1e-6      # discovery
    assert abs(res["propagation_min_s"] - 30.0) < 1e-6     # first_fetched - obs
    assert res["reports_per_delivered_median"] == 2        # redundancy depth


def test_analyze_wrong_payload_is_delivered_not_correct():
    send = 2_000_000.0
    cell = _cell(expected=[[10, 5]])
    tx = [_tx_row("c", "aa", 10, 5, send)]
    det = [_det_row("c", "aa", 10, 9, "r1", send + 30, send + 40)]  # payload 9 != 5
    res = analyze.analyze_cell(cell, tx, det, deliver_window_s=None,
                               skew_s=120.0, polled=True)
    assert res["delivered"] == 1 and res["correct"] == 0


def test_analyze_deliver_window_excludes_late_report():
    send = 3_000_000.0
    cell = _cell(expected=[[10, 5]])
    tx = [_tx_row("c", "aa", 10, 5, send)]
    det = [_det_row("c", "aa", 10, 5, "r1", send + 500, send + 520)]  # 500s later
    # A 300s window rejects the late report -> not delivered.
    res = analyze.analyze_cell(cell, tx, det, deliver_window_s=300.0,
                               skew_s=120.0, polled=True)
    assert res["delivered"] == 0
    # An open window (None) accepts it.
    res2 = analyze.analyze_cell(cell, tx, det, deliver_window_s=None,
                                skew_s=120.0, polled=True)
    assert res2["delivered"] == 1


def test_analyze_never_seen_window_censored_from_latency():
    send = 4_000_000.0
    cell = _cell(expected=[[10, 5], [11, 6]])
    tx = [_tx_row("c", "aa", 10, 5, send), _tx_row("c", "aa", 11, 6, send + 60)]
    det = [_det_row("c", "aa", 10, 5, "r1", send + 30, send + 40)]  # only mid 10
    res = analyze.analyze_cell(cell, tx, det, deliver_window_s=None,
                               skew_s=120.0, polled=True)
    assert res["windows"] == 2 and res["delivered"] == 1
    assert res["latency_n"] == 1  # the lost window is censored, not counted slow


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        original = bc.fetch_reports_for_mid
        try:
            fn()
            passed += 1
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {exc!r}")
            raise
        finally:
            bc.fetch_reports_for_mid = original
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    main()

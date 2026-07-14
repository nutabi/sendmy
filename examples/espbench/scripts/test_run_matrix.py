#!/usr/bin/env python3
"""Offline unit tests for the espbench 3-time-series harness.

These exercise the recording + analysis logic without hardware or the network:
the poller's fetch is monkeypatched to serve synthetic report records, so no
Apple account is touched. Run with:

    scripts/.venv/bin/python -m pytest scripts/test_run_matrix.py -q
    # or, dependency-free:
    scripts/.venv/bin/python scripts/test_run_matrix.py
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import analyze
import run_matrix as rm


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

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
    original = rm.bc.fetch_reports_for_mid
    rm.bc.fetch_reports_for_mid = fake
    return original


# --------------------------------------------------------------------------- #
# adaptive_timeout
# --------------------------------------------------------------------------- #

def test_adaptive_timeout_full_below_cap():
    assert rm.adaptive_timeout(300.0, queue_size=1, soft_cap=8) == 300.0
    assert rm.adaptive_timeout(300.0, queue_size=8, soft_cap=8) == 300.0


def test_adaptive_timeout_shrinks_with_backlog():
    # At 2x the soft cap the effective timeout should halve; strictly decreasing.
    assert rm.adaptive_timeout(300.0, queue_size=16, soft_cap=8) == 150.0
    assert rm.adaptive_timeout(300.0, queue_size=32, soft_cap=8) == 75.0
    big = [rm.adaptive_timeout(300.0, q, 8) for q in (8, 12, 24, 48)]
    assert big == sorted(big, reverse=True)


# --------------------------------------------------------------------------- #
# DetectionPoller
# --------------------------------------------------------------------------- #

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
    poller = rm.DetectionPoller(FakeAccount(), writer, **cfg)
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


# --------------------------------------------------------------------------- #
# Offline metric derivation (analyze)
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #

def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = 0
    for fn in fns:
        original = rm.bc.fetch_reports_for_mid
        try:
            fn()
            passed += 1
            print(f"PASS {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {fn.__name__}: {exc!r}")
            raise
        finally:
            rm.bc.fetch_reports_for_mid = original
    print(f"\n{passed}/{len(fns)} passed")


if __name__ == "__main__":
    _run_all()

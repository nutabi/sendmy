# `espbench`

`espbench` is an experiment harness for characterising the `sendmy` data
channel: how many bytes it can push through the crowd-sourced BLE relay network
per unit time (**throughput**) and what fraction of what it sends actually comes
back (**deliverability**). It is a sibling of `espsend` — same `sendmy_carrier` +
`sendmy_link` plumbing — but instead of transmitting a live sensor reading it
emits *synthetic, reproducible* payloads whose parameters are swept across an
experiment matrix, and it ships Python tooling that automates the whole loop.

## What it does

On boot it loads the 32-byte Unilink ID (`uid`) from NVS, brings up NimBLE, and
runs one experiment according to its compile-time configuration. Each *window*
advertises a single payload octet by deriving a carrier with
`sm_cr_build_carrier(uid, mid, payload)` and handing it to `sm_ll_set_key`. There
are three payload modes:

| Mode          | Payload per window        | `mid`                    | Purpose |
|---------------|---------------------------|--------------------------|---------|
| `static`      | fixed byte                | one, never rotates       | Does a single unchanging beacon keep being delivered over a long soak? |
| `incremental` | `(start + i) mod 256`     | advances every window    | Deterministic self-checking ramp. |
| `random`      | seeded xorshift32 PRNG    | advances every window    | Pseudo-random payloads; the host reproduces the exact sequence from the seed. |

The two independent variables are the **BLE advertising interval**
(`CONFIG_ESPBENCH_ADV_INTERVAL_MS`, how often the radio broadcasts the current
key) and, for the rotating modes, the **update interval**
(`CONFIG_ESPBENCH_UPDATE_INTERVAL_MS`, how long each `mid` is held before moving
to the next). A run of the rotating modes sends `CONFIG_ESPBENCH_MSG_COUNT`
windows and then prints a `done` marker; a `static` run holds its one carrier for
`CONFIG_ESPBENCH_STATIC_DURATION_MS` and then prints `done`.

Every window is logged on a stable, host-parseable line:

```
I (12345) espbench: tx mid=7 payload=0x1a t=63120
...
I (98765) espbench: done count=16 mid_base=0
```

`t` is milliseconds since boot; the harness also stamps host wall-clock time as
it reads each line, so both a relative and an absolute timeline are available.

### Ground truth: both seed *and* serial

Deliverability and correctness need to know what was actually sent. `espbench`
provides that two ways, and the harness cross-checks them:

- **Config-derived** — every mode is deterministic (incremental is arithmetic,
  random is a seeded xorshift32 the host mirrors bit-for-bit in
  `bench_common.expected_sequence`), so the host can reproduce the whole
  `(mid, payload)` sequence from the cell's parameters alone.
- **Serial log** — the firmware also prints each `(mid, payload, t)` as it goes.

The harness reconciles the two and warns on any disagreement, then uses the
config-derived sequence as the canonical list of mids to fetch (robust to a
dropped serial line).

## Configuration

All experiment parameters live in the `espbench configuration` menu
(`idf.py menuconfig`, or set directly in `sdkconfig` / an sdkconfig fragment):

| Option | Meaning |
|--------|---------|
| `ESPBENCH_MODE` | `static` / `incremental` / `random` |
| `ESPBENCH_ADV_INTERVAL_MS` | BLE advertising interval, 20–10240 ms |
| `ESPBENCH_UPDATE_INTERVAL_MS` | window length for rotating modes |
| `ESPBENCH_MSG_COUNT` | number of windows for rotating modes |
| `ESPBENCH_STATIC_DURATION_MS` | hold time for static (0 = forever) |
| `ESPBENCH_MID_BASE` | mid of the first/only window |
| `ESPBENCH_PAYLOAD_START` | constant byte (static) / ramp start (incremental) |
| `ESPBENCH_RANDOM_SEED` | 32-bit PRNG seed (random) |

`ESPBENCH_MID_BASE` matters when sweeping a matrix: every carrier is derived from
`(uid, mid, payload)`, so two cells that reuse the same mid range on the same
`uid` would broadcast colliding carriers on the shared relay. The harness assigns
each cell a disjoint mid range automatically; you only set this by hand for
standalone runs.

## Provisioning

Same as `espsend`: drop a 64-hex-character `uid.hex` (32 bytes) at the project
root, or generate one with `scripts/gen_seed.py`. The build bakes it into the NVS
image and `idf.py flash` writes it; the receiver derives carriers from the same
`uid`.

For **standalone** builds you manage `uid.hex` yourself. Under the automated
harness you do **not** — `run_matrix.py` mints a fresh random `uid` for every
cell (see below) and owns `uid.hex` while it runs.

## Build and flash (standalone)

Targets the Seeed XIAO ESP32-S3 (esp32s3, 8 MB flash).

```sh
idf.py menuconfig       # pick a mode + intervals under "espbench configuration"
idf.py build flash monitor
```

## Automated matrix runs

`scripts/run_matrix.py` drives the whole experiment loop. **Launch it from an
ESP-IDF-activated shell** (so `idf.py` is on PATH) with the board attached. For
each cell it mints a fresh `uid`, writes an sdkconfig fragment, builds and
flashes, resets the board and captures the serial log. It records three
independent, append-only **time-series** as the run happens — transmission (what
went on air), detection (what the relay returned), and density (nearby finders) —
and joins them into the delivery metrics *offline* at the end (see
[Three time-series + offline analysis](#three-time-series--offline-analysis)).

```sh
# one-time host setup
python3 -m venv scripts/.venv
scripts/.venv/bin/pip install findmy cryptography pyserial

# see how cells map onto mid ranges without touching hardware
scripts/.venv/bin/python scripts/run_matrix.py scripts/matrix.example.json --dry-run

# run the matrix (first fetch logs in and saves the session to account.json)
scripts/.venv/bin/python scripts/run_matrix.py scripts/matrix.example.json \
    --port /dev/tty.usbmodem1101
```

### Per-cell UIDs and independence

Every cell gets its own random 32-byte `uid`, so its carriers are fully
independent of every other cell and of any earlier run: even identical
`(mid, payload)` pairs derive different carriers under different UIDs, so nothing
can collide on the shared relay. This is what lets you re-run the *same* matrix
in different places without cross-contamination. Each cell's UID is saved to
`results/<timestamp>/<cell>/uid.hex` (secret; `results/` is gitignored) so you
can re-fetch that cell later. The mid ranges are still allocated disjointly as a
second layer of safety and for readable logs.

### Measuring crowd density

Delivery is dominated by how many relay-capable Apple devices happen to be
nearby, and that count swings minute-to-minute even in one spot — so a static
"environment" label was replaced by an actual measurement. During every run the
harness launches `scripts/scan_density.py` in the background, which passively
BLE-scans for Apple Continuity advertisements and logs the nearby **finder**
count (devices emitting the `0x10` NearbyInfo message — active iPhones/iPads/Macs
that can relay a Find My broadcast) to `density.csv`. Find My beacons (`0x12` —
AirTags, lost devices, and our own ESP32) are counted separately and are *not*
finders. Join each window's timestamp to the nearest density sample to regress
deliverability on the real local density instead of guessing at it.

`scan_density.py` also runs standalone: `python scan_density.py` for one
snapshot, `--rssi-min -70` to count only near devices (a better delivery
predictor, since a finder across the building will not relay our beacon), or
`--watch --interval 60 --out density.csv` to log a time-series.

### The matrix file

A JSON object (see `scripts/matrix.example.json`). Top-level keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | — | serial port (or pass `--port`) |
| `settle_seconds` | 120 | max time to keep draining the poll queue after the last cell |
| `report_floor_skew_s` | 120 | clock-skew slack when rejecting reports older than a window's send time |
| `mid_start` | 0 | first auto-assigned mid |
| `mid_gap` | 100 | spare mids left between cells |
| `deliver_window_s` | — | offline deliverability window (see below); unset = no upper bound |
| `detection` | — | detection/poll producer config (block; flat fallbacks below) |
| `density` | — | density producer config (block or bool toggle; flat fallbacks below) |
| `build_dir` | `<project>/build` | shared build directory (incremental) |
| `results_dir` | `scripts/results` | where run artifacts are written |
| `cells` | — | list of experiment cells (the transmission producer) |

The **`detection`** block (with backward-compatible top-level fallbacks):

| Key | Default | Meaning |
|-----|---------|---------|
| `poll` | false | run the continuous detection poller (`--no-poll` disables it) |
| `poll_interval_s` | 30 | how often the poller fetches every queued key |
| `detections_before_remove` | 1 | detections to log per key before dropping it from the queue (≤ 0 = unbounded, for a soak) |
| `lost_timeout_s` | 300 | base patience for an *unseen* key before the poller stops fetching it |
| `queue_soft_cap` | 8 | queue size under which the full timeout applies; above it the timeout shrinks to bound the fetch rate |

The **`density`** block (or set `density: false` to disable; flat `density_*`
keys still work):

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | true | log nearby Apple-device density during the run (`--no-density` to skip) |
| `interval_s` | 60 | seconds between density snapshots |
| `window_s` | 10 | scan duration per density snapshot |
| `rssi_min` | — | if set, only count devices at/above this RSSI (e.g. `-70` for near) |

Each cell: `mode`, `adv_interval_ms`, and then `update_interval_ms` + `count`
(rotating modes) or `duration_ms` (static); optional `payload_start`, `seed`,
`name`, `mid_base` (auto-assigned if omitted).

### Three time-series + offline analysis

The harness never decides delivery *while it runs*; it records three faithful,
append-only series and joins them afterwards. This is what makes propagation
latency correct — an early cell's keys keep being polled while later cells run, so
a report is first-seen when it truly becomes queryable, not at some end-of-run
sweep — and it lets every metric be re-derived (with a different deliverability
window, say) by `analyze.py` alone, without re-running or touching the servers.

1. **Transmission** (`transmission.csv`) — one row per key put on air
   (`cell, uid, mid, payload, send_time`), straight off the serial `tx` line.
2. **Detection** (`detection.csv`) — one row per (key, report) the poller ever
   sees (`cell, uid, mid, payload, report_id, obs_timestamp, first_fetched_at`,
   decrypted `latitude/longitude/horizontal_accuracy/confidence/status`), deduped
   by report id. `first_fetched_at` is the wall-clock of the *first* fetch that
   returned that id — that minus its observation timestamp is the propagation
   latency.
3. **Density** (`density.csv`) — the nearby-finder time-series (see below).

**The detection poller.** A single background poller runs for the whole run. Each
key is enqueued the instant it starts transmitting (`on_tx`), carrying its own
`uid`, and the poller fetches every queued key each `poll_interval_s`, appending
new reports to the detection series. A key leaves the queue once it has logged
`detections_before_remove` detections, or — while still unseen — once it outlives
the `lost_timeout_s` patience. That timeout is **adaptive**: it holds full value
while the queue fits `queue_soft_cap` and shrinks as the backlog grows, so the
fetch rate on the single account stays bounded under low density. Crucially these
are *polling-efficiency* knobs, **not** deliverability verdicts — deliverability
is decided offline with whatever window you choose, so an aggressive timeout can
never false-LOST a real-but-slow delivery; it only means fewer detection rows.
Only the poller talks to the relay (serial capture does no network I/O), so there
is never concurrent account access. `--no-poll` swaps the continuous poller for a
single post-run sweep per key (deliverability is still complete; propagation is
not meaningful).

Apple caps a single fetch at **~8 most-recent reports per key**, so a soak's true
multi-hour history is only obtainable by polling over time and unioning by id.
Set `detections_before_remove: 0` (unbounded) with a large `lost_timeout_s` to
keep a static key in the queue for the whole soak.

**Offline analysis.** At the end of a run — and re-runnable any time with
`analyze.py results/<timestamp> [--deliver-window-s N]` — the three series are
joined into `summary.csv` / `summary.json` and each `<cell>/result.json` +
`<cell>/timeseries.csv`. Those are *derived artifacts*; the CSV series are the
ground truth. `deliver_window_s` is the query-time deliverability definition: a
window counts as delivered only if its expected payload was observed within that
many seconds of its send time (unset = ever observed).

### Output

Under `scripts/results/<timestamp>/`:

- `transmission.csv` / `detection.csv` — the two secret series (they carry `uid`
  and decrypted GPS), gitignored. `cells.json` — the run config plus each cell's
  config-derived expected sequence, which `analyze.py` joins against.
- `summary.csv` / `summary.json` — one row per cell with
  `deliverability`, `correctness`, `bytes_sent`, `bytes_delivered`,
  `send_seconds`, offered and delivered throughput in bytes/second, the
  discovery-latency aggregates (`latency_n`, `latency_min_s`, `latency_median_s`,
  `latency_mean_s`, `latency_max_s`), `polled` (whether report counts are true
  totals or a capped tail), the report-volume aggregates (`reports_total`,
  `reports_per_delivered_mean` / `_median` / `_max`), and the propagation-latency
  aggregates (`propagation_n`, `propagation_min_s` / `_median_s` / `_mean_s` /
  `_max_s` — observed → queryable delay, from the detection series).
- `<cell>/serial.log` — the raw capture.
- `<cell>/result.json` — per-window detail (delivered/correct, `discovery_latency_s`,
  `observed_at`, `report_count`, `first_seen` / `last_seen` / `observation_span_s`)
  plus the cell's `uid` and `polled`. Each window also carries a full `reports`
  map: for every payload seen on that mid (collisions included), the complete
  chronological list of relay observations, each with `timestamp` and the
  decrypted location (`latitude`, `longitude`, `horizontal_accuracy`,
  `confidence`, `status`). We hold the private scalar for each carrier, so the
  location body is decrypted, not just the envelope timestamp. Without `poll`,
  `report_count` is capped at the ~8-report tail — treat it as a floor.
- `<cell>/timeseries.csv` — every retained observation flattened to one row
  (`mid`, `payload`, `timestamp`, `propagation_latency_s`, `latitude`, `longitude`,
  `horizontal_accuracy`, `confidence`, `status`, `id`), ready to plot. Most useful
  with polling on.
- `<cell>/uid.hex` — the cell's UID, for re-fetching it later.
- `density.csv` / `density.log` — the background density time-series (one row per
  snapshot: `timestamp`, `apple_total`, `finders`, `beacons`) and the scanner's
  own log. Join `finders` to each window by nearest timestamp.

Useful flags: `--no-flash` (capture a run already on the board), `--no-fetch`
(capture only, skip the detection/analysis stage), `--no-poll` (single post-run
sweep instead of the continuous poller), `--deliver-window-s N` (deliverability
window for the offline analysis), `--dry-run`. Re-derive the metrics from an
existing run's series with `analyze.py results/<timestamp> [--deliver-window-s N]`.

### Resilience (unattended runs)

Cells are fault-isolated. If a cell fails — a transient flash/USB glitch or a
network blip during fetch — it is **deferred**, and the harness comes back to it
in a **retry pass after all other cells finish** (a fresh UID and reflash on the
retry). A cell that fails twice is given up on: its `<cell>/error.txt` records the
error and the run continues. `summary.csv` stays in matrix order regardless of
which cells came through the retry pass.

### Metrics

For a cell of `N` windows (1 octet each):

- **deliverability** = windows with a relay report within `deliver_window_s` of
  their send time / `N` (unset window = ever observed). Because this is a
  query-time definition over the detection series, it can be re-derived with a
  different window by re-running `analyze.py` — no re-run of the experiment.
- **correctness** = windows whose recovered byte equals the expected byte / `N`.
- **offered throughput** = `N` bytes / send-duration.
- **delivered throughput** = delivered bytes / send-duration.
- **discovery latency** = per window, the first relay report's observation
  timestamp minus when we put that window on air (host wall-clock). This is the
  time until a passing device first *saw* the beacon — the crowd-density signal.
  It comes straight from the report's own timestamp (no polling). Windows that
  were never seen are **censored** (excluded from the latency stats, not counted
  as infinitely slow); they still count against deliverability.
- **report volume** = how many times the finder network actually observed each
  delivered window (`reports_total` over the cell; `reports_per_delivered_*`
  across seen windows). Distinct from deliverability, which is binary per window;
  this counts the redundant re-observations and is a coverage/density signal —
  most useful for the static soak, where one persistent key accumulates reports
  over hours. Each observation is retained in full in `result.json` (timestamp +
  decrypted location), so the raw time-series is available, not just the count.

The relay only files a report roughly once a minute regardless of how fast you
advertise, so update intervals much shorter than ~60 s will show poor
deliverability by construction — that ceiling is the thing these experiments
measure, not a firmware limitation.

Two latencies exist and `espbench` now measures both. **Discovery** (tx →
observed) comes free from report timestamps and tracks crowd density.
**Propagation** (observed → queryable on the server) is the ingestion delay: it
is the detection series' `first_fetched_at` − observation timestamp. Because the
poller keeps fetching each key (enqueued at its send time) long after its window
ends, that first-seen stamp is when the report *truly* became queryable — not
when some later sweep happened to look. It is mostly density-independent (Apple's
pipeline), so treat it as instrumentation and *not* a variable in the
advertising-interval regression. Resolution is bounded by the poll cadence
(measured to ±one `poll_interval_s`, biased slightly high).

Because the discovery latency is derived from the report's own observation time,
`settle_seconds` (the post-run queue-drain cap) only needs to be long enough that
*some* report has reached the server before the poller stops — it does not bias
the latency value. Reports keep arriving for minutes and the relay retains them
for seven days, so under-settled cells can always be re-fetched later with
`fetch_reports.py` or a fresh `analyze.py`.

## Manual receiver

`scripts/fetch_reports.py` recovers a mid range by hand, independent of the
harness (the relay keeps reports for seven days), and prints each window's report
count, first/last-seen timestamps, and a sample decrypted location. It reads the
project `uid.hex`, so to re-fetch a specific harness cell first copy that cell's
saved UID into place:

```sh
cp scripts/results/<timestamp>/<cell>/uid.hex uid.hex
scripts/.venv/bin/python scripts/fetch_reports.py --mid-base 0 --count 16
```

`scripts/scan_findmy.py` is the same local BLE sanity-check scanner as in
`espsend`: confirm the board is actually broadcasting before waiting on the relay
servers (run on Linux/BlueZ for correct key bytes; macOS hides the MAC).

## Notes

- After a run's `done` marker the *last* window's carrier keeps advertising until
  reboot. This is harmless; the harness has already recorded the run.
- `uid` is a symmetric secret; anyone holding it can read and forge
  transmissions. Keep `uid.hex` out of version control.

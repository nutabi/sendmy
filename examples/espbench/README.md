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
flashes, resets the board and captures the serial log, waits a settle period for
the relay to file reports, fetches every mid, and computes the statistics.

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
| `settle_seconds` | 120 | wait after a run before fetching, to let relays report |
| `report_floor_skew_s` | 120 | clock-skew slack when rejecting reports older than a window's send time |
| `mid_start` | 0 | first auto-assigned mid |
| `mid_gap` | 100 | spare mids left between cells |
| `poll` | false | fetch reports *during* the run and union them (see below) |
| `poll_interval_s` | 60 | how often to poll when `poll` is on |
| `density` | true | log nearby Apple-device density during the run (`--no-density` to skip) |
| `density_interval_s` | 60 | seconds between density snapshots |
| `density_window_s` | 10 | scan duration per density snapshot |
| `density_rssi_min` | — | if set, only count devices at/above this RSSI (e.g. `-70` for near) |
| `build_dir` | `<project>/build` | shared build directory (incremental) |
| `results_dir` | `scripts/results` | where run artifacts are written |
| `cells` | — | list of experiment cells |

Each cell: `mode`, `adv_interval_ms`, and then `update_interval_ms` + `count`
(rotating modes) or `duration_ms` (static); optional `payload_start`, `seed`,
`name`, `mid_base` (auto-assigned if omitted), and per-cell `poll` /
`poll_interval_s` overrides.

### Polling (beating the report cap)

Apple returns at most **~8 most-recent reports per key** per fetch — a hard
server-side cap (the request already asks for the full 7-day window). So a single
post-run fetch only ever sees a key's recent tail: a 3 h static soak surfaces 8
reports, all from its final seconds, and re-fetching later just shifts the tail
(inflating apparent latency). With `poll: true`, a background thread fetches the
currently-advertising mid every `poll_interval_s` throughout the run and unions
the results by report id, so `report_count` becomes the true observation total
and the static soak yields its real multi-hour time-series. Only the poller talks
to the relay during a cell (serial capture does no network I/O), so there is no
concurrent account access. `--no-poll` forces the classic single-fetch path.

For rotating cells each key is on air for one update interval, so keep
`poll_interval_s` below half of it; static cells hold one key for the whole run,
so any interval accumulates (a coarse interval like 300 s is plenty).

### Output

Under `scripts/results/<timestamp>/`:

- `summary.csv` / `summary.json` — one row per cell with
  `deliverability`, `correctness`, `bytes_sent`, `bytes_delivered`,
  `send_seconds`, offered and delivered throughput in bytes/second, the
  discovery-latency aggregates (`latency_n`, `latency_min_s`, `latency_median_s`,
  `latency_mean_s`, `latency_max_s`), `polled` (whether report counts are true
  totals or a capped tail), and the report-volume aggregates (`reports_total`,
  `reports_per_delivered_mean` / `_median` / `_max`).
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
  (`mid`, `payload`, `timestamp`, `latitude`, `longitude`, `horizontal_accuracy`,
  `confidence`, `status`, `id`), ready to plot. Most useful with polling on.
- `<cell>/uid.hex` — the cell's UID, for re-fetching it later.
- `density.csv` / `density.log` — the background density time-series (one row per
  snapshot: `timestamp`, `apple_total`, `finders`, `beacons`) and the scanner's
  own log. Join `finders` to each window by nearest timestamp.

Useful flags: `--no-flash` (capture a run already on the board), `--no-fetch`
(capture only, skip the relay stage), `--no-poll` (single fetch even if the
matrix enables polling), `--dry-run`.

### Resilience (unattended runs)

Cells are fault-isolated. If a cell fails — a transient flash/USB glitch or a
network blip during fetch — it is **deferred**, and the harness comes back to it
in a **retry pass after all other cells finish** (a fresh UID and reflash on the
retry). A cell that fails twice is given up on: its `<cell>/error.txt` records the
error and the run continues. `summary.csv` stays in matrix order regardless of
which cells came through the retry pass.

### Metrics

For a cell of `N` windows (1 octet each):

- **deliverability** = windows with any relay report / `N`.
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

Two latencies exist; `espbench` measures the first: **discovery** (tx →
observed), which comes free from report timestamps and is what differs between
environments. **Propagation** (observed → queryable on the server) would need
polling and is mostly environment-independent, so it is not measured.

Because the discovery latency is derived from the report's own observation time,
`settle_seconds` only needs to be long enough that *some* report has reached the
server before you fetch — it does not bias the latency value. Reports keep
arriving for minutes and the relay retains them for seven days, so under-settled
cells can always be re-fetched later.

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

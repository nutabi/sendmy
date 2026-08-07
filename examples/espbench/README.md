# `espbench`

`espbench` is an experiment harness for characterising the `sendmy` data
channel: how many bytes it can push through the crowd-sourced BLE relay network
per unit time (**throughput**) and what fraction of what it sends actually comes
back (**deliverability**). It is a sibling of `espsend` — same `sendmy_carrier` +
`sendmy_link` plumbing — but instead of transmitting a live sensor reading it
emits *synthetic, reproducible* payloads whose parameters are swept across an
experiment matrix, and it ships Python tooling that automates the whole loop.

## What it does

The board is flashed **once** with a generic, reconfigurable firmware. On boot it
brings up NimBLE, prints a `ready` marker, and then loops reading one-line
`run key=val ...` commands from the console UART — the host pushes each matrix
cell's parameters (including a fresh 32-byte Unilink ID, `uid`) over serial and
the device runs that cell, then waits for the next command. Each *window*
advertises a single payload octet by deriving a carrier with
`sm_cr_build_carrier(uid, mid, payload)` and handing it to `sm_ll_set_key`. There
are three payload modes:

| Mode          | Payload per window        | `mid`                    | Purpose |
|---------------|---------------------------|--------------------------|---------|
| `static`      | fixed byte                | one, never rotates       | Does a single unchanging beacon keep being delivered over a long soak? |
| `incremental` | `(start + i) mod 256`     | advances every window    | Deterministic self-checking ramp. |
| `random`      | seeded xorshift32 PRNG    | advances every window    | Pseudo-random payloads; the host reproduces the exact sequence from the seed. |

The two independent variables are the **BLE advertising interval** (`adv_ms`, how
often the radio broadcasts the current key) and, for the rotating modes, the
**update interval** (`upd_ms`, how long each `mid` is held before moving to the
next). A run of the rotating modes sends `count` windows and then prints a `done`
marker; a `static` run holds its one carrier for `dur_ms` and then prints `done`.
These arrive per cell in the `run` command (see [Command protocol](#command-protocol)),
not from a compile-time configuration.

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

## Command protocol

Because the board is flashed once and reconfigured per cell, there are **no**
`CONFIG_ESPBENCH_*` options and no menuconfig menu — every experiment parameter
arrives at runtime in a single newline-terminated command over the console UART:

```
run uid=<64hex> mode=incremental mid=1024 adv_ms=1000 upd_ms=6000 count=30 pay=0
run uid=<64hex> mode=random      mid=2048 adv_ms=1000 upd_ms=6000 count=30 seed=1
run uid=<64hex> mode=static      mid=4096 adv_ms=1000 dur_ms=600000 pay=7
# optional second independent variable on any line:  ... txdbm=9
```

| Field | Required | Meaning |
|-------|----------|---------|
| `uid` | yes | 64 hex chars (32 bytes), the cell's Unilink ID (RAM only) |
| `mode` | yes | `static` / `incremental` / `random` |
| `mid` | yes | mid of the first/only window |
| `adv_ms` | no (1000) | BLE advertising interval, 20–10240 ms |
| `upd_ms` | no (60000) | window length for rotating modes |
| `count` | no (16) | number of windows for rotating modes |
| `dur_ms` | no (600000) | hold time for static (0 = until next command) |
| `pay` | no (0) | constant byte (static) / ramp start (incremental) |
| `seed` | no (1) | 32-bit PRNG seed (random; must be non-zero) |
| `txdbm` | no | override BLE advertising TX power in dBm |

The device replies on the same host-parseable `run start` / `tx ...` / `done ...`
lines as before, then loops back to read the next command. A malformed command is
logged and skipped (the host times that cell out and retries it). `mid` matters
when sweeping a matrix: every carrier is derived from `(uid, mid, payload)`, so
two cells that reuse the same mid range on the same `uid` would broadcast
colliding carriers on the shared relay. The harness assigns each cell a disjoint
mid range **and** a fresh `uid` automatically; you only set `mid` by hand for
standalone runs.

## Build and flash

Targets the Seeed XIAO ESP32-S3 (esp32s3, 8 MB flash). No `uid.hex` or NVS
provisioning is needed — the UID is supplied per cell over serial.

```sh
idf.py build flash monitor
```

To drive it by hand, type a `run ...` command (as above) into the monitor after
the `ready` marker appears. Under the automated harness you never do this;
`run_matrix.py` sends the commands.

## Automated matrix runs

> **Running the current experiment series?** [`RUNPLAN.md`](RUNPLAN.md) is the
> operating document: which matrices to run in what order, the standard
> instrument block that keeps them comparable, the anchor-cell convention, the
> antenna protocol, and the decision gates between nights. Read it before
> starting a run or editing a `matrix.*.json`.

`run_matrix.py` (at the espbench root, beside the `matrix.*.json` definitions)
drives the whole experiment loop. **Launch it from an ESP-IDF-activated shell**
(so `idf.py` is on PATH) with the board attached. It **flashes the generic
firmware once**, resets the board and waits for its `ready` marker, then for each
cell mints a fresh `uid`, sends the `run` command, and captures the serial log —
no reflash or reboot between cells. It records three independent, append-only
**time-series** as the run happens — transmission (what went on air), detection
(what the relay returned), and density (nearby finders) — and joins them into the
delivery metrics *offline* at the end (see
[Three time-series + offline analysis](#three-time-series--offline-analysis)).

```sh
# one-time host setup
python3 -m venv scripts/.venv
scripts/.venv/bin/pip install findmy cryptography pyserial

# see how cells map onto mid ranges without touching hardware
scripts/.venv/bin/python run_matrix.py matrix.smoke.json --dry-run

# run the matrix (first fetch logs in and saves the session to account.json)
scripts/.venv/bin/python run_matrix.py matrix.smoke.json \
    --port /dev/tty.usbmodem1101
```

The low-level helpers (`bench_common.py`, `analyze.py`, `scan_density.py`, and
the manual `fetch_reports.py` / `scan_findmy.py` / `gen_seed.py`) stay under
`scripts/`. The merged-in offline unit tests run with
`scripts/.venv/bin/python run_matrix.py --test` (or `python -m pytest run_matrix.py`).

### Per-cell UIDs and independence

Every cell gets its own random 32-byte `uid`, sent over serial and held only in
RAM on the device, so its carriers are fully independent of every other cell and
of any earlier run: even identical `(mid, payload)` pairs derive different
carriers under different UIDs, so nothing can collide on the shared relay. This is
what lets you re-run the *same* matrix in different places without
cross-contamination. Each cell's UID is saved to `results/<run>/<cell>/uid.hex`
(secret; `results/` is gitignored) so you can re-fetch that cell later. The mid
ranges are still allocated disjointly as a second layer of safety and for
readable logs.

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

A JSON object (see `matrix.smoke.json` at the espbench root; there are several
`matrix.*.json` definitions and the runner takes the path as a CLI argument).
Top-level keys:

| Key | Default | Meaning |
|-----|---------|---------|
| `port` | — | serial port (or pass `--port`) |
| `settle_seconds` | 120 | floor for the post-run poll-queue drain; the actual cap is `max(settle_seconds, fast_timeout_s + sweep_interval_s·(sweep_max_passes+1))` so both tiers get to finish |
| `report_floor_skew_s` | 120 | clock-skew slack when rejecting reports older than a window's send time |
| `mid_start` | 0 | first auto-assigned mid |
| `mid_gap` | 100 | spare mids left between cells |
| `deliver_window_s` | — | offline deliverability window (see below); unset = no upper bound |
| `detection` | — | detection/poll producer config (block; flat fallbacks below) |
| `density` | — | density producer config (block or bool toggle; flat fallbacks below) |
| `build_dir` | `<project>/build` | shared build directory (incremental) |
| `results_dir` | `results` | where run artifacts are written (espbench root) |
| `cells` | — | list of experiment cells (the transmission producer) |

The **`detection`** block (with backward-compatible top-level fallbacks):

| Key | Default | Meaning |
|-----|---------|---------|
| `poll` | false | run the continuous detection poller (`--no-poll` disables it) |
| `poll_interval_s` | 30 | how often the **fast tier** fetches every queued key |
| `detections_before_remove` | 1 | detections to log per key before dropping it from the fast queue (≤ 0 = unbounded, for a soak) |
| `fast_timeout_s` | = `lost_timeout_s` | fast-tier patience: how long an *unseen* key stays in the fast queue before it is **moved to the slow tier** (falls back to `lost_timeout_s` when unset) |
| `sweep_interval_s` | 900 | **slow tier** sweep cadence (≫ `poll_interval_s`) |
| `sweep_max_passes` | 4 | slow-tier sweeps a key gets before it is dropped; **`0` disables the slow tier** (legacy single-tier behaviour) |
| `lost_timeout_s` | 300 | legacy base patience; kept as the `fast_timeout_s` fallback so old matrices are unchanged |
| `queue_soft_cap` | 8 | fast-queue size under which the full timeout applies; above it the timeout shrinks to bound the fetch rate |

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
2. **Detection** (`detection.csv`) — one row per (key, report) the **fast tier**
   sees (`cell, uid, mid, payload, report_id, obs_timestamp, first_fetched_at`,
   decrypted `latitude/longitude/horizontal_accuracy/confidence/status`), deduped
   by report id. `first_fetched_at` is the wall-clock of the *first* fetch that
   returned that id — that minus its observation timestamp is the propagation
   latency.
3. **Deliverability** (`deliverability.csv`) — one row per (key, report) the
   **slow tier** recovers (`cell, uid, mid, payload, report_id, obs_timestamp,
   swept_at`). Present only when the slow tier is enabled. `swept_at` is
   deliberately **not** `first_fetched_at`: a sweep looks long after the key went
   quiet, so its timestamp is not a queryable-latency and is never used as
   propagation. This series extends *deliverability* only.
4. **Density** (`density.csv`) — the nearby-finder time-series (see below).

**The two-tier detection poller.** A single background poller runs for the whole
run, driving two tiers on one thread (so there is never concurrent account
access; only the poller talks to the relay). Each key is enqueued the instant it
starts transmitting (`on_tx`), carrying its own `uid`.

- **Fast tier** — fetches every queued key each `poll_interval_s` and appends new
  reports to `detection.csv` (with `first_fetched_at`, the propagation clock).
  This tier measures **propagation and discovery latency** on fresh keys. A key
  leaves the fast queue when it logs `detections_before_remove` detections, or —
  while still unseen — once it outlives the adaptive `fast_timeout_s` (full value
  while the queue fits `queue_soft_cap`, shrinking as the backlog grows to bound
  the fetch rate). Instead of being *dropped* there, an unseen key is **moved to
  the slow tier**.
- **Slow tier** — a separate list swept every `sweep_interval_s` (≫ the fast
  interval), up to `sweep_max_passes` times. A sweep that finds a report records
  the key as **delivered** in `deliverability.csv` (stamping `swept_at`, never
  `first_fetched_at`) and drops it; a key never found within its passes is
  dropped. This tier answers **deliverability** for slow, weak-signal keys the
  fast tier timed out on — letting the live view converge toward truth without
  ever writing a slow, non-propagation timestamp into `detection.csv`. Set
  `sweep_max_passes: 0` to disable it (keys are dropped at `fast_timeout_s`, the
  legacy single-tier behaviour).

The rule: **deliverability = detection ∪ deliverability** (fast ∪ slow);
**propagation and discovery latency = detection only**. A window delivered *only*
via the slow tier is (correctly) censored from the timing stats. `--no-poll`
swaps the continuous poller for a single post-run sweep per key (deliverability
is still complete; propagation is not meaningful).

To exercise both tiers on hardware, run the smoke matrix (~15–20 min; it uses a
short `sweep_interval_s` so the slow tier engages within the run):

```sh
scripts/.venv/bin/python run_matrix.py matrix.twotier_smoke.json --port /dev/tty.usbmodemXXXX
```

Confirm afterwards that `detection.csv` carries `first_fetched_at` (propagation),
`deliverability.csv` carries `swept_at` (never `first_fetched_at`), and
`analyze.py` reports deliverability as the union of the two.

> **Note — even the two-tier live counts are not the authoritative
> deliverability.** The slow tier makes the live view converge much closer to
> truth, but it is *bounded* (`sweep_max_passes` × `sweep_interval_s`), so a
> report that only becomes queryable hours later — the weak-signal propagation
> tail — is still missed live. For the authoritative figure, take deliverability
> from the offline **resweep** (below), which has no cap and can be re-run for
> days. Fast-tier `detection.csv` counts *alone* under-report badly under backlog
> and must never be read as deliverability (see
> [Experimental results](#experimental-results)).

Apple caps a single fetch at **~8 most-recent reports per key**, so a soak's true
multi-hour history is only obtainable by polling over time and unioning by id.
Set `detections_before_remove: 0` (unbounded) with a large `lost_timeout_s` to
keep a static key in the queue for the whole soak.

**Offline analysis.** At the end of a run — and re-runnable any time with
`analyze.py results/<run> [--deliver-window-s N]` — the three series are
joined into `summary.csv` / `summary.json` and each `<cell>/result.json` +
`<cell>/timeseries.csv`. Those are *derived artifacts*; the CSV series are the
ground truth. `deliver_window_s` is the query-time deliverability definition: a
window counts as delivered only if its expected payload was observed within that
many seconds of its send time (unset = ever observed).

**Ground-truth resweep.** For authoritative deliverability independent of the
live poller, `resweep.py results/<run>` re-fetches every transmitted key
straight from the relay (no queue cap, no adaptive timeout) and writes
`cell,delivered,total`. It is re-runnable — a later sweep picks up
slow-propagating weak-signal reports the first missed — and `--index LO-HI`
resweeps a subset (e.g. to recover the tail of a run that lost the board partway).
Feed the CSV(s) to `analyze2d.py --resweep a.csv [--resweep b.csv] --final` for
the TX × rotation surface and cell-level significance tests; passing several CSVs
combines runs, with a later CSV overriding an earlier one per cell.

### Output

Each run writes to `results/<label>_<UTC-timestamp>/` (at the espbench root) —
e.g. `results/txpower_rotation_20260717T143000Z/`. The label defaults to the
matrix filename with the `matrix.`/`.json` stripped, or the matrix's own `label`
field, or `--label NAME` on the command line; the timestamp keeps repeat runs of
the same matrix grouped and distinct. Inside that directory:

- `transmission.csv` / `detection.csv` / `deliverability.csv` — the secret series
  (they carry `uid` and decrypted GPS), gitignored. `detection.csv` is the fast
  tier (propagation), `deliverability.csv` the slow tier (present only when the
  slow tier is enabled). `cells.json` — the run config plus each cell's
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
window for the offline analysis), `--label NAME` (name the run directory),
`--dry-run`. Re-derive the metrics from an existing run's series with
`analyze.py results/<run> [--deliver-window-s N]`.

### Resilience (unattended runs)

Cells are fault-isolated. If a cell fails — a transient USB/serial glitch or a
network blip during fetch — it is **deferred**, and the harness comes back to it
in a **retry pass after all other cells finish** (a fresh UID, no reflash — the
board stays up the whole run). A cell that fails twice is given up on: its
`<cell>/error.txt` records the error and the run continues. `summary.csv` stays in
matrix order regardless of which cells came through the retry pass.

### Metrics

For a cell of `N` windows (1 octet each):

- **deliverability** = windows with a relay report within `deliver_window_s` of
  their send time / `N` (unset window = ever observed), where a window counts if
  it appears in `detection.csv` **or** `deliverability.csv` (fast ∪ slow tier).
  Because this is a query-time definition over the series, it can be re-derived
  with a different window by re-running `analyze.py` — no re-run of the
  experiment. This live figure converges toward, but the **authoritative**
  deliverability remains the offline `resweep.py` (no cap, re-runnable for the
  full propagation tail).
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
the post-run queue-drain (capped at `max(settle_seconds, lost_timeout_s)`) only
needs to run long enough that *some* report has reached the server before the
poller stops — it does not bias the latency value. The drain lasts at least
`lost_timeout_s` so the last cell's key is watched as long as any other rather
than being cut off by a short `settle_seconds`. Reports keep arriving for minutes and the relay retains them
for seven days, so under-settled cells can always be re-fetched later with
`fetch_reports.py` or a fresh `analyze.py`.

## Experimental results

Concrete findings from runs of the harness. Each subsection records the matrix
parameters, what the run was meant to answer, the summary statistics, and the
conclusion.

### Update-interval throughput sweep — the live poller under-reports deliverability

**Parameters.** 120 `incremental` cells, 1 s advertising interval, 20 windows
each, over four update intervals — **u06/u10/u14/u18** = 6/10/14/18 s per key, 30
cells apiece. Detection poller: fetch every 30 s, drop a key after its first
detection, base patience 480 s for an unseen key, soft-cap the queue at 12 keys,
120 s clock-skew slack when rejecting stale reports.

**Goal.** Measure how deliverability varies with the update interval — i.e. does
holding each `mid` on air longer improve the fraction that comes back?

**Statistics.** Across the 790 keys the live poller *did* catch, the propagation
delay (send → first queryable) was min 10 s, **median 129 s, p90 256 s**, max
374 s. The density logger showed **finders present the whole run** (3–9 during
every window, including the ones that came back empty). The live poller logged 0
detections for **18 of the 120 cells**; a clean offline resweep (256-carrier
brute per mid, `since_epoch` = send time, 120 s skew — no queue pressure)
recovered **2388 of 2400 keys**, and every one of those 18 cells came back
~20/20.

| interval | cells | live avg /20 | **true avg /20** | live % | **true %** |
|----------|-------|--------------|------------------|--------|------------|
| u06 (6 s)  | 30 | 6.9 | 19.6 | 34.7% | **98.2%** |
| u10 (10 s) | 30 | 7.4 | 20.0 | 37.2% | **99.8%** |
| u14 (14 s) | 30 | 8.7 | 20.0 | 43.3% | **100%** |
| u18 (18 s) | 30 | 9.0 | 20.0 | 44.8% | **100%** |
| **all**    | 120 | 8.0 | 19.9 | **40.0%** | **99.5%** |

**Result.** True deliverability is ~99.5% and **flat across every update
interval** — at these settings the send path and the finder network deliver
essentially everything, with no interval knee. The live poller's counts (40%)
were a sampling artifact of the adaptive timeout: an unseen key is dropped once
`now − send_time > lost_timeout_s · queue_soft_cap / queue_size`, so under
backlog the effective timeout (queue 40 → 144 s, 60 → 96 s) falls below the real
p90 propagation of 256 s and slow-but-real keys get abandoned before their
reports become queryable. The apparent "longer dwell delivers better" trend
(u06 34.7% → u18 44.8%) is the *inverse* of a delivery effect — it is a
queue-pressure gradient, since u06 packs the most keys per unit time and so
suffers the largest backlog and the worst under-count. **Read deliverability from
an offline sweep (`analyze.py` over the detection series, or a fresh re-fetch),
never from the live poller's detection counts.**

### TX-power × rotation sweep — rotation is everything, TX power is nothing

> **Superseded 2026-08-07 — both halves of this heading are wrong.** A
> controlled TX × dwell run (`RUNPLAN.md` § Results) found **TX power does
> matter**: +9 dBm delivered 96.1% against −24 dBm's 89.0% (p = 0.0009). This
> run saw flatness only because every one of its arms was attenuated
> (−12…−24 dBm), with no full-power reference. And **dwell was flat** in that
> run at both power levels, so the large rotation effect below is most
> plausibly a *broadcasts-per-key* effect: `adv` was pinned at 2000 ms here,
> so 4/8/16 s rotation was simultaneously 2/4/8 broadcasts per key. Read the
> numbers below as sound measurements of a confounded design.


**Parameters.** 120 cells, **2000 ms** advertising interval, 20 windows each,
crossing two independent variables: **TX power** at the low tail of the radio —
**−12/−15/−18/−21/−24 dBm** (−24 dBm is the hardware floor) — against **rotation
interval** (time each `mid` is held on air before the next) at **4/8/16 s**. That
is 5 × 3 = 15 conditions, 8 reps (cells) apiece. Detection was deliberately
generous (fetch every 60 s, drop a key after 1 detection, 600 s base patience,
queue soft-cap 200) so the run needed no live-poller rescue, but deliverability
is still taken from the offline ground-truth resweep.

**Goal.** Emulating a very weak backscatter transmitter: does dialing TX power
down toward the hardware floor degrade deliverability, and how does it trade off
against rotation interval? The `−24 dBm` floor is still far stronger than a real
backscatter reflection, so this probes the *approach* to the weak-signal regime.

**Statistics.** Deliverability is the ground-truth resweep (`resweep.py`), read
with `analyze2d.py --resweep … --final` (cell-level permutation tests, so the
p-values respect the fact that a cell's 20 keys are clustered). A first sweep
right after the run gave 89.1%; a second sweep of the later-finishing cells a few
hours on rose to **90.4% overall (2169/2400 keys)** as slow, weak-signal reports
kept trickling in — the same propagation tail the live poller cannot wait out.

| TX (dBm) | r4 | r8 | r16 | **row** |
|----------|-----|-----|-----|---------|
| −12 | 76.9% | 94.4% | 99.4% | 90.2% |
| −15 | 76.9% | 93.8% | 98.1% | 89.6% |
| −18 | 84.4% | 91.2% | 98.8% | 91.5% |
| −21 | 81.9% | 92.5% | 99.4% | 91.2% |
| −24 | 80.6% | 88.8% | 98.8% | 89.4% |
| **col** | **80.1%** | **92.1%** | **98.9%** | **90.4%** |

**Result.** **Rotation interval dominates and TX power does not matter.** Across
the whole −12 → −24 dBm tail deliverability is flat (row means 89–91%,
permutation **p = 0.94**) — the radio floor is still far too strong to starve the
finder network. Rotation, by contrast, is a large monotonic effect (80.1% → 92.1%
→ 98.9%, **p < 0.0001**): the longer a `mid` stays on air, the more likely a
finder observes it before it rotates away. The two goals pull opposite ways —
**16 s maximizes per-key delivery (~99%)** while **4 s maximizes throughput**
(~11–12 keys/min delivered vs ~3.7 at 16 s, since it cycles 4× as many keys
despite the lower per-key rate). Practical implication: pick rotation for the
deliverability/throughput balance you want, spend TX power freely on power budget,
and note that because even the −24 dBm floor is delivery-flat, finding the real
weak-signal cliff needs sub-floor attenuation (an inline RF attenuator or
characterized shielding), not the PA setting.

### Advertising-interval sweep — duty cycle is nearly free

**Parameters.** 120 `incremental` cells (`matrix.advertising.json`,
`results/advertising_20260717T163332Z`), 20 windows each, over six advertising
intervals — **100/250/500/1000/2000/4000 ms** — at a **fixed 8 s dwell**, so a
window is held the same wall time regardless of how often it is broadcast and the
only thing that varies is the number of broadcasts per key (80 down to 2). 20
cells per level, interleaved in 6-cell blocks so diurnal drift is balanced across
conditions. ~5.4 h of transmit, 2400 keys. Two-tier detection poller: fast tier
every 30 s with 300 s patience, slow sweep every 900 s for up to 4 passes, queue
soft-cap 16. Deliverability is the offline ground-truth resweep (`resweep.py`).

**Goal.** The battery knob. Advertising is the dominant power draw, so how much
deliverability (and discovery latency) does a lower radio duty cycle cost?

**Statistics.** Finders were present throughout (12–28, median 17 over 379
samples). Propagation (send → first queryable) over the 1572 keys the live poller
timed: min 10 s, **median 91 s, p90 185 s**, max 330 s.

| adv (ms) | 100 | 250 | 500 | 1000 | 2000 | 4000 | **all** |
|----------|------|------|------|------|------|------|---------|
| broadcasts/key | 80 | 32 | 16 | 8 | 4 | 2 | — |
| delivered /400 | 400 | 400 | 400 | 400 | 399 | 396 | 2395/2400 |
| **deliverability** | **100%** | **100%** | **100%** | **100%** | **99.8%** | **99.0%** | **99.8%** |

Per-level propagation medians were 73–109 s with p90 152–214 s — no ordering with
`adv_ms`. The worst single cell at any level delivered 19/20.

**Result.** **Duty cycle is nearly free over the whole 100–4000 ms range.** A 40×
reduction in broadcasts per key (80 → 2) costs about one point of deliverability,
and discovery latency does not degrade with it either — even two broadcasts in an
8 s window are enough for a finder in this density to catch the key, so the extra
78 broadcasts at 100 ms buy nothing. Combined with the previous run, the picture
is that **dwell (how long a key is on air), not broadcast rate (how often it
repeats), is what delivery depends on**: spend the power budget on rotation
interval and run the radio as slowly as the protocol allows. The knee is not
inside this sweep — at 4000 ms the curve has only just begun to bend, so locating
it needs a sweep out to the 10240 ms protocol maximum. Secondary result: the
two-tier poller's *live* numbers (100/100/100/100/99.8/98.8%) matched the offline
resweep to within one key, so the queue-pressure under-count that invalidated the
first run's live figures is fixed.

### Static soak — a fixed beacon does not go stale

**Parameters.** 3 `static` cells (`matrix.soak.json`,
`results/soak_20260717T225445Z`), each a single non-rotating carrier held for
**110 min** at a 1 s advertising interval, run sequentially (~5.5 h total,
overnight). The poller was configured with `detections_before_remove = 0`, so
each beacon stays in the fast queue for the whole run and *every* re-observation
is logged rather than just the first — the run's output is a delivery-continuity
time-series, not a single deliverability number. Slow tier off.

**Goal.** Does an unchanging carrier keep being picked up for hours, or does the
network stop reporting it — through caching, de-duplication, or finders ignoring
a beacon they have already seen? This is also the only exercise of the `static`
mode and the unbounded poll path.

**Statistics.** All three beacons delivered, and kept delivering for their entire
transmit window.

| beacon | unique reports | first seen after send | observation span | median gap | p90 gap | max gap |
|--------|----------------|-----------------------|------------------|-----------|---------|---------|
| soak0 | 214 | 34 s | 110 min | 0.42 min | 1.7 min | 3.5 min |
| soak1 | 178 | 4 s | 113 min | 0.33 min | 2.3 min | 4.7 min |
| soak2 | 181 | 74 s | 128 min | 0.50 min | 1.8 min | 4.0 min |

Across all 442 inter-observation gaps: median 0.50 min, p90 2.0 min, max 4.7 min,
and **not one gap exceeded 5 min**. Median horizontal accuracy of the decrypted
fixes was 64–74 m. Ambient density fell from ~18 finders to ~8 over the night,
while the report rate held at roughly 40–70 per 30 min with no matching decline.
(soak2's span exceeds its 110 min hold because it was the last cell: after `done`
the final carrier keeps advertising until the next `run` command — see
[Notes](#notes).)

**Result.** **A stationary unchanging beacon is picked up continuously and does
not go stale.** Reports arrive roughly every 30 s for hours with no decay, no
de-duplication, and no observed refractory behaviour, so the relay treats each
observation of an already-seen carrier as a fresh report. Two practical
consequences: a `static` carrier is a reliable presence/liveness channel with a
worst-case observation gap of ~5 min at this density, and because ~200 reports
accumulate per key over two hours, a receiver polling a static beacon should
expect to page through a large report set rather than a handful. The report rate
also held steady while finder density more than halved, which suggests delivery
saturates well below the density seen here — but that is a two-point observation
from one night, and the density matrix (`matrix.density.json`) is the run that
would actually test it.

## Manual receiver

`scripts/fetch_reports.py` recovers a mid range by hand, independent of the
harness (the relay keeps reports for seven days), and prints each window's report
count, first/last-seen timestamps, and a sample decrypted location. It reads the
project `uid.hex`, so to re-fetch a specific harness cell first copy that cell's
saved UID into place:

```sh
cp results/<run>/<cell>/uid.hex uid.hex
scripts/.venv/bin/python scripts/fetch_reports.py --mid-base 0 --count 16
```

`scripts/scan_findmy.py` is the same local BLE sanity-check scanner as in
`espsend`: confirm the board is actually broadcasting before waiting on the relay
servers (run on Linux/BlueZ for correct key bytes; macOS hides the MAC).

## Notes

- After a run's `done` marker the *last* window's carrier keeps advertising until
  the next `run` command overwrites it (or reboot). This is harmless; the harness
  has already recorded the run, and the disjoint mids keep it from colliding.
- `uid` is a symmetric secret; anyone holding it can read and forge
  transmissions. Keep `uid.hex` out of version control.

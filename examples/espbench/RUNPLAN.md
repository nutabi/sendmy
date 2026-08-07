# espbench run series

Four runs over five nights, designed as one series rather than four experiments.
Each answers the question the previous one raised, and all of them are measured
the same way so their results can be put on the same axes.

## What makes them comparable

Three things are fixed across every matrix still to be run. Changing any of them
mid-series breaks cross-run comparison, so change them only deliberately.

**1. One instrument block.** Every matrix carries this verbatim:

```json
"settle_seconds": 900,
"report_floor_skew_s": 120,
"mid_gap": 100,
"deliver_window_s": null,
"detection": {
  "poll": true, "poll_interval_s": 30, "detections_before_remove": 1,
  "fast_timeout_s": 600, "sweep_interval_s": 900, "sweep_max_passes": 4,
  "queue_soft_cap": 256
},
"density": { "enabled": true, "interval_s": 30, "window_s": 10 }
```

Two of those numbers are the fix for problems the earlier corpus actually hit:

- `fast_timeout_s: 600` — the fast tier truncates the propagation distribution at
  its patience, so runs with different patience produce *incomparable* latency
  distributions. The advertising sweep's apparent 91 s median against the dwell
  factorial's 158 s is that artifact, not a difference in the network. One value
  everywhere makes latency comparable and makes the censoring point a known
  constant.
- `queue_soft_cap: 256` — the effective timeout is
  `fast_timeout · soft_cap / queue_size`, so a small cap silently shortens
  patience exactly when the key rate is highest. That mechanism produced the
  original run's false 40% deliverability and, more mildly, the uneven
  propagation coverage (66–97%) across the dwell factorial's conditions. A cap
  above the largest realistic in-flight queue keeps patience at its nominal value.

Deliverability still comes from `resweep.py` in every case. The live poller is
never the ground truth.

**2. One transmit axis.** Advertising interval scales with dwell to hold
**broadcasts per key at 5** in every matrix that sweeps dwell. Dwell therefore
means the same thing in run 2 as in run 4 as in the completed dwell factorial,
and the dwell axis can be plotted continuously from 1 s to 16 s across runs. The
one exception is run 1, which inherits the original fixed-`adv` design because
it is a replication (see below).

**3. One anchor condition.** Cells named `a###_ref` are the shared reference:
`incremental`, adv 1 s, 8 s dwell, 20 windows, +9 dBm. One opens every run and
one follows every few blocks — about 10% of each run. They are not part of any
matrix's design. They exist so that two nights are compared against a condition
they *share* rather than assumed equivalent, and so a night with unusual ambient
density announces itself instead of quietly biasing the contrast.

The anchor is only a valid reference **within one antenna state** — the same cell
transmitted without the directional antenna is physically a different condition.
So the series has a **baseline state: antenna FITTED**. Runs 1, 3 and 4 and the
`_ant` half of run 2 all sit in it and share one anchor chain; the `_noant` half
is the single deliberate departure, and its anchors are what measure the
antenna's own worth. Run anything else antenna-off and its anchors stop comparing
to the rest of the series.

The anchor is also the density probe's only cell, so run 3 is simultaneously the
deep characterisation of the exact condition the other runs spot-check. Analyse
anchors across runs first; then read each run's own contrast.

## The series

| # | Matrix | Transmit | Answers |
|---|---|---|---|
| 1 | `matrix.txpower_dwell_ant.json` + `_noant.json` | 2.7 h each | Was it dwell or signal strength? Can we leave the ceiling on purpose? |
| 2 | `matrix.throughput.json` | ~6.8 h | Is the instrument sound? |
| 3 | `matrix.density.json` | ~5.3 h | Is the ceiling the channel's or the city's? |
| 4 | `matrix.dwell_low.json` | ~3.3 h | Where is the dwell knee, once we can get below the ceiling? |

Add `settle_seconds` (15 min) to each, and run `matrix.smoke.json` (~15 min)
before every one.

**Run 1 — TX × dwell.** Dwell 2/4/8 s crossed with TX {+9 dBm, −24 dBm}. Resolves
the confound in the old TX × rotation run, every arm of which was attenuated, so
its flat-TX finding covers only the attenuated tail while its large rotation
effect may not be a dwell effect at all. The larger purpose is to establish
attenuation as a **dial for leaving the delivery ceiling**, since no parameter is
measurable while everything delivers.

It leads the series because it carries the most value and depends on the least:
deliverability comes from the offline resweep, which is immune to poller
behaviour, so its headline result does not wait on run 2's verdict. It is also
the harder test of the standard instrument block — ~13 keys/min against run 2's
~5 — so a problem with the larger queue cap surfaces on the first night rather
than the third.

*Antenna protocol.* The directional antenna cannot be switched under program
control, so it is a between-run block. Run both files back to back on one night,
then **reverse the order on a second night**, so antenna state is crossed against
time-of-night instead of confounded with it. The `txdbm` axis inside each run is
interleaved and clean on its own; the anchors measure what the antenna itself is
worth, since the same reference condition runs in both states.

**Run 2 — replication.** Re-asks the project's first question with the fixed
instrument, on deliberately unchanged cells. Success is not a new finding: it is
live and offline deliverability agreeing to within a key or two, and the original
"longer dwell delivers better" trend failing to reappear. It also produces
retained artifacts for a run whose originals were lost. Running it second rather
than first costs little — every run's deliverability comes from the resweep
regardless, so what this validates is the interpretation of the *live* series and
the propagation figures, both of which can be re-read after the fact.

**Run 3 — density.** 120 repeats of the anchor, back to back, ideally across a
busy→quiet transition. Nothing varies but time, so deliverability variation is
attributable to ambient density alone. Turns the anchors scattered through the
other runs into points on a density curve.

**Run 4 — sub-4 s dwell.** Extends run 1's dwell axis down to 1 s on the same
iso-broadcast footing. **Run it at whichever attenuation run 1 puts below the
ceiling** — at full power the dwell factorial already delivered 100% at 4 s, so a
full-power pass is expected to stay flat and answer nothing. Together runs 1 and
4 give dwell from 1 s to 8 s at one power setting. The queue stays inside the
256 cap here: it is fed by the block-average key rate (~17/min, since every block
cycles all five dwell levels), not the instantaneous rate during the 1 s cells,
and it drains at the ~158 s median detection time.

## Operating the series

### Setup

The board is flashed **once** for the whole series — every matrix is pushed over
the console UART at runtime, so there is no reflash between runs.

```sh
idf.py build flash            # once
ls /dev/cu.usbmodem*          # matrices hardcode /dev/cu.usbmodem101; --port overrides
scripts/.venv/bin/python -m pip install -r scripts/requirements.txt
```

`scripts/account.json` holds the relay session. It expires, and re-authentication
can prompt for 2FA — trigger it early in the evening via the smoke run, not at
midnight.

**The invariant that is in no config file: put the board in the same physical
place, in the same orientation, every night, and do not move it mid-run.**
Position, orientation and what sits between it and passing foot traffic are
uncontrolled covariates that no logger captures. The anchor cells can detect that
a night was unusual; they cannot tell you it was unusual because the board moved.

### Before every run

```sh
PY=scripts/.venv/bin/python                      # system python3 lacks pyserial/findmy
export PATH="$PWD/scripts/.venv/bin:$PATH"       # run_matrix calls esptool.py by bare name
$PY run_matrix.py matrix.<name>.json --dry-run   # cell/mid mapping, no hardware
$PY run_matrix.py matrix.smoke.json --no-flash   # ~15-20 min
```

Two environment traps, both confirmed the hard way on 2026-08-07:

- `idf.py` here is a **shell alias** (`idf-env idf.py`), and `run_matrix.py` invokes
  it as a subprocess, where aliases do not exist. Flashing from the harness fails.
  The firmware is generic and reconfigured per cell, so `--no-flash` is the normal
  mode for the series — flash once by hand from an IDF shell if the firmware ever
  changes.
- `esptool.py` is likewise absent unless the venv's `bin` is on `PATH`. It is only
  needed by the **final** step, `park_board`, which halts the board so it stops
  beaconing its last carrier. Without it the run completes and writes
  `summary.csv` normally, then dies in the last few lines — and the board keeps
  advertising that carrier into whatever you do next, including an antenna swap.
  `pip install esptool` into the venv and export the `PATH` above, or pass
  `--no-park` and accept the stray beacon.

The smoke run opens with `a000_ref`, the series anchor, and it should come back
20/20. If it does not, the rig or the site is off — find out before committing a
night to it.

### Per night

| Night | Matrices, in order | Antenna | Wall |
|---|---|---|---|
| 1 | `txpower_dwell_ant` → `txpower_dwell_noant` | fitted → **removed at the swap** | ~5.9 h |
| 2 | `txpower_dwell_noant` → `txpower_dwell_ant` | removed → **refitted at the swap** | ~5.9 h |
| 3 | `throughput` | fitted | ~7.0 h |
| 4 | `density` | fitted | ~5.6 h |
| 5 | `dwell_low` | fitted, unless run 1 says otherwise | ~3.6 h |

Nights 1 and 2 are the same pair in reversed order, and they stay **adjacent** so
the crossover's two halves see similar ambient conditions — do not put another
run between them. You swap the antenna once per night, between the two runs.
Mark its orientation before the first removal and refit it identically, or the
"fitted" anchors on later nights are measuring a different rig.

Watch progress with `scripts/dashboard.py`. After each run:

```sh
$PY scripts/resweep.py results/<run>/    # authoritative deliverability
$PY scripts/analyze.py  results/<run>/
```

### Decision gates

These are the two points where the series can branch, and both are load-bearing.

**After night 2** — read which arm landed off the delivery ceiling and run night 5
at that attenuation. At full power the dwell factorial already delivered 100% at
4 s dwell, so a full-power sub-4 s pass is expected to come back flat and answer
nothing. If *neither* arm leaves the ceiling (possible — −24 dBm with the antenna
removed may still be far too strong), night 5 as written is not worth running,
and the honest next step is physical attenuation: an inline RF attenuator or
characterised shielding, not the PA setting.

**After night 3** — live and offline deliverability must agree to within a key or
two, and the original "longer dwell delivers better" trend must fail to reappear.
If they disagree, the instrument is still lying: **stop before nights 4–5**, and
re-read the *propagation* figures from nights 1–2 with that in mind. Their
deliverability stands either way, since it comes from the offline resweep.

### Do not

- **Change the instrument block** for one run. It is the only reason the runs are
  comparable; a one-off tweak silently un-pools every cross-run comparison.
- **Edit the matrices of completed runs.** They are the parameter record of
  results on disk.
- **Read deliverability off the live poller.** Always `resweep.py`.

### Data

`results/` is **gitignored and not backed up anywhere**. The artifacts of two
earlier runs are already lost, which is why one report figure had to be
transcribed from published numbers instead of regenerated. Copy each run
directory off this machine when it finishes — the JSON and CSV are a couple of MB
per run and cost 3–7 h of wall time to reproduce.

## Results, and what they changed

Kept here because `results/` is gitignored — the run directories hold the full
`FINDINGS.md`, but this is the version-controlled record.

### Run 1 — TX × dwell, antenna fitted (2026-08-07)

`results/txpower_dwell_ant_20260807T043442Z`. 1960 keys, offline resweep.

| | dwell 2 s | 4 s | 8 s | row |
|---|---|---|---|---|
| **+9 dBm** | 96.3% | 94.7% | 97.3% | **96.1%** |
| **−24 dBm** | 90.0% | 88.0% | 89.0% | **89.0%** |

Anchors 159/160 = 99.4%. TX contrast **+7.1 points, p = 0.0009** (cell-clustered
permutation). Dwell flat at both power levels. A 4.9 min network outage at the
start inflates propagation for 4 cells (see the run's `OUTAGE.md`);
deliverability is unaffected.

**TX power matters** — the old "TX power is nothing" was scoped to −12…−24 dBm,
all of it attenuated, and the effect appears as soon as a full-power arm exists.

**Dwell does not** — flat even at −24 dBm, where 11 points of headroom exist for
an effect. The old run's 80/92/99% "rotation effect" held `adv` at 2000 ms, so
its rotation levels were also 2/4/8 **broadcasts per key**; this design holds
broadcasts at 5. The effect the project has attributed to dwell since the
beginning is most plausibly broadcast count. The anchors say the same from inside
the run: same power and dwell, 8 broadcasts → 99.4%, 5 broadcasts → 97.3%.

**Changes this forces:**

- **The standard 5-broadcasts/key axis is not a ceiling condition even at full
  power** (96.1% vs the anchors' 99.4%). Good for sensitivity, but results on this
  axis are *not* directly comparable to the earlier 99.5–99.9% runs, which ran at
  8–80 broadcasts per key. **Compare through the anchors, never directly.**
- **`matrix.dwell_low.json` needs `tx_power_dbm: -24` added before night 5.** It
  currently sets no power, so it would run at the firmware default and sit near
  the ceiling — the flat, uninformative outcome the run is designed to avoid.
  Hold until night 2 confirms −24 dBm beats antenna-off as the operating point.
- **A broadcasts × power matrix is now the highest-value experiment not yet
  written**, since broadcast count has become the leading explanation for the
  project's oldest headline finding.
- Run 1 gives **no** independent live-vs-offline check — they agreed exactly,
  which is expected since the slow sweep does what the resweep does. That
  validation still belongs to night 3.

### Run 1b — TX × dwell, antenna removed (2026-08-07)

`results/txpower_dwell_noant_20260807T090000Z` **plus**
`results/txpower_dwell_noant_resume_20260807T103835Z`. The board dropped off USB
(`Errno 6, Device not configured`) partway through `s050_pp09_d04`; the remaining
43 cells ran from `matrix.txpower_dwell_noant_resume.json` after a replug. Pool
the two directories and **drop the parent's partial `s050_pp09_d04`** — that
gives the full 98 cells / 1960 keys. Offline resweep.

| | dwell 2 s | 4 s | 8 s | row |
|---|---|---|---|---|
| **+9 dBm** | 50.0% | 52.3% | 47.0% | **49.8%** |
| **−24 dBm** | 0.0% | 0.0% | 0.0% | **0.0%** (0/900) |

Anchors 105/160 = **65.6%**, against 99.4% fitted.

**Antenna-off is not an attenuator, it is two different regimes.** At −24 dBm it
is a hard floor — 0 of 900 keys, not one report. At +9 dBm the link survives at
half rate. So the antenna is worth ~46 points at full power and ≥89 points at
−24 dBm; there is no setting at which it behaves like a modest, gradable loss.

**Dwell is flat again** — 50.0/52.3/47.0 within +9 dBm, p = 0.58 (cell-clustered
permutation, 20 000 shuffles), now at *half* delivery where 50 points of headroom
exist. This is the second independent replication that dwell does nothing once
broadcasts-per-key is held constant, and the strongest one, because a ceiling
cannot explain it.

**The anchors replicate the broadcast-count effect too** — 8 broadcasts/key gives
65.6% against the standard axis's 49.8% at identical power and dwell, a 16-point
gap in the same direction as run 1's 2-point gap, and much larger away from the
ceiling.

**Changes this forces:**

- **Night 2's reversed-order repeat is no longer worth running as designed.** It
  existed to separate the antenna effect from time-of-night; with a 0/900 arm and
  BLE density matched across the two nights (mean finders 10.0 vs 11.1), there is
  no plausible diurnal confound of that size. Prefer spending the slot on the
  broadcasts × power matrix.
- **Night 5 runs at −24 dBm with the antenna FITTED.** Antenna-off at −24 dBm is
  a dead link and antenna-off at +9 dBm sits at 50%, which is usable but couples
  the attenuation to a physical step that cannot be interleaved. Add
  `tx_power_dbm: -24` to `matrix.dwell_low.json`; the decision gate is closed.
- **The live poller undercounted by 2× again** — it read ~25% for the +9 dBm arm
  where the resweep says 49.8%. The parent run's poller was killed mid-drain,
  which explains part of it, but this is the same failure mode as before. The
  standing rule holds without exception: deliverability comes from `resweep.py`.
- **Physical handling of the board is a run hazard.** Run 1 ran 2 h 43 min
  untouched; run 1b lost USB 1 h 10 min after the antenna was handled. Any run
  requiring a physical change should re-seat and verify the connector before
  starting, and the health monitor must watch for `Device not configured` — the
  first monitor had no pattern for it and missed the real failure while firing a
  false one.

## Retained from the earlier corpus

`matrix.advertising.json`, `matrix.dwell_isobroadcast.json` and
`matrix.soak.json` are kept **unmodified**, with their original instrument
blocks, because they are the parameter record of runs whose results are in
`results/`. Editing them to match the new standard would falsify that record.
Their results remain valid for deliverability; their propagation distributions
are censored at their own patience and should not be pooled with the new runs'.

Removed as superseded: the single-tier smoke matrix (replaced by the two-tier
one, now `matrix.smoke.json`), `matrix.propagation.json` (never run; every run
now yields propagation from its fast tier), and `matrix.txpower_rotation.json`
(its design is what run 2 replaces).

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
| 1 | `matrix.throughput.json` | ~6.8 h | Is the instrument sound? |
| 2 | `matrix.txpower_dwell_ant.json` + `_noant.json` | 2.7 h each | Was it dwell or signal strength? Can we leave the ceiling on purpose? |
| 3 | `matrix.density.json` | ~5.3 h | Is the ceiling the channel's or the city's? |
| 4 | `matrix.dwell_low.json` | ~3.3 h | Where is the dwell knee, once we can get below the ceiling? |

Add `settle_seconds` (15 min) to each, and run `matrix.smoke.json` (~15 min)
before every one.

**Run 1 — replication.** Re-asks the project's first question with the fixed
instrument, on deliberately unchanged cells. Success is not a new finding: it is
live and offline deliverability agreeing to within a key or two, and the original
"longer dwell delivers better" trend failing to reappear. That agreement is what
licenses trusting runs 2–4. It also produces retained artifacts for a run whose
originals were lost.

**Run 2 — TX × dwell.** Dwell 2/4/8 s crossed with TX {+9 dBm, −24 dBm}. Resolves
the confound in the old TX × rotation run, every arm of which was attenuated, so
its flat-TX finding covers only the attenuated tail while its large rotation
effect may not be a dwell effect at all. The larger purpose is to establish
attenuation as a **dial for leaving the delivery ceiling**, since no parameter is
measurable while everything delivers.

*Antenna protocol.* The directional antenna cannot be switched under program
control, so it is a between-run block. Run both files back to back on one night,
then **reverse the order on a second night**, so antenna state is crossed against
time-of-night instead of confounded with it. The `txdbm` axis inside each run is
interleaved and clean on its own; the anchors measure what the antenna itself is
worth, since the same reference condition runs in both states.

**Run 3 — density.** 120 repeats of the anchor, back to back, ideally across a
busy→quiet transition. Nothing varies but time, so deliverability variation is
attributable to ambient density alone. Turns the anchors scattered through runs
1, 2 and 4 into points on a density curve.

**Run 4 — sub-4 s dwell.** Extends run 2's dwell axis down to 1 s on the same
iso-broadcast footing. **Run it at whichever attenuation run 2 puts below the
ceiling** — at full power the dwell factorial already delivered 100% at 4 s, so a
full-power pass is expected to stay flat and answer nothing. Together runs 2 and
4 give dwell from 1 s to 8 s at one power setting. Note the key rate at 1 s dwell
(~60/min) exceeds even the 256 cap, so propagation coverage drops at the
short-dwell levels; deliverability is unaffected.

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
PY=scripts/.venv/bin/python      # system python3 lacks pyserial/findmy
$PY run_matrix.py matrix.<name>.json --dry-run   # cell/mid mapping, no hardware
$PY run_matrix.py matrix.smoke.json              # ~15-20 min
```

The smoke run opens with `a000_ref`, the series anchor, and it should come back
20/20. If it does not, the rig or the site is off — find out before committing a
night to it.

### Per night

| Night | Matrices, in order | Wall |
|---|---|---|
| 1 | `throughput` | ~7.0 h |
| 2 | `txpower_dwell_ant` → `txpower_dwell_noant` | ~5.9 h |
| 3 | `txpower_dwell_noant` → `txpower_dwell_ant` | ~5.9 h |
| 4 | `density` | ~5.6 h |
| 5 | `dwell_low` | ~3.6 h |

Nights 2 and 3 are the same pair in reversed order; you swap the antenna once,
between the two runs. Watch progress with `scripts/dashboard.py`. After each run:

```sh
$PY scripts/resweep.py results/<run>/    # authoritative deliverability
$PY scripts/analyze.py  results/<run>/
```

### Decision gates

These are the two points where the series can branch, and both are load-bearing.

**After night 1** — live and offline deliverability must agree to within a key or
two, and the original "longer dwell delivers better" trend must fail to reappear.
If they disagree, **stop**: the instrument is still lying and nights 2–5 would
inherit the same bias. That agreement, not any new number, is night 1's product.

**After night 3** — read which arm landed off the delivery ceiling and run night 5
at that attenuation. At full power the dwell factorial already delivered 100% at
4 s dwell, so a full-power sub-4 s pass is expected to come back flat and answer
nothing. If *neither* arm leaves the ceiling (possible — −24 dBm with the antenna
removed may still be far too strong), night 5 as written is not worth running,
and the honest next step is physical attenuation: an inline RF attenuator or
characterised shielding, not the PA setting.

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

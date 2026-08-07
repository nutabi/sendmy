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

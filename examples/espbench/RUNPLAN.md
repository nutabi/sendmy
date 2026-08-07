# espbench run series

A series rather than a set of experiments: each run answers the question the
previous one raised, and all are measured the same way so their results sit on
the same axes. Runs 1, 1b, 2 and 3 are complete (all on 2026-08-07/08); run 4
remains, and needs a location the others did not.

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
**broadcasts per key at 5** in every matrix that sweeps dwell, so dwell means the
same thing across runs and can be plotted continuously from 1 s to 8 s.

*Run 3 exposed the cost of this convention.* Holding broadcasts constant forces
`adv = dwell/5`, which makes `adv` and dwell perfectly confounded inside any
dwell sweep — and run 3 found a 22.6-point penalty that tracks `adv` (600 and
1200 ms bad; 200, 400, 800 ms fine) rather than dwell. The convention is still
right for isolating dwell from broadcast count, but **`adv` is now a known latent
covariate in every dwell sweep**, and a fine `adv` sweep at fixed dwell and fixed
broadcasts is needed to characterise it.

**3. One anchor condition.** Cells named `a###_ref` are the shared reference:
`incremental`, adv 1 s, 8 s dwell, 20 windows, +9 dBm. One opens every run and
one follows every few blocks — about 10% of each run. They are not part of any
matrix's design. They exist so that two nights are compared against a condition
they *share* rather than assumed equivalent, and so a night with unusual ambient
density announces itself instead of quietly biasing the contrast.

The anchor is only a valid reference **within one antenna state** — the same cell
transmitted without the directional antenna is physically a different condition.
So the series has a **baseline state: antenna FITTED**. Runs 1, 2, 3 and 4 all sit
in it and share one anchor chain; run 1b is the single deliberate departure, and
its anchors are what measured the antenna's own worth. Run anything else
antenna-off and its anchors stop comparing to the rest of the series.

**The anchor design is validated.** It read 99.4% in run 1, 99.4% in run 2 and
100.0% in run 3 — three runs, two cables, one hardware failure and a full day of
elapsed time between them. That reproducibility is what licenses pooling nights,
and it is what let run 3 attribute a 22-point drop to the transmit parameters
rather than to the night. Analyse anchors across runs first; then read each run's
own contrast.

## The series

| # | Matrix | Transmit | Answers | Status |
|---|---|---|---|---|
| 1 | `matrix.txpower_dwell_ant.json` | 2.7 h | Was it dwell or signal strength? | **done** 2026-08-07 |
| 1b | `matrix.txpower_dwell_noant.json` (+ `_resume`) | 2.7 h | Can antenna removal serve as an attenuation dial? | **done** — answer: no |
| 2 | `matrix.broadcasts.json` (+ `_resume`) | 2.8 h | Dwell or broadcast count? | **done** — answer: broadcast count |
| 3 | `matrix.dwell_low.json` | 4.5 h | Is there a dwell floor below 4 s? | **done** — answer: no, but `adv` bands found |
| 4 | `matrix.density.json` | ~6 h | Does the broadcast rule hold as the city empties? |  |
| — | `matrix.throughput.json` | ~6.8 h | **Dropped** — see below |  |

Numbering follows what was actually run, not the original plan. Add
`settle_seconds` (15 min) to each, and run `matrix.smoke.json` (~15 min) before
every one.

**Run 1 — TX × dwell.** Dwell 2/4/8 s crossed with TX {+9 dBm, −24 dBm}. Resolved
the confound in the old TX × rotation run, every arm of which was attenuated, so
its flat-TX finding covered only the attenuated tail. Led the series because it
depended on the least: deliverability comes from the offline resweep, which is
immune to poller behaviour.

**Run 1b — the same design with the antenna removed**, intended to establish
attenuation as a second dial for leaving the delivery ceiling. *The antenna
protocol is retired.* It called for running both states back to back and then
reversing the order on a second night, so antenna state crossed time-of-night.
That was never needed: antenna-off turned out to be a regime switch, not a dial
(0/900 keys at −24 dBm), and matched BLE density across the two nights ruled out
the diurnal confound directly. **Use TX power with the antenna fitted.**

**Run 2 — broadcasts × TX power.** Dwell pinned at 4000 ms, broadcasts varied
1/2/4/8/16 via `adv`, crossed with TX {+9, −24} dBm. The complement of runs 1
and 1b, and the run that closed the project's central question.

**Run 3 — sub-4 s dwell.** Extends run 1's dwell axis down to 1 s on the same
iso-broadcast footing (5 broadcasts/key, −24 dBm), which run 2 places near 87% —
on the steep part of the curve with headroom both ways. Now that dwell is known
to do nothing between 2 and 8 s, the open question is narrower and more physical:
**relay devices scan intermittently**, so below some dwell a key can be missed
entirely no matter how often it is broadcast inside the window. Run 2 cannot see
this — it pinned dwell at 4000 ms throughout. The queue stays inside the 256 cap:
it is fed by the block-average key rate (~17/min, since every block cycles all
five dwell levels), not the instantaneous rate during the 1 s cells.

**Run 4 — density.** Ideally across a busy→quiet transition, with nothing varying
but time, so deliverability variation is attributable to ambient density alone.
**The original design is void and has been rebuilt.** It was 120 repeats of the
anchor — 8 broadcasts at +9 dBm — which run 2 measured at 100.0%; every cell
would have delivered fully however quiet the night got, and the run would have
produced a flat line. An anchor is chosen to be *stable*, which is the right
property for a reference and exactly the wrong one for a probe. The rebuilt
matrix keeps the anchor as the stability reference and adds two **fragile**
probes at 4 and 2 broadcasts, −24 dBm (83.3% and 66.1% in run 2), where ambient
change has room to move the number in both directions.

This is now the main external-validity test of everything the series has found:
whether "≥8 broadcasts per key" is a general rule or a rule about this location
at this hour.

**Dropped — `matrix.throughput.json`.** It asked two things. The "does the old
longer-dwell-delivers-better trend reappear" half is answered three times over,
and its own design reproduces the original confound: `adv` pinned at 1000 ms
means its dwell levels 6/10/14/18 s *are* 6/10/14/18 broadcasts per key. The
live-vs-offline agreement half is still worth checking and got more urgent, not
less — but that is a free comparison on any run, not a 6.8 h experiment. Compare
`detection.csv` against `resweep.csv` on each run instead.

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

| Night | Matrices, in order | Antenna | Wall | |
|---|---|---|---|---|
| 1 | `txpower_dwell_ant` → `txpower_dwell_noant` (+ `_resume`) → `broadcasts` (+ `_resume`) → `dwell_low` | fitted → removed → refitted | ~18 h | **done** 2026-08-07/08 |
| 2 | `density` | fitted | ~6.5 h | needs a location with real footfall — see below |

Everything through run 3 landed on one long night. The planned crossover
(nights 1 and 2 as the same pair reversed) was **abandoned**: run 1b showed
antenna-off is a regime switch rather than a dial, and matched BLE density across
the two halves ruled out the diurnal confound directly, so the second night would
have bought nothing. If the antenna ever comes off again, mark its orientation
first and refit it identically, or the "fitted" anchors stop describing the same
rig.

**`density` needs a different location.** Ambient density at the spot used for
runs 1–3 barely moves: per-run mean finders were 11.1 / 11.7 / 10.6 / 13.2 / 13.5
across 12:34 → 02:07, while within-hour scatter ran 3–23. The noise is several
times the diurnal signal, so a time-based density run there produces a flat line
whatever the probes are set to — the same failure the rebuilt matrix was meant to
avoid, arriving by a different route. Scout candidates first, without the board:

```sh
$PY scripts/scan_density.py --watch --interval 60 --out /tmp/scout.csv
```

Look for transient footfall (canteen, lecture-theatre exit, MRT concourse,
library entrance) rather than a residence or office, where the same phones are
present at 03:00 and 15:00. The size of the *transition* matters more than the
absolute level. Moving location confounds the anchor's cross-run role, which is
acceptable here because the question is entirely internal — probes falling while
the anchor holds — but record the location in the matrix `_note`, since nothing
in the harness captures it.

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

### Run 2 — broadcasts × TX power, antenna fitted (2026-08-07/08)

`results/broadcasts_20260807T133206Z` **plus**
`results/broadcasts_resume_20260807T142927Z` (a second USB dropout, at
`s032_pn24_b04`; drop the parent's 1-window `s032`). 99 cells / 1980 keys,
dwell pinned at 4000 ms, offline resweep.

| broadcasts/key | 1 | 2 | 4 | 8 | 16 |
|---|---|---|---|---|---|
| **+9 dBm** | 86.1% | 98.3% | 96.7% | 100.0% | 100.0% |
| **−24 dBm** | 42.8% | 66.1% | 83.3% | 97.2% | 100.0% |

Anchors 179/180 = **99.4%**, identical to run 1's 99.4%.

**Broadcast count is the driver. This is the answer to the confound.** Over
1→8 broadcasts at fixed dwell, delivery rises **+18.1 points per doubling at
−24 dBm** and **+4.0 at +9 dBm**, both **p < 0.0001** (cell-clustered
permutation on log2 broadcasts, 20 000 shuffles). Runs 1 and 1b moved dwell with
broadcasts pinned and found nothing, twice. This run moved broadcasts with dwell
pinned and found a 42.8% → 97.2% climb. The factor the project attributed to
dwell for its entire history is broadcast count.

**The old "rotation" result reproduces from broadcast count alone.** That run
read 80/92/99% at 4/8/16 s dwell with `adv` pinned at 2000 ms — i.e. 2/4/8
broadcasts per key. This run's −24 dBm arm at 2/4/8 broadcasts reads
**66/83/97%**: the same curve and the same shape, off only by the power offset
between the two attenuation settings. Dwell was never doing the work.

**Power sets where the curve saturates, not whether it rises.** +9 dBm is
already at 98% by 2 broadcasts and pinned at 100% from 8; −24 dBm needs 8 to
reach 97% and 16 to reach 100%. TX power and broadcast count trade off against
each other — that is the practical design space for the protocol.

**Changes this forces:**

- **≥8 broadcasts per key is the safe operating point**, and 16 buys full
  delivery even at the −24 dBm floor. The standard 5-broadcast axis used in runs
  1/1b sits on the steep part of the curve at low power, which is exactly why
  those runs had headroom — that was luck, not design.
- **The anchor chain validates the series.** 99.4% here against 99.4% in run 1,
  on a different night with a different cable, means the two nights' networks
  were equivalent and cross-run comparison is sound.
- **The live poller's "b16 dip" was pure artifact.** Live data showed −24 dBm
  b16 at 58.3%, driven by one cell reading 1/20; the resweep puts it at 100%.
  Never read a shape off the live poller.
- **`matrix.dwell_low.json` is still worth running** — at 5 broadcasts and
  −24 dBm it sits near 90%, with real headroom — but it is now a *secondary*
  question. Whether sub-4 s dwell hurts is only interesting because dwell
  otherwise does nothing.

### Run 3 — sub-4 s dwell at −24 dBm (2026-08-08)

`results/dwell_low_20260807T180544Z`. 165 cells / 3300 keys, 5 broadcasts per key
throughout (`adv = dwell/5`), antenna fitted, offline resweep. Ran 02:05–06:39
with **no errors and no USB dropout** — the cable swap holds.

| dwell | 1000 ms | 2000 ms | 3000 ms | 4000 ms | 6000 ms |
|---|---|---|---|---|---|
| (adv) | 200 ms | 400 ms | 600 ms | 800 ms | 1200 ms |
| delivered | **91.7%** | 90.2% | **65.0%** | 90.2% | **71.2%** |

Anchors 300/300 = **100.0%**.

**There is no short-dwell floor. The hypothesis this run was built on is dead.**
1 s dwell delivers 91.7% — the *highest* of any level. If relay scan duty cycle
imposed a floor, 1 s would be the worst cell in the matrix; it is the best. Dwell
between 1 s and 6 s does nothing, which now covers 1–8 s across runs 1 and 3.

**But the result is not flat — it is non-monotone, and the split is by `adv`,
not dwell.** Group the levels by advertising interval and the anomaly is exact:

| adv | rate | cells |
|---|---|---|
| 200 / 400 / 800 ms | **90.7%** | 90 |
| 600 / 1200 ms | **68.1%** | 60 |

**+22.6 points, p < 0.0001** (cell-clustered permutation, 20 000 shuffles). The
depression is systematic, not a few bad cells: every d3000 cell lands between
10 and 18 of 20 and every d6000 cell between 11 and 17, while the clean levels
cluster 16–20. It is not drift — first half 82.1% against second half 81.2%,
and all 15 anchors read a perfect 20/20 across the whole night.

*Do not quote a dwell slope for this run.* A regression on log dwell returns
−0.071 per doubling at p < 0.0001, but the relationship is not ordered in dwell
(1000 and 4000 are both fine, 3000 and 6000 are both bad), so that number
describes nothing real.

**Leading hypothesis: BLE channel aliasing.** Advertising events rotate across
channels 37/38/39 while scanners dwell on one channel at a time. When the
advertising interval resonates with a scanner's hop period, a scanner can land
on the wrong channel repeatedly. The spec's mandated 0–10 ms random delay per
advertising event exists to break exactly this, but with only 5 broadcasts per
key there is little opportunity to average out. This is a hypothesis, not a
finding.

**Caveat that blocks a stronger claim:** this design pins `adv = dwell/5`, so
`adv` and dwell are perfectly confounded within it. Attributing the effect to
`adv` rests on the *shape* — dwell admits no ordering that produces
fine/fine/bad/fine/bad, while `adv` splits it cleanly — not on independent
evidence. Run 2 offers no cross-check: it used adv 250/500/1000/2000/4000 and
never visited 600 or 1200 ms.

**Changes this forces:**

- **The highest-value experiment now written is a fine `adv` sweep at fixed
  dwell and fixed broadcasts** — e.g. adv 200…1400 ms in 100 ms steps, dwell
  pinned, broadcasts pinned — to separate `adv` from dwell and map where the
  penalty bands sit. If it reproduces, it is a genuine protocol-design finding:
  some advertising intervals are simply bad, independent of how often you send.
- **Nothing in the series so far controlled `adv` independently.** Runs 1 and 1b
  scaled it with dwell to hold broadcasts constant; run 2 varied it *as* the
  broadcast axis. If certain `adv` values carry a penalty, it is a latent
  covariate in every result to date — though it cannot explain run 2's headline,
  since that curve is monotone across five `adv` values and 55 points deep.
- **The ≥8 broadcasts recommendation stands** but should be stated as
  "≥8 broadcasts per key, avoiding advertising intervals near 600 and 1200 ms
  pending the sweep."

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

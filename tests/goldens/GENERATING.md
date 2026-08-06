# Generating the golden bundle

**Bundle version:** v0.9 · **Binds to:** arXiv v1

Goldens are the only part of the acceptance harness that cannot be produced without a GPU and a
running CARLA. Everything else in `tests/` runs anywhere. This file is the procedure.

Budget: **9 routes × 3 replicates ≈ 1–2 GPU-hours** with PDM-Lite. In the published seed-42
sweep these nine routes took 8–29 s of simulation each; almost all the wall-clock is CARLA
start-up and map loading.

---

## 0. Why the reference agent is PDM-Lite

PDM-Lite is a privileged planner: it reads ground-truth state instead of pixels. That matters
here for one reason — **its behaviour barely moves between runs**, so the run-to-run spread the
tolerance is derived from reflects the simulator, not the model. A perception model's own
variance would swamp it and the tolerance would have to be so wide it never caught anything.

It has a second, sharper property. A privileged planner still *reacts to the OOD actor*, because
the actor is in the ground-truth state it reads. So a route where the prop failed to spawn does
not merely score differently — the TTR/DAR criterion has no actor to hold, and assertion A1 goes
red immediately. That is the failure this whole directory exists for.

Any deterministic reference agent works. If you use a different one, say so in
`--reference-agent`; the bundle records it and `check_acceptance.py` prints it.

---

## 1. Prerequisites

| | |
|---|---|
| CARLA | 0.9.15, the packaged build you will run the benchmark with |
| Content pack | the v0.9 pack installed into that build — see `../../assets/INSTALL.md` |
| Overlay | `setup.sh` run against the pinned upstream SHAs |
| Agent | PDM-Lite, reachable through a runner config |
| GPU | one is enough; the replicates are sequential by design |

The content pack matters more than any other line in that table. **A golden generated against a
build with a missing asset is worse than no golden**: it pins the broken value, and from then on
the acceptance test certifies the breakage. Step 3 exists to make that impossible.

---

## 2. Materialise the split

```bash
cd tests
python3 smoke/materialize.py --out /scratch/smoke_routes
```

This copies the nine route XMLs out of the frozen `routes/` tree, verifying each one's sha256
against the split first, and writes a `MANIFEST.tsv` next to them. Use `--tier core` for the
six-route subset; the bundle records which tier it covers and the harness refuses to compare
across tiers.

---

## 3. Probe the blueprints — **do not skip this**

```bash
# start CARLA first; the probe does not manage its lifecycle
python3 probe_blueprints.py --host localhost --port 2000 --json /scratch/probe.json
echo "probe exit: $?"     # must be 0
```

Nine distinct blueprints are checked: three stock CARLA reference props and the six OOD assets
shipped in v0.9. Each must be registered in `blueprint_library` **and** spawn with a matching
`type_id`.

If this exits non-zero, stop. Fix the install, re-probe, and only then spend GPU-hours.

---

## 4. Run the split three times

Copy `../configs/golden_generation.yaml.template`, fill in the paths, and run it once per
replicate into a **separate output root**:

```bash
for rep in 1 2 3; do
  python3 ../runner/run_benchmark.py \
      --config /scratch/golden_gen.yaml \
      --out    /scratch/smoke_rep${rep}
done
```

Settings that are not optional:

| Setting | Value | Why |
|---|---|---|
| `benchmark.seed` | `42` | the published protocol is seed 42 only |
| `benchmark.repetitions` | `1` | one seed per replicate; the replicates are the repetition |
| `execution.workers` | `1` | a shared GPU adds timing variance to the very quantity being measured |
| `resume.mode` | `none` | a resumed replicate would reuse another replicate's record and collapse the measured spread to zero |
| output root | different per replicate | same reason |

`resume.mode: none` needs `--force` on the runner. That is deliberate friction: silently
reusing results here would produce a tolerance of exactly 0 and a bundle that fails on every
other machine.

Each replicate must exit 0. A non-zero exit means at least one route has no final record, and a
bundle cannot be built from a partial sweep.

---

## 5. Build the bundle

```bash
python3 make_golden.py \
  --replicate /scratch/smoke_rep1 \
  --replicate /scratch/smoke_rep2 \
  --replicate /scratch/smoke_rep3 \
  --reference-agent pdmlite \
  --reference-agent-version <commit-or-tag-of-the-agent-you-ran> \
  --carla-version 0.9.15 \
  --content-pack-version v0.9 \
  --content-pack-sha256 <sha256-of-the-pack-archive> \
  --gpu "<model>, driver <version>" \
  --out goldens/pdmlite_seed42_v0.9.golden.json
```

What it does:

1. Re-derives each route's expected blueprint from the XML — not from the split's own column, so
   a doctored split cannot lower the bar.
2. Checks **A1, A2 and A3 in every replicate**. Any failure and it writes nothing and exits 1.
3. Requires the status to be identical across replicates. A route that sometimes completes and
   sometimes doesn't is not a golden; investigate it or drop it from the split.
4. Takes the **median** Driving Score as the golden value.
5. Derives the tolerance: `max(1.0, 2 × largest observed spread)`, in DS points, and writes both
   the policy string and every replicate value into the bundle so the number can be audited.
6. Prints a comparison against `../reference/pdmlite_seed42_reference.tsv` — the published
   seed-42 values. This is INFO, never a gate: different hardware and a different agent build
   legitimately move closed-loop scores. A large delta is worth understanding before publishing
   the bundle, not a reason to discard it.

The floor of 1.0 DS point is there because these nine routes all scored exactly 100.00 in the
published sweep, so three replicates on one machine will very likely agree exactly and produce a
measured spread of zero. A zero-tolerance golden would fail on any other machine.

---

## 6. Verify, then commit

```bash
python3 check_acceptance.py --results-root /scratch/smoke_rep1 --json /scratch/report.json
echo "exit: $?"     # 0 now, instead of 3
```

Commit the bundle to `tests/goldens/`. The CI job `acceptance-goldens` flips from
skipped-with-reason to running on the next push — it validates the bundle's integrity and its
agreement with the split. **CI can never execute the routes**: GitHub-hosted runners have no GPU
and no CARLA. Executing the split stays a manual step on a GPU host.

---

## 7. When goldens go stale

Regenerate whenever any of these change:

- the **content pack** (a v1.0 asset replacement changes the stimulus, so scores are not
  comparable even where the blueprint id is unchanged);
- the **route XMLs** — the harness hard-fails on a sha256 mismatch before comparing anything;
- the **smoke split** — the bundle pins the split's sha256 and refuses to be used with another;
- the **reference agent** version;
- the **CARLA** version.

The bundle carries `bundle_version`, `binds_to`, the split sha256, the content-pack version and
the agent version precisely so that a stale golden is a loud error and not a quiet wrong answer.

---

## 8. Scope at v0.9 — be honest about this

The split covers `base` for all three categories, plus `visual_shift` and `geometric_shift` for
**pedestrian** and `geometric_shift` for **static**. It cannot cover more:

- **static `visual_shift`** needs `trafficmessageboard`, `trafficarrowboard` or
  `europianarrowboardtrailer` — none of them redistributable.
- **vehicle `visual_shift` and `geometric_shift`** need the six `vehicle.ood.*` assets — none of
  them redistributable.

So a v0.9 golden bundle certifies that the *shipped* half of the benchmark is installed
correctly. It says nothing about an install of the other twelve assets, because no v0.9 user has
them. Extending the split to full level coverage is v1.0 work and is listed in `../README.md`.

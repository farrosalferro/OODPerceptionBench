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
| Agent | PDM-Lite from that same checkout, unmodified — see "The reference agent" below |
| Python | 3.10, with `pip install -r ../../env/requirements-pdmlite.txt` (numpy 1.23.5) |
| GPU | one is enough; the replicates are sequential by design |

> **Set `environment.python` to an absolute interpreter path, not a bare `python3`.** The
> jobscript executes that value verbatim, and `conda activate` does not reliably win against an
> inherited `PATH`: on the first real golden run it silently selected the system Python 3.8,
> which cannot `import carla`, and the replicate died as an infrastructure failure rather than
> as a wrong-interpreter error. The bundle's `environment.python` is read from each replicate's
> `_runner/env_provenance.json` (written by the runner's preflight), not from the interpreter
> that runs `make_golden.py`.

### The reference agent

The golden's agent is the **top-level** `team_code/data_agent.py` of the carla_garage checkout
that `setup.sh` patched, at the pinned commit `beb3433407f42c1adced312b877a61fe04f338ba`, with no
local edits. Anyone can fetch it:

```bash
git clone https://github.com/autonomousvision/carla_garage.git
git -C carla_garage checkout --detach beb3433407f42c1adced312b877a61fe04f338ba
```

(`setup.sh` does exactly this.) It is **not** `Bench2Drive/leaderboard/team_code/data_agent.py`,
which is a different PDM-Lite variant with its own `autopilot.py`.

| Config key | Value | Why |
|---|---|---|
| `agent.entrypoint` | `<carla_garage>/team_code/data_agent.py` | the agent above |
| `agent.working_dir` | `<carla_garage>` | the planner loads `team_code/speed_limits/*.npy` by a relative path |
| `agent.pythonpath` | `[<carla_garage>/team_code]` | its sibling imports (`autopilot`, `config`, …) |
| `agent.env` | `{DATAGEN: "0", TOWN: "smoke", REPETITION: "0"}` | weather safety, and a folder name the agent needs; see below |

**Weather safety comes from `DATAGEN`, not from patch 430.** Patch 430 gates the weather shuffle
in the *Bench2Drive* data agent only. The top-level agent shuffles the weather only when
`DATAGEN=1` (`self.datagen = int(os.environ.get("DATAGEN", 0)) == 1` in `team_code/autopilot.py`,
checked before `shuffle_weather()` in `team_code/data_agent.py`). The template sets
`DATAGEN: "0"` explicitly so an inherited environment variable cannot turn it on.

**`TOWN` and `REPETITION` are required, but only name a folder.** The runner gives every route a
`SAVE_PATH` (its log folder). With `SAVE_PATH` set, `setup()` in `team_code/autopilot.py` names
its own sub-folder from `TOWN` and `REPETITION` and raises `KeyError` when either is missing, so
every route ends `Failed - Agent couldn't be set up`. The runner still exits 0, because that is a
settled result; `make_golden.py` is what refuses it, after the replicates have run. Nothing else
in `team_code/` reads the two variables, so their values do not affect the drive. The v0.9 bundle
never needed them: its agent copies (next paragraph) had that line edited out.

**Keep `team_code/` clean.** `make_golden.py` refuses to build a bundle when
`git -C <carla_garage> status --porcelain -- team_code` prints anything. The v0.9 bundle was
produced by untracked `*_debug.py` copies of the agent, which no one else could fetch; a commit
sha cannot reveal that, `git status` can. `setup.sh` itself never writes under the top-level
`team_code/` (all 26 patches target `Bench2Drive/`), and `__pycache__/` is ignored upstream.

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
  --reference-agent-repo <carla_garage> \
  --reference-agent-commit beb3433407f42c1adced312b877a61fe04f338ba \
  --reference-agent-entrypoint team_code/data_agent.py \
  --reference-agent-url https://github.com/autonomousvision/carla_garage \
  --carla-version 0.9.15 \
  --content-pack-version v0.9 \
  $(awk '{printf " --content-pack-archive %s=%s", $2, $1}' ../assets/SHA256SUMS) \
  --runner-version "$(git rev-parse HEAD)" \
  --gpu "<model>, driver <version>" \
  --out goldens/pdmlite_seed42_v0.9.golden.json
```

Provenance it establishes before reading any result, refusing (nothing written) if it cannot:

- **Agent.** `<carla_garage>` must be at `--reference-agent-commit`, the entrypoint must be a
  tracked file, and `git status --porcelain -- team_code` must be empty. The bundle records
  `repo` (a public URL, never a local path), the full `commit`, the `entrypoint`, its
  `entrypoint_sha256`, and `version` = `carla_garage@<full sha> team_code/data_agent.py`.
- **Interpreter.** Each replicate's `_runner/env_provenance.json` must exist and all of them must
  agree on the Python version and package versions, and must have run the stated entrypoint. The
  bundle stamps `environment.python` and `environment.python_packages` (numpy, scipy, and the
  carla client if recorded) — never an interpreter path. For replicates produced by a runner
  without that preflight, pass `--replicate-python X.Y.Z` (the version of `environment.python`
  in the config); the package versions are then left unstamped. Any disagreement exits 1.
- **Agent environment.** The same file records the `agent.env` the routes ran with. All
  replicates must agree, and every one must have `DATAGEN: "0"` set explicitly — left out, the
  runner's own environment decides; anything else exits 1. The bundle stamps it as
  `reference_agent.env`. Replicates from a runner that did not record it leave that `null`,
  with a warning: check `DATAGEN` by hand then.
- **Host.** `environment.os` is the platform string each replicate's `_runner/report.json`
  recorded — the machine the replicates ran on, never the one running `make_golden.py`. The
  replicates are runs on one machine, so they must agree; a difference exits 1. A replicate
  without a run report leaves it `null`, with a warning.
- **Content pack.** One `--content-pack-archive NAME=SHA256` per archive, taken from
  `assets/SHA256SUMS`. The bundle lists them in `environment.content_pack_archives` and sets
  `environment.content_pack_sha256` to the composite:

  ```text
  composite = sha256( "".join(f"{sha256}  {name}\n" for name, sha256 sorted by name) )
  ```

  i.e. the sha256 of the canonical `SHA256SUMS` text (two spaces, LF line ends, sorted by
  archive name in byte order). `assets/SHA256SUMS` is in that form, so the composite equals
  `sha256sum assets/SHA256SUMS`, whatever order the flags are given in. `--content-pack-sha256`
  is now only a cross-check: a value that differs from the composite is an error. The rule is
  also written into `generated.notes`.

What it then does with the results:

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

### The negative test — the only thing that proves the guard works

A golden bundle whose assertions have never been seen to **fail** is decoration. Prove A1 fires
before you trust it:

```bash
# 1. take one shipped prop out of the CARLA content directory
mv "$CARLA_ROOT/CarlaUE4/Content/RoadClosedBarricade" /tmp/negtest_barricade

# 2. RE-RUN the affected route with the asset missing -- this step is the whole test
<your-python> ../runner/run_benchmark.py --config <golden_gen.yaml> \
    --out /tmp/smoke_negative --force        # resume.mode: none, workers: 1

# 3. now check it
<your-python> check_acceptance.py --results-root /tmp/smoke_negative
echo "exit: $?"          # MUST be non-zero, failing A1 on that one route

# 4. put it back, and re-probe
mv /tmp/negtest_barricade "$CARLA_ROOT/CarlaUE4/Content/RoadClosedBarricade"
```

**Step 2 is not optional, and skipping it produces a test that cannot fail.**
`check_acceptance.py` is offline — it re-reads result JSON that is already on disk. Moving an
asset cannot change a record written yesterday, so checking an *existing* results root after
removing a prop passes every time and proves nothing.

What the real test showed when it was first run: the route **completed with Driving Score
100.0** while `ttr_dar.agent_type` recorded `vehicle.tesla.model3` instead of
`static.prop.roadclosedbarricade`. CARLA had silently substituted a fallback vehicle. Nothing in
the score, the status or the exit code betrayed it — A1 was the only thing that noticed. That is
exactly the failure this directory exists to catch, and it is why the goldens are the point of
the smoke split rather than its scores.

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
- the **reference agent** version or entrypoint;
- the **evaluation environment** (`env/requirements-pdmlite.txt`, or the Python version);
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

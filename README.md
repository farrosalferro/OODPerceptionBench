# OOD-PerceptionBench

> # ⚠️ Pre-release — do not cite, and do not report numbers from this yet.
>
> **The accompanying paper is not yet posted.** This repository is deliberately **untagged**:
> there is no `v0.9.0` release and no DOI. Contents may change without notice.
>
> Twelve of the eighteen OOD props are still being replaced for licensing reasons, and their
> routes re-run — so any score produced from the tree as it stands **will not be comparable**
> to the table in the paper. The evaluation runner has also not been validated against real
> hardware, and no acceptance goldens exist yet.
>
> A `v0.9.0` tag, a Zenodo DOI, and a citable record arrive when the paper goes to arXiv.

**A closed-loop CARLA benchmark that separates *visual* from *geometric* out-of-distribution
shift for end-to-end driving policies.**

Most robustness benchmarks for driving perturb appearance — weather, lighting, texture, noise.
This one holds the scenario fixed and swaps the *object* the ego has to react to, along two
independent axes:

| Level | What changes | Example |
|---|---|---|
| `base` | nothing — the reference prop, a native CARLA asset | a standard adult pedestrian |
| `visual_shift` | novel **appearance**, in-distribution **shape** | an astronaut-suited pedestrian |
| `geometric_shift` | novel **shape / size**, whatever the appearance | a boar; a delivery robot |

Every route exists at all three levels with the same waypoints, weather, traffic and trigger
geometry, so a score difference is attributable to the prop and nothing else. 475 routes across
three interaction categories, evaluated on 17 published end-to-end models.

The headline result is in the working title: **texture-robust, geometry-fragile.** Geometric
shift costs roughly **2.7×** what visual shift costs (mean driving-score drop 13.2 vs 4.9), the
regression is significant for **14 of 17** models, and it holds in **46 of 51** (model, category)
cells at p < 0.001.

---

## Version stamp

> **This is v0.9.0, and it corresponds to arXiv v1 of the paper.**
>
> v1.0 will correspond to arXiv v2. Between them, two pedestrian props and one static prop are
> being replaced for licensing reasons and their routes re-run. **Scores from v0.9 and v1.0 are
> not comparable on the affected props and must never be pooled into one table.** Every artifact
> in this repository carries this stamp — see [`VERSION`](VERSION).

| | v0.9 (this tag) | v1.0 (later) |
|---|---|---|
| Route definitions (475) | ✅ | ✅ (unchanged except replaced props) |
| Baseline records, 17 models, seed 42 | ✅ | re-run for replaced props |
| Portable runner + SLURM example | ✅ | ✅ |
| Import procedures (static / walker / vehicle) | ✅ | ✅ |
| Overlay patches + `setup.sh` + CI | ✅ | ✅ |
| Content pack | 6 of 18 OOD props | replacement props for the other 12 |
| Acceptance harness (`tests/`) | ✅ assertions A1–A3 | ✅ A1–A4 |
| Acceptance goldens | **none** — see below | measured bundle for the smoke split |
| Zenodo DOI | — | ✅ |

> **There is no golden bundle at v0.9, and `tests/goldens/` is empty of real goldens.** What
> ships is the format, the generator and the procedure. Generating a golden needs a GPU, a
> running CARLA 0.9.15 and the installed content pack; none of that existed where this release
> was assembled, and a fabricated golden is worse than none — it would pin whatever was broken
> at the moment it was minted and make the harness certify it forever.
>
> The consequence is deliberate and load-bearing: with no goldens, `tests/check_acceptance.py`
> runs assertions A1–A3 and exits **3 (INCONCLUSIVE)**, never 0. **A1 — did the intended
> blueprint actually spawn — needs no golden and is where nearly all the defensive value sits**;
> only A4 (driving score within tolerance) is blocked. Treat exit 3 as "this install is not known
> to be good", not as a pass. Procedure to close it:
> [`tests/goldens/GENERATING.md`](tests/goldens/GENERATING.md).
>
> `tests/goldens/EXAMPLE.golden.json` is a worked example of the file format, not a golden. It
> carries an all-zero split hash so it can never validate, the harness skips it by filename, and
> CI asserts both of those facts.

---

## What this repository actually is

**A thin overlay, not a fork.** It contains our code only. The simulation harness it modifies
([`autonomousvision/carla_garage`](https://github.com/autonomousvision/carla_garage), which
vendors Bench2Drive) is pinned by commit SHA and patched by [`setup.sh`](setup.sh).

Two reasons. First, Bench2Drive's root licence is CC BY-NC-**ND** — NoDerivatives — and we
modified its tree; shipping patches rather than a modified copy keeps us clear of that question
entirely. Second, it makes our contribution legible: ~26 files, not 100k lines of someone
else's code with ours buried inside.

The known cost of an overlay is **patch rot** — upstream moves and a hunk stops applying. That
is why [CI](.github/workflows/setup.yml) runs `setup.sh` against the pinned SHA on every push
and weekly on a schedule. If that badge is red, do not trust a fresh install.

### Pinned upstream

| | |
|---|---|
| Repository | `https://github.com/autonomousvision/carla_garage.git` |
| Branch | `leaderboard_2` |
| Commit | `beb3433407f42c1adced312b877a61fe04f338ba` |
| Commit date | 2025-12-28 |
| **Pinned on** | **2026-08-03** |
| Simulator | CARLA **0.9.15** (must match exactly — see [`assets/INSTALL.md`](assets/INSTALL.md)) |

Bench2Drive is vendored *inside* carla_garage upstream-side; it is not a submodule, so there is
exactly one repository to clone. Full detail in [`patches/UPSTREAM.txt`](patches/UPSTREAM.txt).

> **The pin is deliberately not upstream's current tip.** Upstream has since merged a
> numpy ≥ 1.24 compatibility fix. Our patches apply cleanly to that newer commit too — the two
> change sets are file-disjoint — but it modifies PDM-Lite internals, and PDM-Lite is the
> reference agent the acceptance goldens will be generated with, so we pin the tree the
> published records were produced against.
>
> **Practical consequence:** at this pin, Bench2Drive still uses numpy aliases that numpy 1.24
> removed. **Pin `numpy<1.24` in your evaluation environment.** Alternatively advance the pin
> yourself and accept that your run is no longer bit-identical to our baselines. The reasoning
> and the verification are recorded in [`patches/UPSTREAM.txt`](patches/UPSTREAM.txt).

### What CI actually checks — and what it cannot

Every push runs, as hard failures: `setup.sh` against the pinned SHA and the reverse-apply proof;
the route freeze validator (475 files, every sha256 against `MANIFEST.tsv`); the reconciliation
proving the published records cover exactly those 475 once per model; the acceptance harness's
own self-tests; and the repository hygiene scanners. A tag build additionally runs
[`tools/check_release_ready.py`](tools/check_release_ready.py) as a **blocking** gate — one
outstanding item and the tag fails.

Two checks are outside a hosted runner's reach, and are reported as *not attempted* rather than
quietly omitted:

- **Replaying the paper's statistics pipeline** (checks 1/3 and 3/3 of
  [`records/verify.sh`](records/verify.sh)) needs the paper repository, private until arXiv. The
  maintainer runs it before tagging.
- **The acceptance assertions A1–A4** need a GPU, CARLA 0.9.15 and the installed content pack.
  CI runs the harness's self-tests, which proves the harness still detects a missing asset — not
  that *your* install has one.

---

## Quick start

```bash
git clone https://github.com/farrosalferro/OODPerceptionBench.git   # default branch: master
cd OODPerceptionBench

# 1. Fetch the pinned upstream and apply our patches. Fails loudly on any
#    rejected hunk; safe to re-run (idempotent).
./setup.sh --upstream-dir ./third_party/carla_garage

# 2. Install CARLA 0.9.15 yourself, then the content pack:
#    see assets/INSTALL.md  <-- do not skip, see "Silent failure" below

# 3. Run. Every path comes from your config file; there are no built-in defaults.
python runner/run_benchmark.py --config config/my_machine.yaml \
                               --agent  /path/to/your_agent.py \
                               --routes routes/ --out results/
```

`setup.sh` does **not** install CARLA, create a conda environment, or download model weights.

### Silent failure — read this before you trust a number

If the content pack is not installed correctly, `try_spawn_actor('static.prop.roadclosedsign')`
**does not raise**. The prop is simply absent, the ego drives an empty road, and the route
completes with a perfectly plausible driving score. Nothing in the logs says anything is wrong.

This is the single most dangerous failure mode in the benchmark, and it is why
[`tests/`](tests/) exists: the acceptance test queries the live world for the spawned actor's
`type_id` and compares it against the route XML. **Run it once after install.** A benchmark that
silently measures nothing is worse than one that crashes.

---

## What you can actually run at v0.9 — the honest table

Twelve of the eighteen OOD props are **not redistributable** (see
[Licensing](#licensing-and-the-twelve-missing-props)). They are not in this repository in any
form. So a fresh install can run this much:

| Category | Routes runnable | Total | Usable for the paired comparison? |
|---|---:|---:|---|
| **Pedestrian** | **126** | 162 | **Yes** — 2 of 3 props at each level; reduced statistical power |
| **Static** | **30** | 70 | Partly — geometric 2 of 3, **visual 0 of 3 → no visual comparison** |
| **Vehicle** | **81** | 243 | **No** — base level only |
| **Total** | **237** | **475** | |

All **145 base-level route files** run without any content pack at all, because every reference
prop is a native CARLA asset. (Those 145 files cover the **55 distinct base routes** — 27
vehicle, 18 pedestrian, 10 static — at one file per reference prop.) The content pack adds the
other 92: 18 each for `astronaut`, `firefighter`, `boar` and `deliveryrobot`, 10 each for
`concreteroadbarrier` and `roadclosedbarricade`.

Concretely, at v0.9 you can reproduce the paper's central *visual-vs-geometric* contrast on the
pedestrian category, you can measure geometric shift but not visual shift on static, and on
vehicle you can only establish a base-level reference. The full result table is reproducible
without any of this — from the published records, no GPU required (Tier A below).

### Support tiers

| Tier | You want to… | Supported? | Needs |
|---|---|---|---|
| **A** | verify the paper's numbers | **Yes** | nothing but this repo — [`records/`](records/) + [`tools/`](tools/), no GPU, no CARLA |
| **B** | evaluate **your own** model | **Yes** | CARLA 0.9.15 + content pack + [`runner/`](runner/); route coverage per the table above |
| **C** | re-run our 17 baselines | **Deferred** to `contrib/` | 17 third-party model environments; that is where all the licence exposure and bit-rot lives |

Tier C is not a promise we can keep across seventeen upstream repositories. We publish the
records instead, which is what a reader actually needs.

---

## Repository layout

```
routes/       475 canonical route XMLs + MANIFEST.tsv + EXCLUSIONS.md + validator
records/      per-route baseline records for 17 models + PDM-Lite, seed 42 (CSV + typed loader)
runner/       portable serial + local multi-GPU runner, and a de-hardcoded SLURM example
config/       machine configuration; every path a user must supply lives here
patches/      the overlay: our changes to the pinned upstream, one patch per file
assets/       install/verify/attribution for the 6 redistributable OOD props
              (the cooked binaries themselves are hosted separately — see assets/README.md)
classifier/   the three notebooks implementing the visual/geometric admissibility rule
docs/         asset-import procedures, asset traps, and the prop-replacement rule
tests/        acceptance harness — proves the intended blueprint actually spawned
tools/        CI checks and repository utilities
setup.sh      clone pinned upstream, apply patches, verify
```

Each directory has its own `README.md` stating exactly what belongs there.

> **The asset binaries are not in this repository.** The six cooked content directories are
> ~167 MB packaged, which is past what a git repository should carry, so they are hosted
> separately and `assets/` holds the installer, the checksums, the attributions and the
> verifier. See [`assets/README.md`](assets/README.md) for the download location.

---

## Protocol — do not vary these if you want comparable numbers

- **Seed 42, single seed.** This matches the published baselines exactly. Requiring three seeds
  would triple entry cost to ~174 GPU-hours for no change in any conclusion; multi-seed
  robustness is reported in the paper's appendix instead.
- **Unit of aggregation** is the **(model, category) cell** — 17 × 3 = 51 cells.
- **Within a cell**, pairs are formed on `(scenario, route, seed)`, prop variants are averaged
  per side, and a paired Wilcoxon signed-rank test is run on reference-vs-visual and
  reference-vs-geometric driving score, with a 95% bootstrap CI on the median Δ.
- **Across cells**, a paired Wilcoxon over the 51 cells with paired Cohen's `d_z`.
- **n = 55 base routes** (27 vehicle + 18 pedestrian + 10 static).

**There is no reportable reduced split, by design.** The headline claims are *counts of models
crossing a significance threshold* over n = 55. Subsampling changes those counts, so a "mini"
split would contradict the paper rather than approximate it. The `smoke` split that ships in
[`tests/`](tests/) exists to prove an install is sound — and, from v1.0, to carry the golden
expected outputs — not for its routes. **Never report a score from it**; every artifact it
produces is stamped `"reportable": false`.

Five routes are deliberately absent because one geometric prop cannot spawn on narrow
Town12/13 streets. They are excluded across *all three levels* so that pairing stays intact.
See [`routes/EXCLUSIONS.md`](routes/EXCLUSIONS.md) — these are not gaps to be fixed.

---

## Bringing your own agent (Tier B)

The runner drives the standard CARLA Leaderboard 2.0 agent interface, the same one Bench2Drive
and carla_garage use. In short:

1. Write a class deriving from the leaderboard `AutonomousAgent` with `setup()`, `sensors()`
   and `run_step()`, and a module-level `get_entry_point()` returning its name.
2. Point `--agent` at that file and set the environment-activation command in your config, so
   the runner can launch your agent in its own environment.
3. Run the acceptance test first (`tests/`) — it verifies your install spawns the right
   blueprints before you spend GPU-hours on a wrong-but-plausible sweep. At v0.9 it ends at
   exit **3 (INCONCLUSIVE)** because there is no golden bundle to compare scores against; a
   failure of A1 (`blueprint_spawned`) still exits 1 and still means stop.
4. Report which route subset you ran. With the v0.9 content pack that is 237 of 475, and a
   number computed over a different subset is not comparable to ours.

Full interface details and the config schema: [`runner/README.md`](runner/README.md).

---

## Licensing, and the twelve missing props

Our code is **MIT** ([`LICENSE`](LICENSE)). The assets are not uniform — read
[`NOTICE`](NOTICE).

**Six OOD props ship**, five under CC BY 4.0 and **one, `walker.pedestrian.firefighter`, under
CC BY-NC 4.0 — NonCommercial.** The content pack is therefore mixed-licence with a
non-commercial component. If your use is commercial you must exclude that asset and the routes
that reference it. (Bench2Drive is itself CC BY-NC-ND, so a non-commercial term is consistent
with this benchmark's lineage — but it has to be stated, not buried.)

**Twelve OOD props do not ship.** Ten are paid marketplace assets whose licences prohibit
standalone redistribution *and*, separately, prohibit AI use — so seller permission alone could
not have fixed it. Two are third-party game IP that was uploaded to a free model site under a
licence the uploader had no right to grant; nobody can redistribute those.

**We deliberately do not tell you where to buy them.** Ten carry an explicit AI-use
prohibition, and pointing users at those listings so they can run an AI benchmark would walk
them into the same restriction that closed the path for us.

### Reproducing the missing props yourself

The benchmark's claims are per-*class*, not per-prop: what matters is that a prop falls in the
visual or the geometric class, not that it is one specific mesh. So a substitute works, and we
publish everything needed to make one:

- **Dimensional specification.** The paper's appendix gives every prop's bounding box and its
  assigned shift class.
- **The classification rule** that decides whether a candidate lands in the visual or the
  geometric class — a z-score test against the reference-prop cluster plus a relative-size
  test — with the reference cluster statistics, in
  [`docs/replacing-props.md`](docs/replacing-props.md).
- **Import procedures.** [`docs/`](docs/) documents the full path from a raw mesh to a
  registered CARLA blueprint — [static](docs/import_procedure_static.md),
  [walker](docs/import_procedure_walker.md) and [vehicle](docs/import_procedure_vehicle.md) —
  with the stage scripts in [`docs/stages/`](docs/stages/) parameterised (no hardcoded paths).
  **The vehicle procedure ships as a DRAFT at v0.9**; static and walker are validated.
- **The classifier itself.** [`classifier/`](classifier/) holds the three notebooks that
  implement the admissibility rule, so a candidate can be checked mechanically rather than by
  eye.
- **Validation.** [`tests/`](tests/) proves your substitute actually spawns.

A substituted prop makes your run **v1.0-incomparable to our v0.9 records on that prop**. Say so
when you report.

---

## Citing

Please cite both the paper and this software — see [`CITATION.cff`](CITATION.cff). If you use
the benchmark, please also cite **Bench2Drive** and **CARLA**; if you use the PDM-Lite reference
agent or the TransFuser++ baseline, also cite **carla_garage**. Full entries are in
[`NOTICE`](NOTICE).

## Contributing

Issues and PRs welcome, especially patch-rot reports (a red CI badge) and replacement-prop
candidates that satisfy the dimensional rule. Whether we accept *submitted result rows* into
[`records/`](records/) is deferred to v1.0 — until then, records are our baselines only, so that
every number in that directory has one known provenance.

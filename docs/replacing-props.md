# Replacing the props we cannot ship

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**
The dimensional targets, cluster statistics and thresholds below are the ones that produced the
v0.9 baseline records in [`../records/`](../records/). v1.0 replaces a subset of the props; when
that lands, this document gets a v1.0 stamp and the targets change with it.

---

## What this document is for

Twelve of the eighteen OOD blueprints used in the paper are **not redistributable** and are not in
this repository in any form. This document is the reproduction path for those twelve. It gives, for
each one:

- the **bounding-box target** `(L, W, H)` and the **shift class** it must reproduce,
- the **reference cluster** it is scored against and the **admissible band** that follows,
- how to check a candidate **mechanically** before you commit any modelling time,
- how to **import** the result and how to **prove** it actually spawned,
- what re-running costs, and what your numbers can and cannot be compared against.

It does **not** tell you where to buy the originals. See [Why there is no shopping
list](#why-there-is-no-shopping-list) — that omission is deliberate and is the point of the whole
approach.

---

## 1. The twelve, and why they are missing

| Blueprint ID | Category | Level | Reason not shipped |
|---|---|---|---|
| `vehicle.ood.sedan` | vehicle | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `vehicle.ood.hatchback` | vehicle | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `vehicle.ood.suv` | vehicle | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `vehicle.ood.armoredvan` | vehicle | geometric | paid marketplace asset — no standalone redistribution **and** no AI use |
| `vehicle.ood.dumptruck` | vehicle | geometric | paid marketplace asset — no standalone redistribution **and** no AI use |
| `vehicle.ood.roadroller` | vehicle | geometric | paid marketplace asset — no standalone redistribution **and** no AI use |
| `walker.pedestrian.soldier` | pedestrian | visual | third-party **game IP**, uploaded to a free model site under a licence the uploader had no right to grant |
| `walker.pedestrian.wheelchair` | pedestrian | geometric | third-party **game IP**, same situation |
| `static.prop.trafficmessageboard` | static | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `static.prop.trafficarrowboard` | static | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `static.prop.europianarrowboardtrailer` | static | visual | paid marketplace asset — no standalone redistribution **and** no AI use |
| `static.prop.roadclosedsign` | static | geometric | paid marketplace asset — no standalone redistribution **and** no AI use |

Two distinct failure modes, and they are not interchangeable:

**Ten are paid marketplace assets.** Nine sit under one marketplace's standard licence, one under
another's royalty-free licence. Both prohibit redistributing the content on a standalone basis, and
both **separately** prohibit use in AI training or AI datasets. The second restriction is the one
that closed the file: a seller granting us redistribution would still not have cured the AI-use
term, so there was no permission path that a normal "may we redistribute this?" email could open.

**Two are third-party game IP.** They were uploaded to a free model site tagged with a permissive
licence, but the uploader did not own the underlying character and could not grant that licence —
in one case the uploader's own description credits the game studio. Nobody can redistribute those,
including the site they came from, and including us.

The six that *do* ship are listed in [`../assets/README.md`](../assets/README.md); one of them is
**NonCommercial**, which makes the pack mixed-licence. Full terms: [`../NOTICE`](../NOTICE).

### Why there is no shopping list

We do not name the listings, link them, or describe how to buy them.

Ten of the twelve carry an explicit **AI-use prohibition**. Telling you where to purchase an asset
whose licence forbids AI use, so that you can run it through an *AI perception benchmark*, would
walk you into exactly the restriction that closed that path for us — while looking like helpful
documentation. The honest version of "here is how to reproduce this" is a **specification**: the
bounding box, the class, and the rule that decides class. That is what follows.

If you go shopping anyway, that is your transaction and your licence review. Two things worth
checking on any candidate, whatever its price: (1) does the licence permit redistribution of the
*cooked* asset inside a research artifact, and (2) is there an AI-use / "NoAI" flag, which is a
separate switch from redistribution and is frequently set independently of it. An asset that is
free is not automatically clear either — the two game-IP cases above were free.

---

## 2. What a substitute does and does not preserve

**The benchmark's claims are per-class, not per-prop.** The hierarchy is *reference → visual shift
→ geometric shift*; the paper's headline numbers are counts of models crossing significance and
paired effect sizes over those levels. No claim anywhere depends on a specific mesh being a
specific truck. So a dimension-matched, class-matched substitute **reconstructs the same level of
the hierarchy** and the same experimental design.

What it does **not** preserve is the absolute numbers. A different mesh has a different silhouette,
texture, colour, and reflectance, and driving policies are sensitive to all of them. Concretely:

- Your per-route and per-prop Driving Scores **will differ** from the v0.9 records for the props you
  replaced. That is expected, not a bug, and not evidence that anything is broken.
- Your **class-level** conclusions — geometric shift hurts more than visual shift, how many models
  regress significantly, the direction and rough magnitude of the effect — are the reproducible part.
- Rows in [`../records/`](../records/) for a prop you replaced are **not comparable** to your rows
  for that prop. Cross-model comparison on a replaced prop requires re-running *every* model you
  want to compare, not just yours.

**Say so when you report.** State which props you substituted, their measured `(L, W, H)`, and the
classifier verdict. A results table that silently mixes our props and yours is unreadable by anyone
downstream.

---

## 3. The specification

### 3.1 The classification rule

The definition of record is the paper's appendix (`app:props`, "Classifier edge cases"), which is
what the published records were produced under; [`README.md` §
Classification](README.md#classification-which-shift-level-did-you-just-build) restates the same
rule for importers. In brief, each candidate dimension
`d ∈ {L, W, H}` is scored against a reference cluster of *in-distribution* CARLA assets:

```
Z_d = |x_d − μ_d| / σ_d        Visual: ∀d, Z_d ≤ 2        Geometric: ∃d, Z_d > 3
```

`Z ∈ (2, 3]` is a deliberate ambiguous band — assets landing there are excluded, not rounded.

Where the reference cluster has near-zero variance (`σ < 10⁻⁴ m` — identical collision primitives,
or a single reference asset), `Z` degenerates and the rule substitutes a **relative difference**:

```
Δ_d = |x_d − μ_d| / μ_d
```

There are **two** relative-difference arms and both use the same cutoff — a single 20% weakest-link
threshold, with **no ambiguous band**:

| Arm | Used for | Visual | Geometric | Ambiguous band |
|---|---|---|---|---|
| walker-adult | the adult half of the pedestrian union rule | `∀d, Δ_d ≤ 20%` | `∃d, Δ_d > 20%` | none — binary |
| static anchor | all static props, vs `trafficwarning` | `∀d, Δ_d ≤ 20%` | `∃d, Δ_d > 20%` | none — binary |

**Both relative-Δ arms are binary, and that follows from why they exist at all.** The `(2, 3]` gap
in the `Z` arm is a statement about *cluster spread*: between 2σ and 3σ the evidence genuinely does
not decide, so the rule declines to. The relative-difference arms are used precisely where there is
no spread to measure — 43 identical capsules, or a single anchor prop — so there is no second
quantity from which a second cutoff could be derived. One threshold is all the data supports, and
`≤ 20%` / `> 20%` partitions every candidate with nothing left over.

That does not leave pedestrians without an ambiguous outcome: the pedestrian rule is a union of two
molds, and its ambiguity comes from the child-cluster `Z` arm. **Statics have only the one arm, so
static classification is strictly binary** — every static candidate is either visual or geometric,
and there is no band the specification excludes. If a static candidate nonetheless feels borderline,
that is a design problem, not a classification one; see §3.4 and §4.2.

Axis convention throughout: **L = X, W = Y, H = Z**, full extents, metres. When you measure a mesh,
**exclude UE collision primitives** (`UCX_`, `UBX_`, `USP_`, `UCP_`, `MCDCX_` prefixes) — including
them inflates the box and can flip a verdict.

### 3.2 Reference clusters

| Cluster | Used for | Statistics |
|---|---|---|
| `vehicle_car` | all vehicle candidates | N = 25, μ = (4.5292, 1.9513, 1.5739), σ = (0.7221, 0.1635, 0.1707) |
| `walker_adult` | pedestrian candidates (mold A) | 43 identical capsules, μ = (0.3754, 0.3754, 1.8600), **σ = 0** → relative Δ |
| `walker_child` | pedestrian candidates (mold B) | 8 capsules, μ = (0.4533, 0.4533, 1.1750), σ = (0.0645, 0.0645, 0.1035) |
| `trafficwarning` | all static candidates | single anchor prop, L×W×H = 2.3734 × 2.8706 × 3.5695 |

Pedestrians use a **union of the two molds**: visual if the candidate fits *either* (OR), geometric
only if it fits *neither* (AND). This is what lets an adult-sized, novel-appearance human count as a
visual shift even though it reads geometric against the child cluster.

Vehicles carry one extra rule: **height saturation** — if the cluster mean height *and* the
candidate height both exceed 2.0 m, `Z_H` is forced to 0. With the shipped `vehicle_car` statistics
(`μ_H = 1.57`) this never fires; it exists so the rule stays sane if the cluster is ever re-derived.

### 3.3 Per-prop targets

Source of record: the paper's appendix tables (`tab:props-vehicle`, `tab:props-pedestrian`,
`tab:props-static`). Reference rows are native CARLA blueprints and are listed because they are the
anchors, not because they need replacing.

**Vehicles** — scored against `vehicle_car`.

| Blueprint | Level | L | W | H | Z_max | Status |
|---|---|---|---|---|---|---|
| `vehicle.lincoln.mkz_2020` | base | 4.89 | 1.84 | 1.49 | — | native CARLA (reference) |
| `vehicle.mini.cooper_s_2021` | base | 4.55 | 2.10 | 1.77 | — | native CARLA (reference) |
| `vehicle.mercedes.coupe_2020` | base | 4.67 | 1.81 | 1.44 | — | native CARLA (reference) |
| **`vehicle.ood.sedan`** | **visual** | **4.97** | **1.96** | **1.46** | 0.66 | **replace** |
| **`vehicle.ood.hatchback`** | **visual** | **4.41** | **1.90** | **1.49** | 0.46 | **replace** |
| **`vehicle.ood.suv`** | **visual** | **4.64** | **2.13** | **1.75** | 1.14 | **replace** |
| **`vehicle.ood.armoredvan`** | **geometric** | **6.11** | **2.36** | **2.53** | 5.65 | **replace** |
| **`vehicle.ood.dumptruck`** | **geometric** | **8.70** | **2.75** | **3.12** | 9.10 | **replace** |
| **`vehicle.ood.roadroller`** | **geometric** | **4.69** | **2.65** | **2.79** | 7.18 | **replace** |

**Pedestrians** — union of `walker_adult` (relative Δ) and `walker_child` (Z).

| Blueprint | Level | L | W | H | Δ adult | Z child | Status |
|---|---|---|---|---|---|---|---|
| `walker.pedestrian.0001` | base | 0.38 | 0.38 | 1.86 | — | — | native CARLA (adult reference) |
| `walker.pedestrian.0028` | base | 0.38 | 0.38 | 1.86 | — | — | native CARLA (adult reference) |
| `walker.pedestrian.0014` | base | 0.38 | 0.38 | 1.30 | — | — | native CARLA (child reference) |
| **`walker.pedestrian.soldier`** | **visual** | **0.44** | **0.44** | **1.86** | 14.7% | 6.80 | **replace** |
| `walker.pedestrian.astronaut` | visual | 0.43 | 0.43 | 1.86 | 12.1% | 6.80 | ships |
| `walker.pedestrian.firefighter` | visual | 0.38 | 0.38 | 1.86 | 0.0% | 6.80 | ships (NonCommercial) |
| **`walker.pedestrian.wheelchair`** | **geometric** | **0.88** | **0.88** | **1.40** | 131.6% | 7.17 | **replace** |
| `walker.pedestrian.deliveryrobot` | geometric | 0.86 | 0.86 | 1.30 | 127.3% | 6.89 | ships |
| `walker.pedestrian.boar` | geometric | 0.94 | 0.94 | 1.22 | 147.4% | 8.17 | ships |

Note the visual walkers all read *geometric* against the child cluster (`Z ≈ 6.8`) and pass through
the adult mold. That is the union rule doing its job, not an error.

**Static props** — scored against the single `trafficwarning` anchor by relative Δ.

| Blueprint | Level | L | W | H | Δ_L | Δ_W | Δ_H | Δ_max | Status |
|---|---|---|---|---|---|---|---|---|---|
| `static.prop.trafficwarning` | base | 2.37 | 2.87 | 3.57 | — | — | — | — | native CARLA (reference) |
| **`static.prop.trafficmessageboard`** | **visual** | **2.80** | **3.17** | **3.21** | 17.8% | 10.4% | 10.2% | 17.8% | **replace** |
| **`static.prop.trafficarrowboard`** | **visual** | **2.75** | **3.23** | **3.30** | 15.7% | 12.4% | 7.6% | 15.7% | **replace** |
| **`static.prop.europianarrowboardtrailer`** | **visual** | **2.02** | **3.13** | **3.74** | 14.9% | 9.0% | 4.9% | 14.9% | **replace** |
| **`static.prop.roadclosedsign`** | **geometric** | **2.78** | **0.84** | **0.93** | 17.3% | 70.6% | 73.9% | 73.9% | **replace** |
| `static.prop.concreteroadbarrier` | geometric | 2.16 | 0.61 | 0.83 | 8.9% | 78.8% | 76.6% | 78.8% | ships |
| `static.prop.roadclosedbarricade` | geometric | 2.92 | 0.48 | 2.17 | 23.1% | 83.3% | 39.2% | 83.3% | ships |

The static geometric props are **narrow and short by design** — they break the bulky-trailer
silhouette rather than merely resizing it. A geometric substitute that is bulkier than the anchor
also passes the arithmetic, but it is not the same manipulation; prefer the narrow/short direction.

### 3.4 Admissible bands

Derived directly from §3.2. Hit the target box in §3.3 if you can; these bands are what you must
not leave. Rounded to the nearest millimetre — do not aim at a boundary.

**Vehicle, visual** (`Z_d ≤ 2` on every dimension, i.e. μ ± 2σ):

| | min | max |
|---|---|---|
| L | 3.085 | 5.973 |
| W | 1.624 | 2.278 |
| H | 1.232 | 1.915 |

**Vehicle, geometric** (`Z_d > 3` on at least one, i.e. outside μ ± 3σ on that dimension):
L outside `2.363 … 6.696`, **or** W outside `1.461 … 2.442`, **or** H outside `1.062 … 2.086`.
Landing between the two tables on every dimension is the ambiguous band — re-size.

**Pedestrian, visual** — easiest via the adult mold (`Δ_d ≤ 20%` on every dimension):
L = W in `0.300 … 0.450`, H in `1.488 … 2.232`. (The child mold, `Z_d ≤ 2`, is the alternative
route: L = W in `0.324 … 0.582`, H in `0.968 … 1.382`. Either one suffices.)

**Pedestrian, geometric** — must fail *both* molds: `Δ_d > 20%` vs adult on at least one dimension
**and** `Z_d > 3` vs child on at least one. In practice the L = W diameter drives this: the
`wheelchair` target of 0.88 m is `Z = 6.6` against the child cluster while its 1.40 m height alone
would only reach `Z = 2.2`. **Widen, do not just heighten.**

**Static, visual** (`Δ_d ≤ 20%` on every dimension): L in `1.899 … 2.848`, W in `2.296 … 3.445`,
H in `2.856 … 4.283`.

**Static, geometric** (`Δ_d > 20%` on at least one dimension): exactly the complement of the visual
box — L outside `1.899 … 2.848`, **or** W outside `2.296 … 3.445`, **or** H outside
`2.856 … 4.283`. There is no gap between the two bands, so unlike the vehicle case there is nothing
to land in the middle of.

Clearing the cutoff by a millimetre is admissible but weak: a prop 21% off the anchor on one
dimension is, to a camera, very nearly the anchor. The three static geometric props in §3.3 sit at
`Δ_max ≥ 73.9%` and the three visual ones at `Δ_max ≤ 17.8%` — the real asset set is nowhere near
the boundary, and a substitute should not be either. Aim at the *target box* in §3.3, not at the
threshold; §4.2 explains how much measurement drift a near-boundary candidate is exposed to.

---

## 4. Checking a candidate mechanically

### 4.1 The checkers

Three notebooks ship in [`../classifier/`](../classifier/), one per category. Each has the cluster
statistics hard-coded at the top and one call to edit at the bottom; edit, re-run, read the verdict.

| Category | Notebook | Input |
|---|---|---|
| static | [`../classifier/static_dimension_checker.ipynb`](../classifier/static_dimension_checker.ipynb) | `check_dimensions(name, X=L, Y=W, Z=H)` — metres |
| pedestrian | [`../classifier/pedestrian_dimension_checker.ipynb`](../classifier/pedestrian_dimension_checker.ipynb) | `check_walker(name, radius=…, half_height=…)` — **centimetres, from the UE capsule** |
| vehicle | [`../classifier/vehicle_dimension_checker.ipynb`](../classifier/vehicle_dimension_checker.ipynb) | `check_vehicle(name, L, W, H)` — metres |

Each prints per-dimension scores, the headroom to the threshold, the valid range, and a single
verdict: `VISUAL (Level 1)`, `GEOMETRIC (Level 2)`, or `AMBIGUOUS`. An ambiguous verdict means the
asset is not admissible at either level — re-size it, do not argue with it.

**`AMBIGUOUS` is reachable for vehicles and for the pedestrian child mold; for statics it is not.**
The static rule is binary (§3.1), so the static notebook's ambiguous branch survives only as a
guard and says so when it prints. A static candidate always comes back visual or geometric, and
that verdict *is* the specification's verdict — there is no extra hand-check to perform on top of
it. What you still owe it is a comparison against the level you **declared** (§3.3), and a sanity
check against §3.4's advice on staying well clear of the boundary.

For CI or scripted use, [`stages/static/dimension_check.py`](stages/static/dimension_check.py) is a
non-notebook implementation of the static rule with `--expect visual|geometric`, exiting non-zero on
a mismatch. Use it as the gate in an automated import; the declared shift class should be
*verified*, never inferred. It applies the same flat-20% rule as the notebook and the paper, so the
two agree by construction.

**Walkers are measured from the UE capsule, not the mesh.** CARLA derives a walker's bounding box
from its collision capsule: `L = W = 2 × Capsule Radius`, `H = 2 × Capsule Half Height`, cm → ÷100.
You therefore cannot classify a walker in Blender — you have to get as far as the blueprint's Shape
panel first. Budget for the possibility that the answer sends you back to the mesh.

### 4.2 Two caveats that will otherwise cost you

**The static geometric cutoff is a flat 20%, and older copies of this document said otherwise.**

| Artifact | Static geometric cutoff | Ambiguous band |
|---|---|---|
| The paper's appendix (specification of record) | `Δ_d > 20%` | none — binary |
| `classifier/static_dimension_checker.ipynb` as shipped | `Δ_d > 20%` | none — binary |
| `stages/static/dimension_check.py` as shipped | `Δ_d > 20%` | none — binary |

Paper and tooling agree, on both sides of the rule: `Δ_d ≤ 20%` on every dimension is visual,
`Δ_d > 20%` on at least one is geometric, and nothing is excluded in between. **Any artifact
quoting a `> 50%` static cutoff, or a `(20%, 50%]` band excluded as ambiguous, is out of date** —
that was a drafting error, and §3.1 above is the rule the published records were produced under.

Nothing about the existing props turns on it either way: every static visual prop sits at
`Δ_max ≤ 17.8%` and every static geometric prop at `Δ_max ≥ 73.9%`, so no published number moves
under either reading. It matters only for *new* candidates — which is exactly who is reading this
document — and the practical advice for them is §3.4's: land near the target box, not near the
threshold.

**Typing a §3.3 table value into a notebook will not reproduce the table's score.** This is
expected, and it trips people. The appendix columns were computed from the **unrounded** capsule
measurement against the **rounded** cluster statistics printed in the appendix preamble; the
notebooks use the **unrounded** cluster statistics and whatever number you type in. The two drift
apart in the second decimal:

| Prop | §3.3 says | Notebook, fed the §3.3 box | Why |
|---|---|---|---|
| `soldier` | Δ adult 14.7% | 17.2% | ref 0.38 vs μ 0.3754; L/W actually 0.4359, table rounds to 0.44 |
| `astronaut` | Δ adult 12.1% | 14.5% | same; L/W actually 0.426 |
| `wheelchair` | Z child 7.17 | 6.62 | σ 0.06 vs 0.064487 |
| `boar` | Z child 8.17 | 7.55 | same |

Worst observed drift is ~2.5 pp on Δ and ~0.6 on Z. **No verdict changes** for any prop, in either
direction — the visual walkers sit far below the 20% cutoff and the geometric ones far above `Z = 3`.

Two consequences for you. **Do not chase an exact match** to the appendix number; match the
*verdict*. And **do not design to a boundary**: if your candidate lands within ~3 pp of a cutoff,
treat it as ambiguous and re-size, because which artifact you scored it with starts to decide the
answer. The commented-out example calls inside the notebooks are likewise scratch values from asset
selection and occasionally differ from the appendix in the second decimal — **the tables in §3.3 are
the specification**, the notebook comments are not.

---

## 5. Admissibility the classifier cannot check

Passing the dimensional rule is necessary, not sufficient. Four more constraints:

**1. A visual substitute must actually look out-of-distribution.** The visual level exists to change
appearance while holding geometry fixed. A candidate that is dimensionally in-band but is just
another ordinary sedan is not a visual shift — it is a second reference asset, and it will quietly
weaken the very contrast the benchmark measures. Novel texture, novel silhouette detail, novel
colour scheme; recognisably not part of CARLA's stock fleet.

**2. Vehicles must advertise the attributes the scenarios filter on.** One of the six vehicle
scenarios, `vehicle_opens_door_two_ways`, requests its actor with
`attribute_filter={"has_dynamic_doors": True}`. A blueprint that does not advertise that attribute
is silently filtered out — and the fallback is **`vehicle.tesla.model3`**, an ordinary in-distribution
sedan, with a plausible score and a single warning line in the log. That is 5 of the 27 routes for
every vehicle prop. See [`ASSET_TRAPS.md`](ASSET_TRAPS.md) §1 for the full mechanism; it is the
single most expensive trap in this pipeline.

**3. Walkers need a rig and a working animation set.** A geometric walker substitute is still a
`walker.pedestrian.*` that has to walk. The per-rig animation work is the irreducible manual part of
[`import_procedure_walker.md`](import_procedure_walker.md).

**4. It has to fit on the road.** Five base routes are deliberately excluded from all three levels
because an oversized geometric prop could not be placed on them — see
[`../routes/EXCLUSIONS.md`](../routes/EXCLUSIONS.md). If your substitute is materially larger than
the target box, expect more of these. Do **not** silently drop the failing route from one level
only: that breaks base/visual/geometric parity and invalidates the paired statistics. Either stay
close to the specified box, or remove the base route from all three levels and report the change.

---

## 6. Importing the substitute

| Category | Procedure | Status | Effort |
|---|---|---|---|
| static prop | [`import_procedure_static.md`](import_procedure_static.md) | validated end-to-end | ~1–2 h, largely scriptable |
| pedestrian | [`import_procedure_walker.md`](import_procedure_walker.md) | validated end-to-end | ~half a day; animation is per-rig manual work |
| vehicle | [`import_procedure_vehicle.md`](import_procedure_vehicle.md) | ⚠ **DRAFT — not validated end-to-end** | **1–2 days**; the Blender rigging half is skilled manual work |

> **The vehicle procedure ships as a draft.** Its upstream source was still being written when v0.9
> was cut, and its final import step was reconstructed from a truncated original. Every step in it
> is provisional; the validated version lands in v1.0. Read the banner at the top of that file
> before you commit two days to it.

Read [`README.md`](README.md) (prerequisites, path variables, anchor assets) and
[`ASSET_TRAPS.md`](ASSET_TRAPS.md) before starting any of the three. All of them need a **CARLA
0.9.15 source build plus Unreal Engine 4.26** — the packaged release cannot cook new content.

**Register the substitute under the same blueprint ID.** If your replacement for
`vehicle.ood.dumptruck` is also called `vehicle.ood.dumptruck`, every route XML keeps working
untouched and [`../routes/MANIFEST.tsv`](../routes/MANIFEST.tsv) still validates. This is precisely
why the vehicle IDs are neutral `ood.*` names rather than the asset's provenance: a mesh swap must
not rewrite the benchmark definition. If you use a different ID you will have to rewrite every route
that references the old one (27 per vehicle prop, 18 per pedestrian prop, 10 per static prop) and
regenerate the manifest checksums.

Note the registration asymmetry: a **prop** ID is plain JSON (`Content/<Name>/Config/<Name>.Package.json`),
but **walker and vehicle** IDs live in cooked base content (`WalkerFactory` / `VehicleFactory`), so
those two categories require a re-cook and their packages overwrite base content on install — which
locks them to CARLA 0.9.15 exactly.

---

## 7. Proving it worked

**A missing or misregistered asset does not raise.** `try_spawn_actor` on an unknown blueprint
returns `None`, the prop is simply absent, the ego drives an unobstructed road, and the route
finishes `Completed` with a believable Driving Score. For vehicles it is worse: the request falls
back to `vehicle.tesla.model3`, so the route runs with the exact in-distribution object the
benchmark is supposed to contrast against.

So the last step of every import is an assertion, not a successful cook:

1. Run [`../tests/`](../tests/) after installing. Its first and most important assertion queries the
   live world for the spawned actor's `type_id` and compares it against the route XML.
2. Confirm the classifier verdict against the *installed* blueprint's reported bounding box, not
   against your Blender measurement. For walkers this is mandatory — CARLA reads the capsule, and
   the capsule is what the classifier consumes.
3. Only then run routes.

Goldens in [`../tests/`](../tests/) cover the **base level** at v0.9, because no user could
reproduce a shifted-level golden without the twelve props. Your substitute will not match a shifted
golden and is not expected to.

---

## 8. What it costs to re-run

Re-running is only needed for the routes that reference the prop you replaced.

| Category | Routes per prop | Scenarios spanned | ≈ GPU-h per prop, one model | ≈ GPU-h per prop, all 17 baselines |
|---|---|---|---|---|
| vehicle | 27 | 6 | ~3.2 | ~55 |
| pedestrian | 18 | 4 | ~2.2 | ~37 |
| static | 10 | 2 | ~1.2 | ~20 |

Basis: ≈0.12 GPU-h per route, measured on a 4-parallel sweep of the 243-route vehicle category.
Replacing all twelve and re-running all seventeen published baselines is ≈485 GPU-h. Replacing all
twelve for **your own model only** is ≈29 GPU-h.

You do not need to re-run our seventeen published baselines unless you want cross-model comparison
*on your substituted props* — our records for those props were produced with different meshes and
are not comparable to yours. This is the practical cost of substitution, and it is the reason v1.0 exists:
it will ship licence-clean replacements plus a full re-run, so the comparison is restored for
everyone at once.

---

## 9. Checklist

1. Pick the prop and read its row in §3.3 — target box, level, reference cluster.
2. Source a mesh whose licence permits redistribution of the cooked asset **and** carries no AI-use
   restriction. Record author, licence, URL and `sha256` of what you downloaded.
3. Measure `(L, W, H)` with `L = X, W = Y, H = Z`, full extents, metres, **UE collision primitives
   excluded**.
4. Run the checker for the category (§4). Verdict must equal the level in §3.3. `AMBIGUOUS` — a
   vehicle or pedestrian-child outcome only — means re-size, not "close enough". Statics get a
   binary verdict and need no hand-check on top of it, but a static candidate that only just clears
   `Δ = 20%` should still be re-sized towards its §3.3 target box (§3.4).
5. Import via the category procedure (§6), registering under the **same blueprint ID**.
6. Verify: blueprint resolves; installed bounding box still classifies correctly; scenario attribute
   filters satisfied (§5, item 2); [`../tests/`](../tests/) passes with the `type_id` assertion green.
7. Re-run the affected routes (§8).
8. Report the substitution: which prop, measured box, classifier verdict, and the fact that those
   rows are not comparable to the v0.9 records.

---

## Related

- [`README.md`](README.md) — prerequisites, path variables, and the same classification rule
  restated for importers
- [`ASSET_TRAPS.md`](ASSET_TRAPS.md) — the silent-failure catalogue; read before importing anything
- [`import_procedure_static.md`](import_procedure_static.md) ·
  [`import_procedure_walker.md`](import_procedure_walker.md) ·
  [`import_procedure_vehicle.md`](import_procedure_vehicle.md) (**draft**)
- [`../classifier/`](../classifier/) — the three dimension checkers
- [`../routes/MANIFEST.tsv`](../routes/MANIFEST.tsv) · [`../routes/EXCLUSIONS.md`](../routes/EXCLUSIONS.md)
- [`../tests/`](../tests/) — acceptance harness and goldens
- [`../assets/README.md`](../assets/README.md) — the six props that do ship
- [`../NOTICE`](../NOTICE) — per-asset licences and attribution obligations

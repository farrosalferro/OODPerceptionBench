# Building your own OOD assets for OOD-PerceptionBench

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**
Blueprint IDs, dimensional specs and classification thresholds in these documents are the ones
that produced the v0.9 baseline records. A future v1.0 will replace a subset of the props; when
that happens these documents get a v1.0 stamp and the numbers they refer to change with it.

---

## Why these documents exist

OOD-PerceptionBench evaluates driving policies against *visual* (appearance) and *geometric*
(shape/size) out-of-distribution shifts. Those shifts are carried by 18 custom CARLA blueprints —
6 static props, 6 pedestrians, 6 vehicles.

**Only 6 of the 18 can be redistributed.** The other 12 are third-party marketplace assets whose
licences do not permit us to ship them, in any form, inside a content pack. For those, the
reproduction path is *specification plus procedure*: the benchmark publishes the dimensional
envelope each prop must satisfy, and these documents tell you how to turn a mesh you have licensed
yourself into a working CARLA blueprint that satisfies it.

That makes these procedures load-bearing, not appendices. They are also, we think, useful
independently of this benchmark: CARLA's own asset-import documentation is thin, and a complete
static + walker + vehicle procedure does not otherwise exist in one place.

## The three procedures

| Document | Produces | Difficulty |
|---|---|---|
| [`import_procedure_static.md`](import_procedure_static.md) | `static.prop.<name>` | ~1–2 h per asset; largely scriptable |
| [`import_procedure_walker.md`](import_procedure_walker.md) | `walker.pedestrian.<name>` | ~half a day; the animation blend space is per-rig manual work |
| [`import_procedure_vehicle.md`](import_procedure_vehicle.md) | `vehicle.<make>.<model>` | **1–2 days**; the Blender rigging half is skilled manual work, not a script |

Read [`ASSET_TRAPS.md`](ASSET_TRAPS.md) before you start any of them. It documents the failure
modes that cost the most debugging time — in particular the fact that **a missing or misregistered
asset does not crash CARLA**; the route runs to completion with a plausible score and the object
simply absent. Every procedure ends with an assertion designed to catch exactly that.

Helper scripts referenced by the procedures live in [`stages/`](stages/README.md).

---

## Prerequisites

These are hard requirements. None of the three procedures can be completed without all of them.

### Software

| Requirement | Notes |
|---|---|
| **CARLA 0.9.15, built from source** | The packaged release is **not** enough. Cooking new content requires the Unreal project, the `Makefile`, and `Content/Carla/**` — none of which ship in the release tarball. Build per CARLA's Linux build instructions at tag `0.9.15`. |
| **Unreal Engine 4.26** — CARLA's fork | `CarlaUnreal/UnrealEngine` at the `carla` branch. Stock Epic UE 4.26 will not open the CARLA project. Requires a linked Epic Games GitHub account. |
| **Blender 3.x or newer** | Any recent build. The procedures were authored on 5.1; the operations used (join, vertex groups, weight paint, bone constraints, UV editing, FBX export) are stable across versions. |
| **A Python 3.8 environment for the build system** | CARLA's `make` targets require it; see `PythonAPI/util/requirements.txt` in the CARLA source tree. |
| **A Python 3.7+ environment with the `carla` client** | For the spawn/probe verification steps. This can be, and usually is, a *different* interpreter from the one CARLA builds with. |
| **A packaged CARLA to install into** | The build you actually evaluate with. May be produced by `make package` from the same source tree, or be a stock 0.9.15 release. |

### Hardware and time

| | |
|---|---|
| Disk | **~250 GB** for the UE 4.26 build + CARLA source + Content, plus ~130 GB if you keep every `Dist/` package. Each cooked package tarball is ~1 GB, of which only 5–340 MB is your asset — the rest is re-cooked base content. |
| RAM | 32 GB is workable; 64 GB makes shader compilation and `make package` much less painful. |
| GPU | Any Vulkan-capable discrete GPU. Needed for the editor and for the spawn tests. |
| First build | **4–8 hours** for UE 4.26 + CARLA from cold, most of it unattended. |
| Per cook | 20–60 minutes for `make package` on a single asset package. |

### Knowledge

You need to be comfortable in the Unreal editor (content browser, blueprint editor, physics asset
editor, material editor) and in Blender's edit/pose/weight-paint modes. The vehicle procedure in
particular assumes you can rig a mesh. There is no way around this — it is the reason we describe
the vehicle path as *possible* rather than *practical* for a typical benchmark user.

---

## Path variables used throughout

The procedures never hardcode a location. Set these once for your machine and substitute
throughout. The [`stages/`](stages/README.md) scripts read the same names from a config file.

| Variable | Meaning |
|---|---|
| `$CARLA_SRC` | CARLA 0.9.15 **source** build root — contains `Makefile`, `Unreal/CarlaUE4/`, and (after cooking) `Dist/`. |
| `$CARLA_CONTENT` | `$CARLA_SRC/Unreal/CarlaUE4/Content` — the editor's content root. |
| `$CARLA_PKG` | The **packaged** CARLA you evaluate with — contains `CarlaUE4.sh`, `Import/`, `ImportAssets.sh`. |
| `$UE4_ROOT` | Unreal Engine 4.26 (CARLA fork) root; `$UE4_ROOT/Engine/Binaries/Linux/UE4Editor-Cmd` is the headless entry point. |
| `$BLENDER` | Path to the Blender executable. |
| `$MESH_SRC` | Working directory holding your downloaded source meshes and the exported anchor FBX files. |
| `$BENCH_ROOT` | Your clone of the OOD-PerceptionBench repository. |
| `$RESULTS_ROOT` | Where validation-route output is written. |

> **`$CARLA_PKG` must be the build you actually run the benchmark with.** This is the single most
> common way to spend an afternoon on nothing: cooking succeeds, the tarball is installed into a
> *different* CARLA than the evaluator launches, and every route then quietly runs without your
> asset. See `ASSET_TRAPS.md` §1.

### Shell entry points

Earlier internal versions of these documents referred to shell aliases. What they actually invoke:

| Was | Is |
|---|---|
| "open Blender" | `"$BLENDER"` |
| "open the Unreal Engine" | activate the CARLA build Python environment, then `cd "$CARLA_SRC" && make launch` |
| "run the standalone CARLA simulator" | `bash "$CARLA_PKG/CarlaUE4.sh"` |

---

## Anchor assets

Each procedure aligns your new asset against a reference already in CARLA, so that scale and
orientation come out right and so that the dimensional classification is computed against a fixed
baseline. All three anchors are CARLA base content in the **source tree** — they are not files you
can find in a packaged release. Open the asset in the editor and use
**Asset Actions → Export…** to get an FBX/TGA you can import into Blender.

| Procedure | Anchor | Location in `$CARLA_CONTENT` |
|---|---|---|
| Static | `static.prop.trafficwarning` | `Carla/Static/Dynamic/Construction/SM_TrafficCones_4` |
| Walker | any base adult walker skeletal mesh, e.g. `SK_euroM_` | `Carla/Static/Pedestrian/<character>/Meshes/` |
| Vehicle | `SK_Lincoln_MKZ2020` | `Carla/Static/Car/4Wheeled/LincolnMKZ2020/SK_Lincoln_MKZ2020` |
| Vehicle (lights) | `T_8ColorMask_op` — the 8-colour light UV mask | `Carla/Static/GenericMaterials/T_8ColorMask_op` |

> Note the static anchor's name mismatch: the blueprint is `static.prop.**trafficwarning**` but the
> mesh behind it is `SM_**TrafficCones_4**`. There *is* an `SM_WarningConstruction` in the same
> folder and it is a different prop. This is a general property of CARLA content — blueprint IDs are
> assigned in config and factories, not derived from asset names. Confirm the mapping in
> `Carla/Config/Default.Package.json` rather than guessing from the name.

The vehicle procedure additionally uses CARLA's stock `LincolnMKZ2020` as the reference
implementation for every blueprint it asks you to build. Its material instances, animation
blueprint and wheel blueprints are all under
`Carla/Static/Car/4Wheeled/LincolnMKZ2020/` and `Carla/Blueprints/Vehicles/LincolnMKZ2020/`.

---

## Classification: which shift level did you just build?

A prop only belongs in the benchmark if it lands unambiguously in one level. The published rule
scores each candidate dimension `d ∈ {L, W, H}` against a reference cluster of *training* assets:

```
Z_d = |x_OOD,d − μ_d| / σ_d       Visual: ∀d, Z_d ≤ 2       Geometric: ∃d, Z_d > 3
```

The gap at `Z ∈ (2, 3]` is deliberate: it is **ambiguous** and such assets are excluded.

Two reference clusters have **σ ≈ 0**, which makes `Z` undefined. For those the published rule
substitutes a **relative difference** `Δ_d = |x − μ_d| / μ_d` with a single 20% cutoff and
**no ambiguous band**:

| Category | Reference | Scoring | Visual | Geometric |
|---|---|---|---|---|
| **Static** | the single anchor prop `trafficwarning`, L×W×H = 2.3734 × 2.8706 × 3.5695 m — one asset, so σ is undefined | relative Δ | `Δ_d ≤ 20%` on every dimension | `Δ_d > 20%` on at least one |
| **Pedestrian** | **union of two molds**: `walker_adult` (43 identical capsules, σ = 0 → relative Δ) and `walker_child` (8 capsules with spread → Z-score) | mixed | fits **either** mold (OR) | fits **neither** mold (AND) |
| **Vehicle** | `vehicle_car` cluster, N = 25, μ = (4.529, 1.951, 1.574), σ = (0.722, 0.163, 0.171) | Z-score | `Z_d ≤ 2` on every dimension | `Z_d > 3` on at least one |

Checkers, one per category, ship in the benchmark repository:
`static_dimension_checker.ipynb`, `pedestrian_dimension_checker.ipynb`,
`vehicle_dimension_checker.ipynb`. A scripted equivalent of the static rule, suitable for CI, is
[`stages/static/dimension_check.py`](stages/README.md).

> **Note.** The static notebook and `dimension_check.py` implement the same published rule: a flat
> 20% relative-Δ weakest-link test against `trafficwarning` — visual when `Δ_d ≤ 20%` on every
> dimension, geometric when `Δ_d > 20%` on at least one. Statics have **no ambiguous band**; the
> `(2, 3]` gap exists only for the vehicle *Z*-score rule. (For context the real asset set sits far
> from the boundary — every static visual prop at `Δ_max ≤ 0.178`, every geometric one at
> `Δ_max ≥ 0.74` — but do not lean on that margin when sizing a new asset.)

Two further specifics worth knowing before you size an asset:

- **Height saturation (vehicles).** When the cluster mean height *and* the candidate height both
  exceed 2.0 m, `Z_H` is forced to 0. Above that point additional height is not informative about
  distribution membership, and without the rule every tall vehicle would be classified geometric
  on height alone.
- **Walkers are sized by their UE capsule, not their mesh.** CARLA derives a walker's bounding box
  from the collision capsule: `L = W = 2 × Capsule Radius`, `H = 2 × Capsule Half Height`, in cm →
  divide by 100. You therefore cannot classify a walker in Blender; you must get to the blueprint's
  Shape panel first.

Axis convention throughout: **L = X, W = Y, H = Z.** When measuring a mesh's bounding box,
**exclude UE collision meshes** — any object whose name starts with `UCX_`, `UBX_`, `USP_`,
`UCP_` or `MCDCX_`. Including them silently inflates the measurement and can flip a visual shift
into an ambiguous one.

---

## Registration: the asymmetry that bites

Where a blueprint ID comes from is **not** the same for the three categories, and this determines
what you have to ship and what a rename costs.

| Category | ID source | Plain-text edit? | Re-cook to rename? |
|---|---|---|---|
| **Prop** | `Content/<Name>/Config/<Name>.Package.json` → `props[].name` | yes, it is JSON | **no** |
| **Walker** | cooked base content `Carla/Blueprints/Walkers/WalkerFactory` | no, it is a `.uasset` | **yes** |
| **Vehicle** | cooked base content `Carla/Blueprints/Vehicles/VehicleFactory` | no, it is a `.uasset` | **yes** |

For walkers and vehicles the `Package.json` entry alone is **not** enough. The `Package.json`
`name` is the packaging identity; the blueprint ID the Python API answers to comes from the
factory. A package can install cleanly, appear on disk, and still not exist as a blueprint.

Corollary: because the factories are base content, a walker or vehicle package **overwrites
base-content assets on install** and is therefore locked to CARLA 0.9.15 exactly.

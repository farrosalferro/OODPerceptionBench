# Decision record — shipping `WalkerFactory` with four of six registered walkers

> ## ✅ RESOLVED 2026-08-06 — this decision no longer applies
> The user re-cooked `WalkerFactory` in Unreal, keeping **only** the four shipped walkers
> (`astronaut`, `boar`, `deliveryrobot`, `firefighter`) plus CARLA's native `0001`–`0051`,
> and removing every other imported entry — `soldier`, `wheelchair`, `caneman`,
> `crutcheswoman`, `cow`, `tire`.
>
> The pack was rebuilt against that clean factory on 2026-08-06. Verified: zero phantom
> blueprint IDs in the shipped tarball; `props` and `walkers-ccbync` byte-identical to the
> previous build; only `walkers-ccby` changed (521 bytes smaller). The authors' evaluation
> build was never modified — the rebuild used a hardlinked staging root — so every
> published number remains reproducible.
>
> **Consequence:** the silent-failure path this record describes is gone. A user who
> installs the pack and runs a route needing a non-shipped prop now gets a *loud* failure
> (unregistered blueprint) rather than a quiet one (registered, contentless). The analysis
> below is retained as the record of why the factory must ship at all.

**Status:** decided and implemented for v0.9. The open item in §7 is resolved at v1.0.
**Date:** 2026-08-04 · **Version stamp:** pack v0.9 ↔ arXiv v1

---

## 1. The problem

The two static props in this pack self-register: CARLA reads
`Content/<Name>/Config/<Name>.Package.json` and the `props[]` entry creates
`static.prop.<name>`. Dropping the content directory in is sufficient, and nothing in
CARLA's base content is touched.

The four walkers do **not** work that way. Their `Package.json` also has a `walkers[]`
entry, but that entry is consumed at *cook* time, not at runtime. At runtime the walker
blueprint IDs come from the cooked base-content asset
`Content/Carla/Blueprints/Walkers/WalkerFactory.{uasset,uexp}`. Verified: the string
`astronaut` appears in `WalkerFactory.uexp`, and `/Game/Astronaut/Blueprints/BP_Astronaut`
appears in `WalkerFactory.uasset`'s import table.

So shipping the four walkers requires shipping `WalkerFactory` — and the `WalkerFactory`
in the build that produced every published number also registers **nine other walkers**
whose content this pack does not include, including the two non-redistributable benchmark
props `walker.pedestrian.soldier` and `walker.pedestrian.wheelchair`.

Producing a WalkerFactory that registers only the four would require opening the CARLA
UE4 project, editing the factory blueprint and re-cooking base content. **Re-cooking in
Unreal Engine is forbidden this wave**, so that option was not available.

---

## 2. Options considered

| # | Option | Verdict |
|---|---|---|
| A | Ship `WalkerFactory` as-is | **CHOSEN.** Measured below. |
| B | Do not ship `WalkerFactory`; ship walker content only | **Rejected — actively dangerous.** See §4. |
| C | Binary-patch the cooked `.uasset`/`.uexp` to delete the two entries | **Rejected.** Requires rewriting a serialised `TArray` plus the export table offsets, import table and package summary of a cooked UE4 package, with no way to validate short of running it. A corrupt factory registers nothing and fails exactly as silently as the problem it is trying to fix. Not a defensible thing to do by hand. |
| D | Author stub content for the two absent walkers | **Rejected.** Authoring a blueprint is UE work. |
| E | Drop the walkers from v0.9; ship the two static props only | **Rejected here, but it is a real option** — it contradicts the locked 2026-08-04 decision that v0.9 ships all six, so it is not mine to take. It would cost 72 of the 237 runnable routes. Recorded for completeness. |

---

## 3. Measurement, not inference

I did not want to guess what a factory entry pointing at absent content does, so I
measured it. A CARLA build root was assembled out of symlinks to the build that produced
every published number (read-only; nothing in it was modified), with `Content/`
containing **only** base `Content/Carla` plus the six shipped directories — i.e. exactly
what a user gets after installing this pack over stock CARLA 0.9.15. The
`CarlaUE4-Linux-Shipping` binary was copied (not symlinked) so that UE resolved the
project root to the test tree rather than following `/proc/self/exe` back to the original.

Result, CARLA 0.9.15 server, Town10HD:

```
WALKER_REGISTERED astronaut     = True     SPAWN OK  extent=(0.213,0.213,0.930)
WALKER_REGISTERED firefighter   = True     SPAWN OK  extent=(0.190,0.190,0.930)
WALKER_REGISTERED deliveryrobot = True     SPAWN OK  extent=(0.432,0.432,0.650)
WALKER_REGISTERED boar          = True     SPAWN OK  extent=(0.470,0.470,0.610)
WALKER_REGISTERED soldier       = True     SPAWN -> None
WALKER_REGISTERED wheelchair    = True     SPAWN -> None
```

Three things follow, all of them load-bearing:

1. **A missing import does not take the package down.** The `WalkerFactory` package loads,
   and every walker whose content *is* present registers and spawns normally. Option A
   does not endanger the four assets we ship.
2. **The two absent walkers become "phantoms":** present in
   `world.get_blueprint_library()`, findable by `.find()`, carrying the full walker
   attribute set (`gender`, `age`, `speed`, …) — and unspawnable.
   `world.try_spawn_actor(...)` returns `None`; `world.spawn_actor(...)` raises
   `RuntimeError: Spawn failed because of invalid actor description`.
3. **Static props behave differently and better.** The four non-shipped props
   (`roadclosedsign`, `trafficarrowboard`, `trafficmessageboard`,
   `europianarrowboardtrailer`) are simply absent from the blueprint library, because
   props are registered by their own `Package.json` and there is no shared factory.

---

## 4. Why option B is worse, and by how much

This is the part that decided it. The relevant code path is
`CarlaDataProvider.create_blueprint()` in
`scenario_runner/srunner/scenariomanager/carla_data_provider.py`:

```python
try:
    blueprints = CarlaDataProvider._blueprint_library.filter(model)
    ...
    blueprint = CarlaDataProvider._rng.choice(blueprints)
except ValueError:
    # The model is not part of the blueprint library. Let's take a default one
    new_model = _actor_blueprint_categories[actor_category]   # 'car' -> vehicle.tesla.model3
    print("WARNING: Actor model {} not available. Using instead {}".format(model, new_model))
    blueprint = CarlaDataProvider._rng.choice(CarlaDataProvider._blueprint_library.filter(bp_filter))
```

`_rng` is `numpy.random.RandomState`, and `RandomState.choice([])` raises **`ValueError`**
(verified), so the `except` branch is live. Every pedestrian scenario in this benchmark
calls `request_new_actor(self._pedestrian_blueprint, transform)` with no
`actor_category`, and the parameter defaults to `"car"`. Verified at all four call sites
(`dynamic_object_crossing_modified.py`, `pedestrian_crossing_stop.py`,
`vehicle_turning_route_pedestrian_modified.py`, `obstacle_approaching_ego.py`).

Therefore:

| | What the harness does | Outcome |
|---|---|---|
| **Option B** — walker content shipped, factory not | ID absent from library → `filter()` → `[]` → `ValueError` → **substitutes `vehicle.tesla.model3`** with a stdout warning | A **Tesla Model 3 drives at the ego as the "crossing pedestrian"**. Route completes. Driving Score is plausible. **72 routes × every model silently mis-measured.** |
| **Option A** — factory shipped | ID present → `try_spawn_actor` → `None` → `request_new_actor` returns `None` → scenario hits `if adversary is None: raise ValueError("Failed to spawn pedestrian")` | The route **errors out** and is recorded as failed. |

Option B converts our four *good* assets into silent Tesla substitutions. Option A leaves
two IDs that fail noisily. That is not a close call.

---

## 5. What we are actually shipping, and its precise consequence

`ood-perceptionbench-walkers-ccby-v0.9.tar.gz` contains
`CarlaUE4/Content/Carla/Blueprints/Walkers/WalkerFactory.{uasset,uexp}`, taken byte-for-byte
from the build that produced every published number
(`sha256(WalkerFactory.uexp) = f5f418f1b39a0100dd1d33bebf3fcc0566ae2ee2f607995c2c5c3fb94045957c`).

**Consequence, stated precisely:**

> Installing this pack overwrites one file in CARLA's base content. Afterwards the
> blueprint library advertises **nine walker blueprint IDs whose content is not in the
> pack**: `soldier`, `wheelchair`, `ball`, `caneman`, `cow`, `crutcheswoman`, `deer`,
> `labrador`, `tire`. All nine are *unspawnable*: `try_spawn_actor` returns `None` and
> `spawn_actor` raises. They cannot silently produce a wrong prop, but they can waste your
> time if you try to use them. Two of them — `soldier` and `wheelchair` — are referenced by
> 36 of the benchmark's 475 route XMLs, and those 36 routes are **not runnable at v0.9**.
> The other seven belong to unrelated experiments and appear in no benchmark route.
>
> Because this file overwrites base content, the pack is **hard-locked to CARLA 0.9.15**.
> Do not install it over any other CARLA version.

`tools/verify_pack.py` asserts all of this — that the six shipped IDs spawn, and that
every phantom does *not*.

---

## 6. The clean fix, and why it is nearly free

The right artefact is a `WalkerFactory` registering only the walkers actually shipped.
That needs one UE editor session: open the CARLA project, delete the unwanted rows from
the factory blueprint's parameter array, re-cook base content.

**A batched Unreal Engine session is already on the v1.0 critical path** (replacing the two
unlicensable walkers, refused-vehicle swaps, the `vehicle.ood.*` rename and its
`VehicleFactory` re-cook, plus a `WalkerFactory` re-cook for the replaced walkers). Trimming
the factory is a few minutes inside a session that must happen anyway. **Recommend folding
it in rather than scheduling separate work.** After that session the phantom list should
drop to zero and this document can be retired.

---

## 7. Open item

This is a known, accepted defect of v0.9, not an oversight: the pack ships a
`WalkerFactory` that advertises nine unusable blueprint IDs, because the alternative (§4)
silently substitutes a Tesla Model 3 for a pedestrian across 72 routes. It is scheduled to
be fixed in the v1.0 UE session described in §6, after which the phantom list should be
empty and this document can be retired.

---

## Appendix — incidental findings from this investigation

**A. The `vehicle.ood.*` rename has not reached the cooked `VehicleFactory`.**
`Content/Carla/Blueprints/Vehicles/VehicleFactory.uexp` still contains `Tohoku`, `Inkas`,
`Caterpillar`, `Hamm`, `Sedane`, `Hatchback`, `DumpTruck`, `RoadRoller`. The `ood.*` rename
landed in the route XMLs, the route generator and `vehicle_classification.json`, but the
blueprint IDs a running CARLA actually serves are still the old trademarked ones. This is
consistent with the fact that a vehicle rename needs a full re-cook, scheduled for the
v1.0 UE session) and is not a v0.9 blocker — no vehicle asset ships at v0.9 — but it does
mean the shipped route XMLs reference six vehicle blueprint IDs that exist in **no**
current build. The frozen route set and the acceptance goldens both expect that.

**B. `Content/Boar/Animations/` contains 60.3 MB of uncooked editor assets.** Seven
`.uasset` files with no `.uexp` sibling (`alerted`, `digging_feeding`, `observing`,
`sniffing`, `trot`, `wake_up`, `wound`), dated 2025-12-09 rather than the 2026-07-07 cook.
The shipping runtime cannot load them and no cooked package imports them — the boar's
locomotion uses `walk`/`run`/`sleeping` via `BS_Boar` and `laydown` via `BP_Boar`, all four
of which are properly cooked and shipped. Excluded from the pack; hashes recorded in
`build/EXCLUSIONS.tsv`.

**C. `Content/Firefighter/Blueprints/` has a case-insensitive filename collision.**
`BP_Firefighter.uasset` and `BP_FireFighter.uasset` differ only in one letter's case. On
Linux both exist and `BP_Firefighter_v2` correctly inherits from the lower-case one; on
macOS or Windows one silently overwrites the other during extraction, and the surviving
file's internal package name may not match the path UE looks it up by — which would take
the firefighter down. The upper-case `BP_FireFighter` is referenced by nothing (that exact
string occurs in exactly one file in the whole build: itself), so it is excluded from the
pack and the collision is gone.

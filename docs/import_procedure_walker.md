# Import procedure — walker (pedestrian)

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**

Produces a `walker.pedestrian.<name>` blueprint in a CARLA 0.9.15 build: a rigged, animated,
correctly-sized pedestrian actor that CARLA's walker control API can drive.

Read [`README.md`](README.md) (prerequisites, path variables, anchors, classification) and
[`ASSET_TRAPS.md`](ASSET_TRAPS.md) first.

Written for source assets distributed as `.fbx`. Unreal-native assets also work; skip the Blender
stage and start at Stage 2.

**Expect this to take about half a day per asset.** Two things make it slower than the static
procedure and they cannot be scripted away:

- **Every walker rig is different.** Bone names, orientation and root motion vary per asset, so the
  animation blend space has to be authored and tuned by eye for each one. There is no generic
  retarget that produces a natural-looking walk.
- **The blueprint ID lives in cooked base content.** Unlike props, the `Package.json` entry does not
  register anything. Registration is an edit to `WalkerFactory`, which is a `.uasset` in
  `Carla/Blueprints/Walkers/`, and it requires a re-cook.

---

## Naming

Pick one CamelCase, alphanumeric-only `AssetName` (e.g. `Cow`) and derive everything from it.

| Thing | Form | Example |
|---|---|---|
| Content folder | `$CARLA_CONTENT/<AssetName>/` | `Cow/` |
| Skeletal mesh | `SK_<AssetName>` | `SK_Cow` |
| Physics asset | `<AssetName>_PhysicsAsset` | `Cow_PhysicsAsset` |
| Skeleton | `<AssetName>_Skeleton` | `Cow_Skeleton` |
| Blend space | `BS_<AssetName>` | `BS_Cow` |
| Animation blueprint | `ABP_<AssetName>` | `ABP_Cow` |
| Actor blueprint | `BP_<AssetName>` | `BP_Cow` |
| Level | `<AssetName>Map` | `CowMap` |
| Blueprint ID | `walker.pedestrian.<assetname lowercased>` | `walker.pedestrian.cow` |

---

## Stage overview

| # | Stage | Tool | Ends at |
|---|---|---|---|
| 1 | Align + export | Blender | **CHECKPOINT 1** — orientation and scale |
| 2 | Import mesh, animations, textures | Unreal editor | **CHECKPOINT 2** — material compiles, clips on the skeleton |
| 3 | Blend space + animation blueprint | Unreal editor | **CHECKPOINT 3** — walk cycle looks natural |
| 4 | Actor blueprint + capsule | Unreal editor | **CHECKPOINT 4** — capsule matches the intended shift class |
| 5 | `WalkerFactory` + level + `Package.json` | Unreal editor | **CHECKPOINT 5** — factory entry present |
| 6 | Spawn test | CARLA Python API | **CHECKPOINT 6** — spawns, `type_id` matches, it moves |
| 7 | Cook + install | `make package`, `ImportAssets.sh` | **CHECKPOINT 7** — registered in the target build |
| 8 | Route check | benchmark routes | **CHECKPOINT 8** — real routes use it |

---

## Stage 1 — Blender: align and export

Working directory: `$MESH_SRC`.

1. **Open Blender**: `"$BLENDER"`.

2. **Import the anchor.** Use a base CARLA adult walker skeletal mesh — e.g. `SK_euroM_` under
   `$CARLA_CONTENT/Carla/Static/Pedestrian/EuroM_/Meshes/` — exported once from the Unreal editor
   (**Asset Actions → Export…** → FBX) into `$MESH_SRC`. This gives you the scale and facing
   reference; it is not retargeted onto and does not need to be.

3. **Import your asset** (`.fbx` or `.glb`).

4. **Align it to the anchor** — rotate (`r`) and scale (`s`) uniformly.

   > **You cannot classify a walker here.** CARLA derives a walker's bounding box from its Unreal
   > **collision capsule**, not from the mesh, so the shift level is not determined until Stage 4.
   > For a geometric shift, size the asset to its real-world dimensions in Blender and treat the
   > capsule you set later as the authoritative number.
   >
   > A starting capsule can be computed from your target dimensions:
   > ```bash
   > python stages/walker/walker_sizing.py \
   >     --target_dims_m '{"L":1.95,"W":0.80,"H":1.64}' --shift_type geometric \
   >     --out <verdict.json>
   > ```
   > It derives `radius_cm = W×100/2`, `half_height_cm = H×100/2` and runs the published
   > union-of-molds classifier, cross-checking against the shift type you declared.

5. **Export as FBX**:

   | Section | Setting |
   |---|---|
   | Include → Object Types | `Mesh` **and** `Armature` |
   | Transform | **X Forward**, **Z Up** |
   | Geometry | Face Smoothing on |
   | Armature | Add Leaf Bones **off** |
   | Animation | **on** if the asset carries clips, otherwise off |

   If the animation clips are separate FBX files, leave them separate and export the mesh alone —
   you will import the clips individually in Stage 2.

> ### CHECKPOINT 1 — orientation and scale
> **Pass when:** the asset stands on the ground plane, is centred, faces the same way as the
> anchor, and is at plausible real-world scale. A walker exported facing the wrong way will walk
> backwards along its route and nothing downstream will flag it.

---

## Stage 2 — Unreal editor: import mesh, animations, textures

Open the editor with `cd "$CARLA_SRC" && make launch`.

1. **Create the content folder** `$CARLA_CONTENT/<AssetName>/` with five sub-folders:

   | Folder | Contents |
   |---|---|
   | `Config` | `<AssetName>.Package.json` |
   | `Maps` | the level that carries the edited `WalkerFactory` |
   | `Static` | skeletal mesh, physics asset, skeleton |
   | `Blueprints` | blend space, animation blueprint, actor blueprint |
   | `Animations` | the animation clips |

   The `Maps` folder is **not** optional here, unlike for props. It is the mechanism by which the
   edited `WalkerFactory` gets cooked into your package.

2. **Import the mesh** into `Static`. Unlike a prop: **check `Skeletal Mesh`** and `Import Mesh`,
   and check `Import Animation` if the clips came in the same FBX.

3. **Rename** the three assets the import created: `SK_<AssetName>`, `<AssetName>_PhysicsAsset`,
   `<AssetName>_Skeleton`.

4. **Import the textures.** Uncheck **sRGB** for Roughness, Metalness and Ambient Occlusion; leave
   it on for BaseColor and Emissive; set Normal maps to **Normalmap** compression. Set these
   *before* wiring, not after.

5. **Wire the material** — Albedo → Base Color, Normal → Normal, Roughness → Roughness,
   Metalness → Metallic, AO → Ambient Occlusion, Emissive → Emissive Color. Packed `ORM` /
   `metallicRoughness` maps should be split into single channels first
   (`stages/static/texture_classify.py` does this). **If you get a sampler error, disconnect
   Metallic** — see `ASSET_TRAPS.md` §5.

6. **Import the animations** into `Animations`. Standalone clip FBXs are imported onto
   `<AssetName>_Skeleton`; clips that arrived inside the mesh FBX are simply moved into the folder.

7. **Save every package the import created** — mesh, skeleton, physics asset, material, every
   texture, every clip. **Save All** (`Ctrl+Shift+S`).

   A scripted version of steps 2–7 exists and is worth using as a starting point; the material
   still needs the interactive pass at CHECKPOINT 2:

   ```bash
   "$UE4_ROOT/Engine/Binaries/Linux/UE4Editor-Cmd" "$CARLA_SRC/Unreal/CarlaUE4/CarlaUE4.uproject" \
     -run=pythonscript \
     -script="stages/walker/ue_walker_import.py --asset_name <AssetName> \
              --sk_fbx <SK_AssetName.fbx> --anims_dir <clips_dir> \
              --textures_json <texture_classify.json> --dest_root /Game --out <verdict.json>" \
     -unattended -nosplash -nopause -nullrhi -stdout
   ```

   **Judge it by `"ok": true` in `<verdict.json>`, not by the exit code** — the headless editor
   routinely segfaults on teardown after doing all its work correctly.

> ### CHECKPOINT 2 — material and clips
> **Pass when, in the editor:**
> 1. `SK_<AssetName>` previews **textured**, not grey;
> 2. "Compiling Shaders" has reached 0;
> 3. every animation clip opens and plays on `<AssetName>_Skeleton` without a retarget warning;
> 4. **Save All** is done and no package shows an unsaved marker.
>
> As with props, an interactively-authored material means the eventual cook must be a **clean**
> cook — a warm cook reuses an empty shader cache and the walker stays grey.

---

## Stage 3 — Blend space and animation blueprint

This is the part that is genuinely per-rig and cannot be automated.

1. In `Blueprints`, create a **Blend Space** on `<AssetName>_Skeleton`, named `BS_<AssetName>`.

2. Configure the horizontal axis:

   | Field | Value |
   |---|---|
   | Name | `Speed` |
   | Maximum Axis Value | `300.0` |
   | Number of Grid Divisions | `300` |
   | Interpolation Type | Linear |

3. Place the clips on the axis. A working starting point:

   | Clip | Speed | Rate scale |
   |---|---|---|
   | Idle | `0.0` | — |
   | Walk | `3.0` | — |
   | Run | `20.0` | `2.0` |

   These are *starting* values. Tune them by running the asset (CHECKPOINT 3) and iterating —
   the right numbers depend on the clip's own stride length and playback rate.

   Compile and save.

4. In the same folder create an **Animation Blueprint**: parent class `AnimInstance`, skeleton
   `<AssetName>_Skeleton`, named `ABP_<AssetName>`.

5. Wire it:
   1. In **Event Graph**, delete all nodes.
   2. Open the animation blueprint of an existing CARLA walker, copy its entire Event Graph, and
      paste it into yours. This graph reads the owning pawn's speed each frame; it is generic and
      does not need editing.
   3. Create two variables:
      - `CharacterReference`, type **BP Walker** (accept the "Change Variable Type" prompt);
      - `ForwardSpeed`, type **Float** (same prompt).
   4. In **AnimGraph**, drag in `BS_<AssetName>`.
   5. Drag in `ForwardSpeed` as a **get**.
   6. Connect `ForwardSpeed` → the blend space's `Speed` input, and the blend space's pose output
      → **Output Pose**. A live connection animates with white dots travelling along it.
   7. Compile and save.

> ### CHECKPOINT 3 — the walk cycle looks natural
> **Pass when:** driven at a constant speed, the asset's feet do not skate, the gait matches the
> translation rate, and the transition between idle/walk/run is not visibly snapped.
>
> Test it by spawning the walker in the editor's simulator and driving it with
> `carla.WalkerControl` at a few speeds. **If it looks wrong, go back to step 3 and retune the
> blend-space sample speeds.** Expect two or three iterations; this is normal and is the single
> biggest time sink in the procedure.

---

## Stage 4 — Actor blueprint and capsule

1. In `Blueprints`, create a **Blueprint Class** with parent **`BP Walker`**, named
   `BP_<AssetName>`.

   A scaffold that duplicates a template walker's three blueprints with the right names, parent
   class and generic event graph:

   ```bash
   "$UE4_ROOT/Engine/Binaries/Linux/UE4Editor-Cmd" "$CARLA_SRC/Unreal/CarlaUE4/CarlaUE4.uproject" \
     -run=pythonscript \
     -script="stages/walker/ue_walker_clone.py --asset_name <AssetName> \
              --clone_from <TemplateWalker> --dest_root /Game --out <verdict.json>" \
     -unattended -nosplash -nopause -nullrhi -stdout
   ```

   **It donates naming and the parent class only.** It copies the source graph verbatim and does
   **not** retarget across skeletons — the skeleton repoint, blend-space samples, mesh/anim-class
   assignment and capsule sizing all remain manual. Treat it as boilerplate removal, not as an
   import.

2. **Mesh component** — in Details: set `Skeletal Mesh` to `SK_<AssetName>`, set `Materials` to
   the asset's materials, and set Animation → `Anim Class` to `ABP_<AssetName>`.

3. **Capsule component** — set `Capsule Half Height` and `Capsule Radius`. **This is what
   determines the walker's shift classification.** CARLA converts the capsule to a bounding box as
   `L = W = 2 × radius`, `H = 2 × half_height`, in cm.

   Cross-check with `pedestrian_dimension_checker.ipynb` (or `stages/walker/walker_sizing.py`)
   before moving on. The published rule is the **union of two molds**: visual if the capsule fits
   *either* the adult mold (relative Δ ≤ 20% of μ = 0.3754 / 0.3754 / 1.86 m) or the child mold
   (Z ≤ 2 of μ = 0.4533 / 0.4533 / 1.175 m); geometric only if it fits **neither**; ambiguous
   otherwise — and ambiguous assets must not be used.

4. Size the three trigger volumes to the new body: `PedestrianDeathTrigger`,
   `PedestrianPropDeathTrigger`, `CarStopper`. These drive the scenario's death and
   vehicle-stopping logic; leaving them at a default human's dimensions on a much larger or
   smaller body produces subtly wrong scenario behaviour that no check will catch.

5. **Character Movement** component → set `Max Acceleration` to `6048`.

6. Compile and save.

> ### CHECKPOINT 4 — capsule matches the intended shift class
> **Pass when:** the classifier's verdict on your capsule equals the shift class you declared for
> this asset, with no ambiguity.
>
> ```bash
> python stages/walker/walker_sizing.py \
>     --capsule_override '{"radius_cm": 40, "half_height_cm": 75}' \
>     --shift_type geometric --out <verdict.json>
> ```
>
> A mismatch here is a **halt**, not a warning. An asset that is not cleanly in one class blurs
> exactly the distinction the benchmark measures.

---

## Stage 5 — Register: `WalkerFactory`, level, `Package.json`

1. Under `Content` (the root), find and open **`WalkerFactory`**
   (`Carla/Blueprints/Walkers/WalkerFactory`).

2. Add an element to the `Walkers` list:

   | Field | Value |
   |---|---|
   | `Id` | `<assetname>` lowercased — this becomes `walker.pedestrian.<Id>` |
   | `Class` | `BP_<AssetName>` |
   | `Gender` | as appropriate |
   | `Age` | as appropriate |
   | `Speed` | three elements, in order: `0.0`, `1.0`, `2.5` |
   | `Generation` | `0` |

   Compile and save.

   > **This step cannot be scripted.** The `Walkers[]` list is baked into the factory's
   > `GenerateDefinitions` blueprint *graph*, not exposed as a reflected class-default property, so
   > the Unreal Python API cannot add to it. It is a GUI edit. Do not spend time trying to automate
   > it — we did.

3. In your asset's `Maps` folder, create a **Level** named `<AssetName>Map` and open it.

4. **Drag into the level** the actor blueprint `BP_<AssetName>` **and** the edited `WalkerFactory`.
   Two objects. Save.

   The factory actor in the level is what causes the edited factory to be cooked into your package.
   Without it your package installs and your walker is still not registered.

5. Create `Config/<AssetName>.Package.json`:

   ```json
   {
       "props": [],
       "maps": [
           {
               "name": "<AssetName>Map",
               "path": "/Game/<AssetName>/Maps",
               "use_carla_materials": false
           }
       ],
       "walkers": [
           {
               "name": "<assetname lowercased>",
               "path": "/Game/<AssetName>/Blueprints/BP_<AssetName>.BP_<AssetName>"
           }
       ]
   }
   ```

   The `walkers[]` entry does **not** register the walker at runtime — nothing reads it. It exists
   so that packaging tooling can assert that the JSON and the factory `Id` agree. Keep it correct
   anyway; a mismatch here is a cheap early signal that the factory edit did not land.

   Steps 3–5 can be scripted (the factory edit in step 2 cannot), with the editor closed:

   ```bash
   "$UE4_ROOT/Engine/Binaries/Linux/UE4Editor-Cmd" "$CARLA_SRC/Unreal/CarlaUE4/CarlaUE4.uproject" \
     -run=pythonscript \
     -script="stages/walker/ue_walker_finalize.py --asset_name <AssetName> \
              --walker_id <assetname> --dest_root /Game \
              --package_json_path <Content/<AssetName>/Config/<AssetName>.Package.json> \
              --out <verdict.json>" \
     -unattended -nosplash -nopause -nullrhi -stdout
   ```

> ### CHECKPOINT 5 — the factory entry exists
> **Pass when:** `WalkerFactory` lists your `Id`, `<AssetName>Map` contains exactly the two actors,
> and `Package.json` parses with `walkers[0].name` equal to the factory `Id`.
>
> This is the step whose omission is invisible for the longest. Everything downstream — cook,
> install, route — succeeds without it, and the routes score a fallback actor.

---

## Stage 6 — Spawn test in the editor

Play in the editor (`Alt+P`) and drive the Python API against it.

```bash
python stages/walker/carla_walker_probe.py \
    --walker_name <AssetName> --blueprint_id walker.pedestrian.<assetname> \
    --gate_dir <renders/> --out <verdict.json> \
    --host localhost --port 2000
```

The probe checks four things in order, and each catches a different failure:

1. **Registration** — `blueprint_library.filter(<id>)` returns exactly your walker. Empty means
   CARLA would silently substitute a fallback actor in every scenario.
2. **Spawn and type** — the actor spawns and `actor.type_id == <id>` exactly.
3. **Animation** — after applying `carla.WalkerControl` for N ticks, the actor has actually
   *translated*. This catches a frozen or broken skeletal setup, which otherwise looks fine
   standing still.
4. **Render** — RGB from two angles, which is how you catch a grey material.

> ### CHECKPOINT 6 — it is real, registered, and it moves
> **Pass when:** all four checks pass and the two renders show a textured, correctly-scaled walker.
> The verdict also reports `bounding_box.extent` — record it; scenario spawn-offset logic uses
> `extent.x`.

---

## Stage 7 — Cook and install

1. **Close the editor**, then cook from the Python 3.8 build environment:

   ```bash
   cd "$CARLA_SRC"
   make package ARGS="--packages=<AssetName>"
   ```

   Clean cook — see CHECKPOINT 2.

2. **Install** into every CARLA you will evaluate with:

   ```bash
   cp "$CARLA_SRC/Dist/<AssetName>_0.9.15-dirty.tar.gz" "$CARLA_PKG/Import/"
   cd "$CARLA_PKG" && bash ImportAssets.sh
   ```

> ### CHECKPOINT 7 — registered in the build you will evaluate with
> `ImportAssets.sh` exits **2** when it skips newer shared Engine files. Normal. Judge by artifacts:
>
> ```bash
> test -d "$CARLA_PKG/CarlaUE4/Content/<AssetName>" && echo "content installed"
>
> # against a RUNNING server from $CARLA_PKG:
> python -c "import carla; bl=carla.Client('localhost',2000).get_world().get_blueprint_library(); \
>            print([b.id for b in bl.filter('walker.pedestrian.<assetname>')])"
> ```
>
> **Pass when both succeed.** Remember that a walker package **overwrites base content**
> (`WalkerFactory`), so this install is version-locked to CARLA 0.9.15 and must be repeated on
> every machine that will run routes.

---

## Stage 8 — Route check

Run the pedestrian scenarios that use the walker, end to end, in the packaged build.

```bash
python stages/walker/walker_route_run_gen.py \
    --walker_name <AssetName> --blueprint_id walker.pedestrian.<assetname> \
    --out <verdict.json>
```

This emits one route XML and one run script per pedestrian scenario, filled from the committed
templates — waypoints, town, trigger point and weather are kept verbatim from the validated
benchmark routes; only the asset suffix and the blueprint ID change.

Start the standalone simulator (`bash "$CARLA_PKG/CarlaUE4.sh"`), run the scripts, then parse:

```bash
python stages/walker/parse_route_result.py \
    --checkpoint <route_checkpoint.json> \
    --blueprint_id walker.pedestrian.<assetname> \
    --log <route_stdout.log> --scenario <ScenarioClassName> --out <verdict.json>
```

> ### CHECKPOINT 8 — real routes actually used the walker
> **Pass when, for every scenario:**
> 1. route status is `Completed` or `Perfect` and harness `entry_status` is `Finished`;
> 2. the log contains **no** `Actor model … not available. Using instead …` line naming your
>    blueprint ID;
> 3. the log shows **no** spawn failure or scenario skip for your walker.
>
> Conditions 2 and 3 are the point. A route that completes without them is the classic false pass:
> a fallback actor played the OOD pedestrian's part and the score is meaningless.

---

## Checkpoint summary

| # | Verify | Cheapest evidence |
|---|---|---|
| 1 | Orientation, scale | Blender viewport beside the anchor |
| 2 | Material, clips on skeleton | textured preview, shaders 0, clips play, Save All |
| 3 | Gait | drive with `WalkerControl`, no foot-skate |
| 4 | Capsule → shift class | `walker_sizing.py` verdict == declared class |
| 5 | Factory entry | `WalkerFactory` lists the `Id`; level has 2 actors; JSON agrees |
| 6 | Real, registered, animated | `carla_walker_probe.py` verdict `ok: true` |
| 7 | Registered where it matters | content dir on disk **and** live blueprint query |
| 8 | Routes used it | `parse_route_result.py` — completed **and** no fallback line |

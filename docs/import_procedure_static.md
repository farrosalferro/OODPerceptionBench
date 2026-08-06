# Import procedure — static prop

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**

Produces a `static.prop.<name>` blueprint in a CARLA 0.9.15 build, correctly scaled against the
benchmark's static anchor, with working collision and a compiled material.

Read [`README.md`](README.md) (prerequisites, path variables, anchors, classification) and
[`ASSET_TRAPS.md`](ASSET_TRAPS.md) first. Path variables — `$CARLA_SRC`, `$CARLA_CONTENT`,
`$CARLA_PKG`, `$UE4_ROOT`, `$BLENDER`, `$MESH_SRC`, `$BENCH_ROOT`, `$RESULTS_ROOT` — are defined
there.

Static props are the **easiest** of the three categories and the only one where the blueprint ID
lives in plain JSON rather than in a cooked base-content factory. A rename costs a text edit, not
a re-cook.

---

## Naming

Pick one CamelCase, alphanumeric-only `AssetName` (e.g. `MessageSignTrailer`) and derive everything
from it. Do not deviate; several of the checks below are exact string comparisons.

| Thing | Form | Example |
|---|---|---|
| Content folder | `$CARLA_CONTENT/<AssetName>/` | `MessageSignTrailer/` |
| Static mesh | `SM_<AssetName>` | `SM_MessageSignTrailer` |
| Package config | `<AssetName>.Package.json` | `MessageSignTrailer.Package.json` |
| Blueprint ID | `static.prop.<assetname lowercased>` | `static.prop.messagesigntrailer` |
| Cooked tarball | `<AssetName>_0.9.15-dirty.tar.gz` | — |

---

## Stage overview

| # | Stage | Tool | Ends at |
|---|---|---|---|
| 1 | Classify + align + export | Blender | **CHECKPOINT 1** — orientation and scale |
| 2 | Import, material, collision | Unreal editor | **CHECKPOINT 2** — material compiles and previews |
| 3 | Register | text editor | **CHECKPOINT 3** — `Package.json` is valid |
| 4 | Spawn test | CARLA Python API | **CHECKPOINT 4** — spawns, blocks, `type_id` matches |
| 5 | Cook + install | `make package`, `ImportAssets.sh` | **CHECKPOINT 5** — registered in the target build |
| 6 | Route check | benchmark route | **CHECKPOINT 6** — a real route sees the prop |

Checkpoints are not optional. Each one exists because skipping it produces a *plausible-looking*
result rather than an error — see `ASSET_TRAPS.md`.

---

## Stage 1 — Blender: align, scale, export

Working directory: `$MESH_SRC`.

1. **Open Blender**: `"$BLENDER"`.

2. **Import the anchor.** The static anchor is CARLA's `static.prop.trafficwarning`
   (`$CARLA_CONTENT/Carla/Static/Dynamic/Construction/SM_TrafficCones_4`). Export it once
   from the Unreal editor (**Asset Actions → Export…** → FBX) into `$MESH_SRC`, and reuse that
   FBX for every static asset you import.

   > The blueprint is `trafficwarning` but the mesh is `SM_TrafficCones_4` — and there is a
   > *different* prop called `SM_WarningConstruction` in the same folder. Blueprint IDs in CARLA
   > come from config, not from asset names; the mapping is in
   > `$CARLA_CONTENT/Carla/Config/Default.Package.json`. Check it rather than guessing.

   Anchor dimensions, which are also the classification reference:
   **L × W × H = 2.3734 × 2.8706 × 3.5695 m.**

3. **Import your prop** (`.fbx` or `.glb`).

4. **Align it to the anchor.** Rotate (`r`) and scale (`s`) until the prop sits on the ground plane,
   is centred, and faces the same direction as the anchor.

   Scale **uniformly**. Non-uniform scaling distorts the object and invalidates the dimensional
   claim the benchmark makes about it. If the prop cannot be made to fit its intended shift class
   by uniform scaling, it is the wrong prop — pick a different one.

5. **Classify.** Measure the aligned bounding box with **L = X, W = Y, H = Z**, excluding any
   `UCX_` / `UBX_` / `USP_` / `UCP_` / `MCDCX_` collision objects, and check the shift level:

   ```bash
   python stages/static/dimension_check.py --L 2.74 --W 3.23 --H 3.30 --expect visual
   ```

   `--expect` makes this a gate: the script exits non-zero if the realized dimensions do not
   produce the shift class you declared. **Declare the class first, then verify it.** Never read
   the class off the measurement and accept whatever comes out — that is how an ambiguous prop
   enters the asset set.

   For a **geometric** shift, size the prop to its real-world dimensions (look them up) rather
   than to an arbitrary multiple of the anchor. The claim the benchmark makes is that these are
   *plausible objects of unusual extent*, not arbitrarily-rescaled ones.

6. **Export as FBX** with exactly these settings — they are pinned to the anchor's own export
   convention and getting them wrong rotates the prop in CARLA:

   | Section | Setting |
   |---|---|
   | Include → Object Types | `Mesh` only |
   | Transform | **X Forward**, **Z Up** |
   | Geometry | Face Smoothing on |
   | Armature | Add Leaf Bones **off** |
   | Animation | **off** |

> ### CHECKPOINT 1 — orientation and scale
> Render the exported FBX beside the anchor and look at it. Flat shading is enough for scale but
> often cannot show which way a near-symmetric prop faces; if you are unsure, defer the
> orientation judgement to CHECKPOINT 2, where the textured preview makes the front obvious.
>
> ```bash
> "$BLENDER" --background --python stages/static/bpy_align_export.py -- \
>     --fbx <prop.fbx> --shift_type visual --target_mode match_anchor \
>     --out_fbx <export.fbx> --out_blend <aligned.blend> \
>     --render_dir <dir> --out <verdict.json>
> ```
>
> **Pass when:** the prop sits on the ground, is centred, is the intended size relative to the
> anchor, and faces the intended direction. `<verdict.json>` must contain `"ok": true` and a
> `realized_LWH` consistent with your step-5 classification.
> **If orientation is wrong:** re-run with `--yaw 90` (or `--mirror x`, or `--front_axis -Y`) and
> look again. Do not proceed on "probably fine" — every later stage is expensive and none of them
> will tell you the prop is backwards.

---

## Stage 2 — Unreal editor: import, material, collision

Working directory: `$CARLA_SRC`. Open the editor with
`cd "$CARLA_SRC" && make launch` (from the Python 3.8 build environment).

1. **Create the content folder** `$CARLA_CONTENT/<AssetName>/` with two sub-folders:
   - `Config` — holds `<AssetName>.Package.json`
   - `Static` — holds the mesh
   (A `Maps` folder is **not** needed for props. Walkers and vehicles need one because they ship a
   level containing an edited factory; props do not.)

2. **Import the exported FBX** into `Static`. Default import settings, with two changes:
   - **Skeletal Mesh: unchecked** — a prop is a static mesh.
   - Leave the import transform alone; alignment was done in Blender.

3. **Rename** the imported static mesh to `SM_<AssetName>`.

4. **Import the textures.** In the import dialog's Texture tab, **uncheck sRGB** for:
   Roughness, Metalness, Ambient Occlusion. Leave sRGB **on** for BaseColor and Emissive. Set
   Normal maps to the **Normalmap** compression setting.

   Getting sRGB wrong is not a subtle difference — it produces visibly washed-out or crushed
   surfaces. Set it *before* wiring the texture into the material; changing it afterwards requires
   a recompile that is easy to forget.

5. **Wire the material.** Usual mapping:

   | Texture | Material input |
   |---|---|
   | Albedo / BaseColor / Diffuse | Base Color |
   | Normal | Normal |
   | Roughness | Roughness |
   | Metalness | Metallic |
   | AO / Occlusion | Ambient Occlusion |
   | Emissive | Emissive Color |

   Source assets often ship **packed** maps instead — `metallicRoughness` (glTF: G = Roughness,
   B = Metallic) or `ORM` (R = AO, G = Roughness, B = Metallic). Split these into single-channel
   images before importing rather than masking channels inside the material graph:

   ```bash
   python stages/static/texture_classify.py \
       --textures_dir <src/textures> --out_dir <normalized/> --out <verdict.json>
   ```

   **If the editor reports a texture-sampler error, disconnect the Metallic input.** This is the
   single most common cause; see `ASSET_TRAPS.md` §5. The material must actually compile — an
   uncompiled material cooks to a grey checkerboard.

6. **Set up collision.** In the Static Mesh editor:
   - Collision must be **convex elements** — if auto-generation produced nothing, add a convex
     decomposition or a simple primitive hull. A prop with no collision is invisible to physics.
   - Collision Enabled → **Collision Enabled (Query and Physics)**
   - Collision Preset → **BlockAll**

7. **Save every package the import created**, not just the mesh: the mesh, the material, and each
   texture. UE's FBX importer creates them as *sibling* packages and an SM-only save silently drops
   them (`ASSET_TRAPS.md` §6). **Save All** (`Ctrl+Shift+S`) is the reliable way to do this by hand.

> ### CHECKPOINT 2 — material and collision
> This is the step that cannot be automated in UE 4.26: headlessly-authored materials cook to an
> invalid shader and render as the grey `WorldGridMaterial` checkerboard. Only the interactive
> editor compiles real shaders into the derived-data cache.
>
> **Pass when, in the editor:**
> 1. `SM_<AssetName>` previews **textured**, not grey;
> 2. the status bar's "Compiling Shaders" counter has reached **0**;
> 3. the Collision menu shows at least one primitive, preset `BlockAll`, Query **and** Physics;
> 4. you have done **Save All**, and the Content Browser shows no unsaved (`*`) packages.
>
> Then **close the editor**. The cook must not run against a live editor.
>
> **Consequence for the cook:** because you authored the material interactively, the next cook must
> be a **clean** cook. A warm cook reuses the empty shader cache from any earlier headless attempt,
> logs `ShadersCompiled=0`, and the prop stays grey. A correct clean cook logs
> `Missing cached shader map … compiling` with `ShadersCompiled > 0`.
>
> A non-interactive re-verification of the collision half is available and worth running on resume:
> ```bash
> "$UE4_ROOT/Engine/Binaries/Linux/UE4Editor-Cmd" "$CARLA_SRC/Unreal/CarlaUE4/CarlaUE4.uproject" \
>   -run=pythonscript \
>   -script="stages/static/tier1_collision_verify.py --asset_name <AssetName> --out <verdict.json>" \
>   -unattended -nosplash -nullrhi -stdout
> ```

---

## Stage 3 — Register the prop

Create `$CARLA_CONTENT/<AssetName>/Config/<AssetName>.Package.json`:

```json
{
    "maps": [],
    "props": [
        {
            "name": "<AssetName>",
            "path": "/Game/<AssetName>/Static/Other/<AssetName>/SM_<AssetName>.SM_<AssetName>",
            "size": "Medium"
        }
    ]
}
```

- `name` becomes the blueprint ID, lowercased: `static.prop.<name.lower()>`.
- `path` is the UE **object path** of the static mesh — the full package path, then a `.`, then the
  object name again. It must resolve; a typo here produces a package that installs and a blueprint
  that does not exist.
- `size` is one of `Tiny` / `Small` / `Medium` / `Large`.

> ### CHECKPOINT 3 — the package config
> **Pass when:** the file parses as JSON, and the `path` you wrote matches what the editor shows
> under **Copy Reference** for `SM_<AssetName>` (strip the `StaticMesh'…'` wrapper).
> This is a 30-second check that routinely saves a 40-minute cook.

---

## Stage 4 — Spawn test in the editor

Launch the simulator from inside the editor (Play, or `Alt+P`) and drive the Python API against it.
You are testing three things: that the blueprint resolves, that it is oriented correctly, and that
it physically blocks.

```python
import carla
REQUESTED = "static.prop.<assetname>"

world = carla.Client("localhost", 2000).get_world()
bp = world.get_blueprint_library().find(REQUESTED)      # raises if not registered
prop = world.try_spawn_actor(bp, spawn_transform)

assert prop is not None, f"{REQUESTED} did not spawn"
assert prop.type_id == REQUESTED, f"got {prop.type_id!r}, wanted {REQUESTED!r}"
```

Then spawn a vehicle a few metres away facing the prop, attach a collision sensor, apply throttle,
and confirm the sensor fires **against the prop** and the vehicle decelerates rather than passing
through it.

> ### CHECKPOINT 4 — spawn, orientation, blocking
> ```bash
> python stages/static/carla_probe_test.py --asset_name <AssetName> \
>     --host localhost --port 2000 --out <verdict.json>
> ```
> **Pass when:** the blueprint is found, `type_id` matches exactly, the prop is oriented like the
> anchor, and the probe vehicle's collision sensor reports the prop as the other actor while the
> vehicle's speed drops.
>
> This is the **only** behavioural test of physical blocking you will get. The benchmark's route
> check uses a privileged expert planner that *avoids* obstacles, so a route that completes tells
> you nothing about whether the prop is solid.

---

## Stage 5 — Cook and install

1. **Close the editor.** Then cook, from the Python 3.8 build environment:

   ```bash
   cd "$CARLA_SRC"
   make package ARGS="--packages=<AssetName>"      # --packages takes the CONTENT FOLDER name
   ```

   Because CHECKPOINT 2 authored the material interactively, make this a **clean** cook.

2. **Install** into every CARLA you will evaluate with. The cook writes
   `$CARLA_SRC/Dist/<AssetName>_0.9.15-dirty.tar.gz`:

   ```bash
   cp "$CARLA_SRC/Dist/<AssetName>_0.9.15-dirty.tar.gz" "$CARLA_PKG/Import/"
   cd "$CARLA_PKG" && bash ImportAssets.sh
   ```

   If you cook on one machine and evaluate on another, repeat this on the evaluation machine.
   Installing into one build does **not** install into the other (`ASSET_TRAPS.md` §3).

> ### CHECKPOINT 5 — registered in the build you will evaluate with
> `ImportAssets.sh` extracts with `--keep-newer-files` and **exits 2** when it skips shared Engine
> files. That is normal. Judge by the artifact, not the exit code:
>
> ```bash
> test -d "$CARLA_PKG/CarlaUE4/Content/<AssetName>" && echo "content installed"
>
> # then, against a RUNNING server from $CARLA_PKG:
> python -c "import carla; bl=carla.Client('localhost',2000).get_world().get_blueprint_library(); \
>            print([b.id for b in bl.filter('static.prop.<assetname>')])"
> ```
>
> **Pass when:** the content directory exists **and** the live blueprint library lists the exact ID.
> Both are required. Content on disk without a live query proves nothing.

---

## Stage 6 — Route check

Run one benchmark route that uses the prop, end to end, in the packaged build.

1. Generate the route XML and its run script from the committed templates:

   ```bash
   python stages/static/route_run_gen.py \
       --asset_name <AssetName> --shift_type visual \
       --source_dir <src/dir> --out <verdict.json>
   ```

   This substitutes the route ID, the blueprint ID and the shift directory into the template, and
   points the run script at `$CARLA_PKG`. The town is read from the route XML.

2. Start the standalone simulator: `bash "$CARLA_PKG/CarlaUE4.sh"`.

3. Run the generated script. Output lands under `$RESULTS_ROOT`.

> ### CHECKPOINT 6 — a real route sees the prop
> **Pass when:**
> 1. the route reaches `Completed`;
> 2. the recorded frames actually contain the prop — check the saved RGB, or assert `type_id` on
>    the spawned actor from within the scenario;
> 3. no fallback warning appears in the log for your blueprint ID.
>
> **A `Completed` route is not by itself a pass.** With the prop missing, the route also completes,
> with a plausible Driving Score. Condition (2) is the one that carries the information.

---

## Checkpoint summary

| # | Verify | Cheapest evidence |
|---|---|---|
| 1 | Orientation, scale, shift class | GATE-1 render + `dimension_check.py --expect` exit 0 |
| 2 | Material compiles, collision present | textured preview, shader counter 0, Save All, `tier1_collision_verify.py` |
| 3 | `Package.json` valid | JSON parses; `path` == editor's Copy Reference |
| 4 | Spawns, oriented, blocks | `carla_probe_test.py` verdict `ok: true` |
| 5 | Registered where it matters | `Content/<AssetName>/` on disk **and** live blueprint query |
| 6 | Route uses it | route `Completed` **and** prop visible / `type_id` asserted |

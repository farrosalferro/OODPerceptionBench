# Import procedure — vehicle

> ## ⚠ DRAFT — NOT COMPLETE
> The upstream source for this procedure is still being written by the author. Unlike the
> static and walker procedures, this one has **not** been validated end-to-end, and its final
> import step was reconstructed from a truncated source. Treat every step as provisional.
> Tracking: the completed version lands in v1.0.



**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**

Produces a `vehicle.<make>.<model>` blueprint in a CARLA 0.9.15 build: a drivable four-wheeled
vehicle with rigged wheels, physics-constrained doors, working lights, and a correctly-classified
bounding box.

Read [`README.md`](README.md) (prerequisites, path variables, anchors, classification) and
[`ASSET_TRAPS.md`](ASSET_TRAPS.md) first.

---

## Read this before you start

**This is by far the highest-friction of the three procedures.** Budget **1–2 days per vehicle**,
most of it in Blender.

Source vehicle meshes almost always arrive as a pile of static meshes with no skeleton. CARLA
needs a *skeletal* mesh whose armature drives four wheels and four doors, plus separate static
meshes for doors, glass and lights, plus an eight-region UV mask that tells the light material
which lamp is which. Assembling that is **skilled manual rigging work, not a script**. Nothing in
this document automates the Blender half, and we do not expect that to change.

Two consequences worth being explicit about:

- If you are reproducing the benchmark's vehicle props, the practical path is to obtain the assets
  under their own licences and follow this procedure — but understand the cost before you commit.
- The blueprint ID lives in `VehicleFactory`, cooked base content. A rename requires a re-cook, and
  a vehicle package **overwrites base-content assets** on install.

---

## Naming

Pick one CamelCase, alphanumeric-only `AssetName` (e.g. `PoliceCar`).

| Thing | Form | Example |
|---|---|---|
| Content folder | `$CARLA_CONTENT/<AssetName>/` | `PoliceCar/` |
| Skeletal mesh | `SK_<AssetName>` | `SK_PoliceCar` |
| Physics asset | `<AssetName>_PhysicsAsset` | `PoliceCar_PhysicsAsset` |
| Skeleton | `<AssetName>_Skeleton` | `PoliceCar_Skeleton` |
| Collision static mesh | `SM_<AssetName>` | `SM_PoliceCar` |
| Animation blueprint | `ABP_<AssetName>` | `ABP_PoliceCar` |
| Wheel blueprints | `BP_<AssetName>_FW`, `BP_<AssetName>_RW` | `BP_PoliceCar_FW` |
| Actor blueprint | `BP_<AssetName>` | `BP_PoliceCar` |
| Level | `<AssetName>Map` | `PoliceCarMap` |
| Blueprint ID | `vehicle.<make>.<model>`, both lowercased by CARLA | `vehicle.honda.policecar` |

> **The blueprint ID does not come from `Package.json`.** It is assembled from the `Make` and
> `Model` fields of your `VehicleFactory` entry. A content folder named `SUV_Import` whose
> `Package.json` says `vehicles[].name: suv_import` can perfectly well register as
> `vehicle.ood.suv`. **Never guess the ID** — query a running server for it (Stage 6).

---

## Stage overview

| # | Stage | Tool | Ends at |
|---|---|---|---|
| 1 | Body skeletal mesh | Blender | **CHECKPOINT 1** — scale and shift class |
| 2 | Rig wheels and doors | Blender | **CHECKPOINT 2** — weights and wheel rotation |
| 3 | Split glass and lights, paint the light mask | Blender | **CHECKPOINT 3** — every lamp in the right mask region |
| 4 | Import + materials + physics asset | Unreal editor | **CHECKPOINT 4** — textured, collision boxes right |
| 5 | Blueprints: ABP, wheels, actor | Unreal editor | **CHECKPOINT 5** — doors, lights, wheels wired |
| 6 | `VehicleFactory` + level + `Package.json` | Unreal editor | **CHECKPOINT 6** — factory entry present, ID known |
| 7 | Spawn / drive / doors / lights test | CARLA Python API | **CHECKPOINT 7** — spawns, drives, `type_id` matches |
| 8 | Cook + install | `make package`, `ImportAssets.sh` | **CHECKPOINT 8** — registered in the target build |
| 9 | Route check | benchmark routes | **CHECKPOINT 9** — real routes use it, no fallback |

---

# Part A — Blender

The goal is to end up with **four** exported FBX groups:

| Export | Type | Contents |
|---|---|---|
| **Body** | skeletal mesh | the whole car body, with an armature carrying the wheel and door bones, and vertex groups binding the wheel vertices to those bones |
| **Doors** | static meshes, **one FBX per door** | front-left, front-right, rear-left, rear-right |
| **Glass** | static meshes | windscreen, windows, and one per door |
| **Lights** | static meshes | the lamp lenses, UV-mapped onto the 8-colour mask |

The walkthrough below assumes the worst and most common case: the source arrives as all-static
meshes with no rig at all.

## Stage 1 — Body skeletal mesh, scale, classification

1. **Open Blender** (`"$BLENDER"`) and delete everything in the default scene.

2. **Import the rig anchor**, `SK_Lincoln_MKZ2020`. This is CARLA base content in the **source
   tree** at

   ```
   $CARLA_CONTENT/Carla/Static/Car/4Wheeled/LincolnMKZ2020/SK_Lincoln_MKZ2020
   ```

   and is **not present in a packaged CARLA release**. Open it in the Unreal editor once and use
   **Asset Actions → Export…** to write an FBX into `$MESH_SRC`; reuse that file for every vehicle.

   You are borrowing the Lincoln's **armature** — its bone names, hierarchy and orientation are what
   CARLA's vehicle animation blueprint expects. Everything else about it gets thrown away.

3. In the Outliner, **delete the Lincoln's static meshes except `LOD0` and `LOD1`.**

4. **Import your vehicle** and join its parts into a single mesh: select all its static meshes and
   press `Ctrl+J`. Rename the mesh data to something identifiable, e.g. `PoliceCar_Body`.

5. **Graft your body onto the Lincoln's armature.** Select the Lincoln's `LOD0` static mesh, open
   the Data tab (green triangle) in Properties, and change the mesh datablock to your body mesh
   (`PoliceCar_Body`). Your geometry is now driven by the anchor's armature.

6. **Align to the anchor** — rotate (`r`) and scale (`s`), **uniformly**.

7. **Classify.** Measure the body's bounding box with **L = X, W = Y, H = Z** and check the shift
   level with the **vehicle** checker:

   ```
   $BENCH_ROOT/vehicle_dimension_checker.ipynb
   ```

   > The vehicle rule scores against the `vehicle_car` training cluster
   > (N = 25, μ = 4.529 / 1.951 / 1.574 m, σ = 0.722 / 0.163 / 0.171 m):
   > **visual** when `Z_d ≤ 2` on every dimension, **geometric** when `Z_d > 3` on at least one,
   > **ambiguous** in between — and ambiguous assets must not be used.
   >
   > Note the height-saturation rule: when the cluster mean height *and* the candidate height both
   > exceed 2.0 m, `Z_H` is forced to 0.
   >
   > Use the **vehicle** notebook, not the static one. The static checker scores relative
   > difference against a single traffic-warning prop and will give you a confidently wrong answer
   > for a car.

   For a **geometric** shift, size the vehicle to its real-world dimensions rather than to an
   arbitrary multiple.

> ### CHECKPOINT 1 — scale and shift class
> **Pass when:** the body sits on the ground plane at the anchor's origin, faces the same way as
> the Lincoln, and the vehicle checker's verdict equals the shift class you declared for this
> asset — with no ambiguity.

## Stage 2 — Rig the wheels and doors

Hide everything else and switch the viewport to **Wireframe** (`z` → Wireframe); you cannot select
interior vertices otherwise.

1. Select the body mesh and enter **Edit Mode** (`Tab`).

2. In Properties → Data → **Vertex Groups**, add one group per wheel and per door, named **exactly**
   after the corresponding bone in the anchor armature — e.g. `Wheel_Front_Left`,
   `Wheel_Rear_Right`. Capitalisation must match; the bones bind by name.

3. For each group: select the part's vertices (`L` selects linked geometry under the cursor), then
   press **Assign** in the Vertex Groups panel. Press **Deselect**, then **Select**, and confirm the
   right geometry lights up.

4. Verify with **Weight Paint** mode, one group at a time: red weight must appear **only** on the
   intended part. A wheel that carries stray body weights will drag geometry as it rotates.

5. **Separate the doors into their own meshes.** With a door's rigged vertices selected in Edit
   Mode, press `P` → **Selection**. The new object appears as the body's name with a `.001` suffix.
   Return to **Object Mode**, press `Alt+P` → **Clear and Keep Transformation** so the door is no
   longer parented to the body.

6. **Move each door's origin onto its bone.** Select the armature, enter Edit Mode, select the
   door's bone, click its **head** (the larger sphere), and press `Shift+S` → **Cursor to
   Selected**. Back in Object Mode, select the door mesh, right-click → **Set Origin → Origin to 3D
   Cursor**, then `Alt+G` to move the door to `(0,0,0)`.

   The result: the door mesh sits at the world origin with its pivot at the hinge. That pivot is
   what the physics constraint will rotate about in Unreal.

7. **Wheels.**
   1. In armature Edit Mode, move each wheel bone to the exact centre of its wheel.
   2. In **Pose Mode**, select a wheel bone → **Bone Constraints**, and add two:
      - **Limit Location** — check every box **except** *Affect Transform*; Owner = **Local Space**.
      - **Limit Rotation** — check **X** and **Z**; Owner = **Local Space**.
        (Y is left free: that is the spin axis.)
   3. Test with `r`: the wheel must spin cleanly about its own centre. If it wobbles or orbits, the
      bone is off-centre — go back to (1).
   4. Repeat for all four.

> ### CHECKPOINT 2 — weights and wheel rotation
> **Pass when:** every wheel and door group shows red weight only on its own part in Weight Paint,
> each door is a standalone object with its origin on its hinge bone, and each wheel bone spins
> about the wheel's true centre with no visible wobble.
>
> Wheel-centre error is cheap to fix here and expensive later: in Unreal it shows up as a vehicle
> that hops, drifts or refuses to reach speed, with no obvious cause.

## Stage 3 — Glass, lights, and the 8-colour mask

1. **Glass.** Rather than rigging it by hand, select it by material: in Edit Mode, open the
   Material tab, select the glass material instance, and press **Select**. Then `P` → **Selection**,
   Object Mode, `Alt+G`.

   Make a **separate mesh for each door's glass** — door glass has to parent to the door so it
   swings with it.

2. **Lights.** Extract the lamp lenses the same way, by their material.

3. **Paint the light mask.** CARLA identifies each lamp by *where its UVs sit* on an 8-region
   colour mask, not by mesh name.

   Open the **UV Editing** workspace, click **Open**, and load the mask texture
   `T_8ColorMask_op` — CARLA base content at

   ```
   $CARLA_CONTENT/Carla/Static/GenericMaterials/T_8ColorMask_op
   ```

   (export it once from the Unreal editor as TGA, like the other anchors).

   Move each lamp's UVs into the correct region:

   | Region | Colour | Lamp |
   |---|---|---|
   | `0` | Black | Low beam |
   | `R` | Red | Left blinker |
   | `G` | Green | Reverse |
   | `B` | Blue | High beam |
   | `R+G` | Yellow | Brake |
   | `R+B` | Purple | Right blinker |
   | `G+B` | Cyan | Fog |
   | `R+G+B` | White | Position |

   UVs may be freely resized to fit a region — this does not affect the mesh. A single lamp mesh
   can carry more than one lamp type: split its UV islands across regions.

4. **Clean up before exporting.** On the glass, door and light meshes:
   - remove leftover vertex groups,
   - remove unused materials,
   - remove modifiers,
   - give each mesh its final name.

5. **Export.** Use **Limit to → Selected Objects**, or hide everything else.

   | Section | Setting |
   |---|---|
   | Include → Object Types | `Mesh` **and** `Armature` for the body; `Mesh` only for the rest |
   | Transform | **X Forward**, **Z Up** |
   | Geometry | Face Smoothing on |
   | Armature | Add Leaf Bones **off** |
   | Animation | on only if the asset carries clips |

   **Export doors one per FBX** — front-left, front-right, rear-left, rear-right as four files.

> ### CHECKPOINT 3 — the light mask
> **Pass when:** every lamp's UV island lies wholly inside exactly one mask region, and the mapping
> matches the table above.
>
> Getting this wrong does not error. The vehicle simply signals with the wrong lamp — the brake
> light flashes as an indicator, say — which is both a rendering bug and, in a benchmark that
> measures how models react to vehicles, a behavioural one.

---

# Part B — Unreal Engine

Open the editor with `cd "$CARLA_SRC" && make launch`.

Throughout this part, **CARLA's stock `LincolnMKZ2020` is the reference implementation.** When a
step says "copy from an existing vehicle", it means that one:

| Thing | Path under `$CARLA_CONTENT` |
|---|---|
| Skeletal mesh + physics asset | `Carla/Static/Car/4Wheeled/LincolnMKZ2020/` |
| Glass material instance | `Carla/Static/Car/4Wheeled/LincolnMKZ2020/Materials/MI_GlassExt_Lincoln2020` |
| Lights material instance | `Carla/Static/Car/4Wheeled/LincolnMKZ2020/Materials/MI_VehicleLights_Lincoln2020` |
| Vehicle animation blueprint | `Carla/Static/Car/4Wheeled/LincolnMKZ2020/AnimBP_Lincoln2020_Animation` |
| Wheel blueprints | `Carla/Blueprints/Vehicles/LincolnMKZ2020/BP_Lincoln2020_{FLW,FRW,RLW,RRW}` |
| Actor blueprint | `Carla/Blueprints/Vehicles/LincolnMKZ2020/BP_Lincoln2020` |
| Vehicle base class | `Carla/Blueprints/Vehicles/BaseVehiclePawn` |
| Shared tyre config | `Carla/Blueprints/Vehicles/CommonTireConfig` |

## Stage 4 — Import, materials, physics asset

1. **Create the content folder** `$CARLA_CONTENT/<AssetName>/` with **four** sub-folders:

   | Folder | Contents |
   |---|---|
   | `Config` | `<AssetName>.Package.json` |
   | `Maps` | the level that carries the edited `VehicleFactory` |
   | `Static` | skeletal mesh, physics asset, skeleton, and the door/glass/light static meshes |
   | `Blueprints` | animation blueprint, wheel blueprints, actor blueprint |

   (Vehicles need no `Animations` folder — wheel and door motion are physics-driven, not clip-driven.)

2. **Import the FBXs** into `Static`:
   - **body** → check `Skeletal Mesh` and `Import Mesh`;
   - **doors, glass, lights** → leave `Skeletal Mesh` **unchecked**; they are static meshes.

3. **Rename** the three assets the body import created: `SK_<AssetName>`,
   `<AssetName>_PhysicsAsset`, `<AssetName>_Skeleton`.

4. **Import the textures.** Uncheck **sRGB** for Roughness, Metalness and Ambient Occlusion; leave
   it on for BaseColor and Emissive; set Normal maps to **Normalmap** compression. Set these before
   wiring.

5. **Wire the body materials** — Albedo → Base Color, Normal → Normal, Roughness → Roughness,
   Metalness → Metallic, AO → Ambient Occlusion, Emissive → Emissive Color, for every material
   instance the asset uses. **If a texture-sampler error appears, disconnect Metallic**
   (`ASSET_TRAPS.md` §5).

6. **Glass and lights do not get hand-authored materials.** Copy the Lincoln's two material
   instances into your `Static` folder and rename them for your asset:

   - `MI_GlassExt_Lincoln2020` → `MI_GlassExt_<AssetName>`
   - `MI_VehicleLights_Lincoln2020` → `MI_VehicleLights_<AssetName>`

   The lights material is what reads the 8-colour UV mask you painted in Stage 3; a hand-made
   material will not respond to CARLA's light state at all.

7. **Assign** the material instances to the corresponding meshes. If steps 4–7 are right, the
   skeletal mesh and physics asset preview with the asset's colours on their surfaces.

8. **Physics asset.** Open `<AssetName>_PhysicsAsset`; the import will have wrapped the vehicle in
   capsules.

   1. **Body** — change `Primitive Type` from Capsule to **Box**, press **Re-generate Bodies**, then
      orient and resize the box to enclose the whole body, with its lowest face level with the
      **half-height of the wheels**.
   2. **Wheels** — change `Primitive Type` to **Sphere**, **Re-generate Bodies**, then resize and
      position each sphere to enclose its wheel.
   3. On each **wheel** body, in Details:

      | Field | Value |
      |---|---|
      | Linear Damping | `0.0` |
      | Physics Type | **Kinematic** |
      | Simulation Generates Hit Events | checked |
      | Collision Complexity | **Use Simple Collision As Complex** |

      Set **Collision Complexity** on the **body** as well.

9. **Make the collision static mesh.** Open `SK_<AssetName>` and choose **Make Static Mesh**. Name
   it `SM_<AssetName>` and keep it beside the skeletal mesh. This becomes the actor's
   `CustomCollision` mesh in Stage 5.

10. **Save every package the import created** — meshes, skeleton, physics asset, materials, textures.
    **Save All** (`Ctrl+Shift+S`); UE's importer makes these as sibling packages and a mesh-only
    save loses them (`ASSET_TRAPS.md` §6).

> ### CHECKPOINT 4 — textured, and the collision bodies are right
> **Pass when:**
> 1. `SK_<AssetName>` previews textured, not grey, and "Compiling Shaders" has reached 0;
> 2. the physics asset shows a **box** around the body and a **sphere** on each wheel, with the
>    body box's underside level with the wheel centres — a body box that reaches the ground makes
>    the vehicle skid on its belly instead of rolling;
> 3. `SM_<AssetName>` exists;
> 4. **Save All** is done.
>
> Because the material was authored interactively, the eventual cook must be a **clean** cook.

## Stage 5 — Blueprints

1. **Animation blueprint.** Create an Animation Blueprint with parent class
   **`VehicleAnimInstance`** and skeleton `<AssetName>_Skeleton`. Name it `ABP_<AssetName>`.

   Open the Lincoln's `AnimBP_Lincoln2020_Animation`, copy its **AnimGraph** contents, and paste
   them into yours. Compile and save.

2. **Wheel blueprints.** In `Blueprints`, create a Blueprint Class with parent class
   **`VehicleWheel`** — one per axle, named `BP_<AssetName>_FW` and `BP_<AssetName>_RW`.

   | Field | Front | Rear |
   |---|---|---|
   | Collision Mesh | `Wheel_Shape` | `Wheel_Shape` |
   | Affected by Handbrake | unchecked | **checked** |
   | Tire Config | `CommonTireConfig` | `CommonTireConfig` |
   | Steer Angle | `70` | `0` |
   | Shape Radius / Width | measure in Blender | measure in Blender |

   Optionally tune `Lat Stiff Max Load`, `Lat Stiff Value`, `Long Stiff Value`,
   `Max Brake Torque`, `Max Hand Brake Torque` against the Lincoln's wheel blueprints.

3. **Actor blueprint.** Create a Blueprint Class with parent class **`BaseVehiclePawn`**, named
   `BP_<AssetName>`.

   > `BaseVehiclePawn` (`Carla/Blueprints/Vehicles/BaseVehiclePawn`) is the class every CARLA
   > four-wheeled vehicle derives from; it is itself a blueprint over the native
   > `CarlaWheeledVehicle`. **Do not use `VehicleWheel` here** — that is the wheel class from
   > step 2 and a vehicle parented to it will not be a pawn at all.

   Then configure it.

   1. **`Mesh` component** — `Anim Class` = `ABP_<AssetName>`, `Skeletal Mesh` = `SK_<AssetName>`.

   2. **`CustomCollision` component** — `Static Mesh` = `SM_<AssetName>`.

   3. **`Vehicle Movement` component** — set each `Wheel Class` to match the **bone name**: bones
      `Wheel_Front_Left` / `Wheel_Front_Right` → `BP_<AssetName>_FW`; the rear pair →
      `BP_<AssetName>_RW`. Optionally tune `Max RPM`, `Gear Switch Time`,
      `Gear Auto Box Latency`, `Final Ratio`.

   4. **Attach the door, glass and light static meshes** under the `Mesh` component (drag and drop).

   5. **Each door mesh:**

      | Field | Value |
      |---|---|
      | Parent Socket | the door's bone name |
      | Simulate Physics | checked |
      | Mass In Kg | checked, ≈ `50.0` |
      | Enable Gravity | **unchecked** |
      | Collision Presets | `BlockAll` |
      | Receive Decals | unchecked |
      | Component Tags | one element: `paint` |

   6. **Each glass mesh:** Collision Presets `NoCollision`, Receive Decals unchecked. **Parent each
      door's glass under that door**, so it swings with it.

   7. **Each light mesh:** Collision Presets `NoCollision`, Receive Decals unchecked,
      `Override Materials` = `MI_VehicleLights_<AssetName>`, Component Tags: one element
      `emissive`.

   8. **Door physics constraints.** Add one **Physics Constraint** component per door, named with
      the door's position suffix (`FL`, `FR`, `RL`, `RR`):

      | Field | Value |
      |---|---|
      | Rotation → Z | `180` (aligns the constraint's X zero with the door) |
      | Parent Socket | the door's bone name |
      | Component Name 1 | the door static mesh |
      | Component Name 2 | `VehicleMesh` (the parent of `Mesh`) |
      | Disable Collision | unchecked |
      | Swing 1 Motion | Limited |
      | Swing 2 Motion | Locked |
      | Twist Motion | Locked |
      | Swing 1 Limit | `30` degrees of travel |
      | Angular Rotation Offset Z | `-30.0` right / `+30.0` left — the **open** position |
      | Angular Drive Mode | Twist and Swing |
      | Target Orientation Z | `+30.0` right / `-30.0` left — the **closed** position |
      | Target Orientation → Strength | `10000` |
      | Target Orientation → Swing | checked |

      Angular Rotation Offset and Target Orientation are negatives of each other; the sign
      convention is what makes a door open outwards rather than through the body.

   9. **Lights.** Add Light components. The quickest correct route is to copy the whole light
      component set from the Lincoln's `BP_Lincoln2020` and then move and rotate each light to sit
      behind the matching lamp mesh.

   10. **Select `self`** in the Components tab and set:

       | Light parameter | Intensity |
       |---|---|
       | Position | `300000` |
       | Low Beam | `550000` |
       | High Beam | `500000` |
       | Brake | `500000` |
       | Reverse | `100000` |
       | Right / Left Blinker | `500000` |
       | Fog | `500000` |

       Then **Door Animation → Constraint Component Name**: add one array element per physics
       constraint, named exactly as the constraints are, **in the order FL → FR → RL → RR**. The
       order is positional; getting it wrong opens the wrong door.

   Compile and save.

> ### CHECKPOINT 5 — the blueprint is complete
> **Pass when**, in the blueprint editor: it compiles with no errors; the four wheel classes are
> assigned to the matching bones; every door has a physics constraint and appears in the
> `Constraint Component Name` array in FL→FR→RL→RR order; each light mesh has the lights material
> instance overridden and the `emissive` tag; each door mesh has the `paint` tag.

## Stage 6 — Register: `VehicleFactory`, level, `Package.json`

1. Under `Content` (the root), find and open **`VehicleFactory`**
   (`Carla/Blueprints/Vehicles/VehicleFactory`).

2. Add an element to the `Vehicles` list:

   | Field | Value |
   |---|---|
   | `Make` | the manufacturer, e.g. `Honda` — CARLA lowercases it into the ID |
   | `Model` | the model name in CamelCase, e.g. `PoliceCar` — also lowercased into the ID |
   | `Class` | `BP_<AssetName>` |
   | `Number of Wheels` | as appropriate |
   | `Generation` | `0` |
   | `Has Dynamic Doors` | **checked** |
   | `Has Lights` | **checked** |

   Compile and save.

   > **`Has Dynamic Doors` and `Has Lights` are not cosmetic.** Bench2Drive scenarios pass an
   > `attribute_filter` to `create_blueprint`, and a blueprint that *lacks* a filtered attribute is
   > removed from the candidate list. If the list empties, the resulting error is caught by the
   > same handler as an unknown model and CARLA silently substitutes `vehicle.tesla.model3`. A
   > fully-registered vehicle can be replaced by a Tesla purely because this box was unchecked.
   > See `ASSET_TRAPS.md` §1(b).

3. In your asset's `Maps` folder, create a **Level** named `<AssetName>Map` and open it.

4. **Drag into the level** the actor blueprint `BP_<AssetName>` **and** the edited `VehicleFactory`.
   Two objects. Save.

   The factory actor in the level is what causes the edited factory to be cooked into your package.
   Without it, the package installs and the vehicle is still not registered.

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
       "vehicles": [
           {
               "name": "<assetname lowercased>",
               "path": "/Game/<AssetName>/Blueprints/BP_<AssetName>.BP_<AssetName>"
           }
       ]
   }
   ```

   As with walkers, the `vehicles[]` entry does not register anything at runtime — the ID comes
   from the factory Make/Model. Keep it correct anyway as a cheap consistency signal.

> ### CHECKPOINT 6 — the factory entry exists and you know the real ID
> **Pass when:** `VehicleFactory` lists your entry with `Has Dynamic Doors` and `Has Lights` both
> checked, `<AssetName>Map` contains exactly the two actors, and `Package.json` parses.
>
> **Then find out what the blueprint ID actually is** rather than assuming it. With a server
> running, ask:
>
> ```bash
> python -c "import carla; bl=carla.Client('localhost',2000).get_world().get_blueprint_library(); \
>            print('\n'.join(sorted(b.id for b in bl.filter('vehicle.*'))))"
> ```
>
> Record the exact ID; every later step takes it as input. This is the number-one time sink in the
> whole procedure.

## Stage 7 — Spawn, drive, doors, lights

Play in the editor (`Alt+P`) and drive the Python API against it. Test four things:

1. **Registration and type** — `blueprint_library.filter(<id>)` returns exactly your vehicle, it
   spawns, and `actor.type_id == <id>` exactly.
2. **Attribute preconditions** — for every scenario you intend to run, evaluate that scenario's
   `attribute_filter` against your blueprint's real attributes. Any scenario whose filter would
   empty the candidate list is a hard failure: fix the factory entry, do not run the route.
3. **Driving** — apply throttle and confirm the vehicle accelerates, reaches speed, and comes to
   rest without sinking, hopping or spinning on the spot.
4. **Doors and lights** — open and close each door via the CARLA door API and confirm the right one
   moves; switch each light state and confirm the right lamp illuminates.

A probe implementing 1–3 plus render capture:

```bash
python stages/vehicle/carla_vehicle_probe.py \
    --vehicle_name <AssetName> --blueprint_id vehicle.<make>.<model> \
    --scenarios <Scenario1,Scenario2,...> \
    --gate_dir <renders/> --out <verdict.json> \
    --host localhost --port 2000
```

> ### CHECKPOINT 7 — it is real, registered, drivable, and complete
> **Pass when:** all four tests above pass, the renders show a textured vehicle at the right scale,
> and no requested scenario's `attribute_filter` would discard it.
>
> A vehicle that spawns but does not reach speed usually has a physics-asset problem — go back to
> CHECKPOINT 4 and check that the body box does not reach the ground and that the wheel spheres are
> centred.

## Stage 8 — Cook and install

1. **Close the editor**, then cook from the Python 3.8 build environment:

   ```bash
   cd "$CARLA_SRC"
   make package ARGS="--packages=<AssetName>"      # --packages takes the CONTENT FOLDER name
   ```

   Clean cook — see CHECKPOINT 4.

2. **Install** into every CARLA you will evaluate with. The cook writes
   `$CARLA_SRC/Dist/<AssetName>_0.9.15-dirty.tar.gz`:

   ```bash
   cp "$CARLA_SRC/Dist/<AssetName>_0.9.15-dirty.tar.gz" "$CARLA_PKG/Import/"
   cd "$CARLA_PKG" && bash ImportAssets.sh
   ```

   > **`$CARLA_PKG` must be the build you actually evaluate with.** The cook target
   > (`$CARLA_SRC/Dist/`) and the install target (`$CARLA_PKG`) are different directories and are
   > frequently on different machines. Installing into one CARLA does not install into another —
   > and a route run against a build that never received the tarball scores a Tesla
   > (`ASSET_TRAPS.md` §3). If you cook on a workstation and evaluate on a cluster, run this step
   > on both.

   > ⚠ **Reconstructed step — confirm before publishing.** The source document from which this
   > procedure was transcribed is truncated mid-sentence at *"Then import the asset via"*. The
   > `bash ImportAssets.sh` invocation above is reconstructed from the identical closing steps of
   > the static and walker procedures, which both end with copying the `Dist/` tarball into
   > `Import/` and running `ImportAssets.sh`. It has not been confirmed against the original
   > author's intent, and a vehicle may require an additional step the other two do not.

> ### CHECKPOINT 8 — registered in the build you will evaluate with
> `ImportAssets.sh` exits **2** when it skips newer shared Engine files. Normal. Judge by artifacts:
>
> ```bash
> test -d "$CARLA_PKG/CarlaUE4/Content/<AssetName>" && echo "content installed"
>
> # against a RUNNING server from $CARLA_PKG:
> python -c "import carla; bl=carla.Client('localhost',2000).get_world().get_blueprint_library(); \
>            print([b.id for b in bl.filter('vehicle.<make>.<model>')])"
> ```
>
> **Pass when both succeed, on every build.** A `strings(1)` grep of the installed
> `VehicleFactory` is a useful smoke test but is **not** sufficient — only a live server settles
> registration.
>
> Note that because `VehicleFactory` is base content, this package **overwrites base assets** and
> is locked to CARLA 0.9.15 exactly.

## Stage 9 — Route check

Run the vehicle scenarios that use the asset, end to end, in the packaged build.

```bash
python stages/vehicle/vehicle_route_run_gen.py \
    --vehicle_name <AssetName> --blueprint_id vehicle.<make>.<model> \
    --out <verdict.json>
```

Start the standalone simulator (`bash "$CARLA_PKG/CarlaUE4.sh"`), run the generated scripts, then
parse each result.

> ### CHECKPOINT 9 — real routes actually used the vehicle
> **Pass when, for every scenario:**
> 1. route status is `Completed` or `Perfect` and harness `entry_status` is `Finished`;
> 2. the log contains **no** `Actor model … not available. Using instead …` line naming **your**
>    blueprint ID (other vehicles falling back is informational — several scenarios legitimately
>    request generic `vehicle.*` blockers of their own);
> 3. the log shows **no** spawn failure or scenario skip for your vehicle.
>
> Condition 3 is not hypothetical for large vehicles: an oversized bounding box makes
> `try_spawn_actor` return `None`, the leaderboard skips the scenario, and the route records
> `Completed` with a near-perfect score. Three routes are permanently excluded from
> OOD-PerceptionBench for exactly this reason.

---

## Checkpoint summary

| # | Verify | Cheapest evidence |
|---|---|---|
| 1 | Scale, shift class | `vehicle_dimension_checker.ipynb` verdict == declared class |
| 2 | Weights, wheel rotation | Weight Paint per group; `r` on each wheel bone |
| 3 | Light mask | every lamp's UV island inside one correct region |
| 4 | Textured; physics bodies | shaders 0; body **box**, wheel **spheres**; `SM_<AssetName>` exists |
| 5 | Blueprint complete | compiles; wheels by bone; constraints FL→FR→RL→RR; tags set |
| 6 | Factory entry + real ID | factory lists it, doors/lights checked; live `vehicle.*` query |
| 7 | Real, drivable, complete | probe verdict `ok: true`; no scenario filter discards it |
| 8 | Registered where it matters | content dir on disk **and** live blueprint query, per build |
| 9 | Routes used it | completed **and** no fallback line naming your ID **and** no skip |

---

## Changes from the internal source document

This procedure was transcribed from an internal working note. Defects found and corrected:

| # | Defect | Resolution |
|---|---|---|
| 1 | A `WalkerFactory` registration block (with `Id` / `Gender` / `Age` / `Speed`) was spliced into the middle of the vehicle flow after step 16, with the numbering restarted. A literal reader would have registered a vehicle in the walker factory. | Removed. `VehicleFactory` registration is Stage 6 step 2. |
| 2 | The document ended mid-sentence at *"Then import the asset via"*. | Reconstructed as `bash ImportAssets.sh` in Stage 8, **explicitly marked as unconfirmed**. |
| 3 | The cooked package was copied to a hardcoded path that was not the build used for evaluation. | Replaced with `$CARLA_PKG`, plus an explicit warning that cook target and install target differ. |
| 4 | Step 7 pointed at the **static** dimension checker for vehicle sizing. | Corrected to `vehicle_dimension_checker.ipynb`, with the vehicle Z-score rule stated inline. |
| 5 | Step 13 set the whole-car blueprint's parent class to `VehicleWheel`. | Corrected to `BaseVehiclePawn`. Verified: every CARLA stock vehicle blueprint and every previously-imported custom vehicle has parent `BaseVehiclePawn`; `VehicleWheel` is the wheel class used at Stage 5 step 2. |
| 6 | "Create 5 sub-folders", followed by a list of 4. | Corrected to 4 (`Config`, `Maps`, `Static`, `Blueprints`); vehicles need no `Animations` folder. |
| 7 | Asset-rename examples (`SK_Cow`, `Cow_PhysicsAsset`, `CowMap`) were left over from the walker procedure. | Replaced with vehicle examples. |
| 8 | Glass / lights material instances and the reference animation blueprint were cited by absolute path into a privately-imported vehicle package. | Repointed at CARLA's stock `LincolnMKZ2020`, which is base content present in any source build. |

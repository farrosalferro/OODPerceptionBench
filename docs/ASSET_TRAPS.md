# Asset traps

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**

Read this before you start any of the three import procedures. Every item below is a real failure
we hit and lost time to. They share one property that makes them dangerous: **almost none of them
raise an error.** The simulator keeps running, the route reaches `Completed`, a Driving Score is
recorded, and the number looks entirely reasonable.

---

## 1. The silent fallback — the one that matters

**A missing asset does not crash CARLA.**

`try_spawn_actor('static.prop.roadclosedsign')` on a build where that prop was never installed does
not raise. The prop is simply absent, the ego drives an unobstructed road, and the route finishes
with a plausible Driving Score. Nothing in the log says the benchmark just measured nothing.

For **vehicles** there is a second, nastier variant. Bench2Drive's `CarlaDataProvider.create_blueprint`
falls back to the generic `car` category when it cannot resolve a requested model — and the generic
`car` category resolves to **`vehicle.tesla.model3`**. So the route does not even run empty: it runs
with a completely ordinary sedan standing in for your OOD vehicle, which is *precisely* the
in-distribution object the benchmark is supposed to contrast against.

There are three separate ways to reach that fallback.

**(a) The ID is not registered at all.** The package is on disk but the factory entry is missing
(see §2), or the package was installed into a different CARLA than the one being launched.

**(b) The ID is registered but the scenario's `attribute_filter` removes it.** This one is
vehicle-only and has no walker analogue:

```python
blueprints = library.filter(model)
for key, value in attribute_filter.items():
    blueprints = [x for x in blueprints if check_attribute_value(x, key, value)]
blueprint = rng.choice(blueprints)      # ValueError when the list is now EMPTY
```

`check_attribute_value` returns `False` when a blueprint simply **lacks** the attribute, and the
resulting `ValueError` is caught by the *same* `except` branch as an unknown model. Same warning
line, same Tesla. A scenario filtering on `{"has_dynamic_doors": True}` will silently discard a
perfectly-registered vehicle whose blueprint does not advertise that attribute.

**(c) The blueprint exists but cannot be placed.** A large vehicle's bounding box clips static
geometry, `try_spawn_actor` returns `None`, the leaderboard *skips* the scenario, and the route
records `Completed` with a near-perfect score. Three routes are permanently excluded from
OOD-PerceptionBench for exactly this reason.

### The guard

Never infer success from "the route completed". Assert the actual spawned actor:

```python
actor = world.try_spawn_actor(bp, transform)
assert actor is not None, f"{requested_id} did not spawn (placement failure)"
assert actor.type_id == requested_id, (
    f"spawned {actor.type_id!r}, route asked for {requested_id!r} — silent fallback"
)
```

Both assertions are needed. The first catches (c); the second catches (a) and (b). Every procedure
in this directory ends with a checkpoint that performs them.

For a full benchmark sweep, run the acceptance harness rather than eyeballing scores: its first and
most important assertion is that the actor CARLA actually spawned has the `type_id` the route XML
asked for.

---

## 2. Registration asymmetry

| Category | Blueprint ID comes from | Editing it |
|---|---|---|
| **Prop** | `Content/<Name>/Config/<Name>.Package.json` → `props[].name` | plain JSON, no re-cook |
| **Walker** | cooked base content `Carla/Blueprints/Walkers/WalkerFactory` | `.uasset`, needs the editor + a re-cook |
| **Vehicle** | cooked base content `Carla/Blueprints/Vehicles/VehicleFactory` | `.uasset`, needs the editor + a re-cook |

Two consequences people get wrong:

1. **`Package.json` alone does not register a walker or a vehicle.** The `name` there is the
   *packaging* identity. The blueprint ID the Python API answers to comes from the factory. A
   walker package can install cleanly, put all its assets on disk, and still not exist as a
   blueprint. There is no error at any point.

2. **The blueprint ID does not have to resemble the package name.** A content folder called
   `SUV_Import`, whose `Package.json` says `vehicles[].name: suv_import`, can register as
   `vehicle.ood.suv` — because Make and Model come from the `VehicleFactory` entry, not the
   JSON. **Do not guess the ID.** Ask a running server:

   ```bash
   python -c "import carla; bl=carla.Client('localhost',2000).get_world().get_blueprint_library(); \
              print('\n'.join(sorted(b.id for b in bl.filter('vehicle.*'))))"
   ```

   If your vehicle is not in that list it is not registered. No amount of route-running will tell
   you anything useful until it is.

3. Because the factories are **base** content, a walker or vehicle package overwrites base assets
   on install. That hard-locks the package to CARLA 0.9.15 and means a "slim content pack over a
   stock CARLA" must ship the factory assets too.

---

## 3. One asset, two CARLAs

If you cook on one machine and evaluate on another — a workstation with the UE build and a cluster
with the simulator, say — **installing into one does not install into the other**. The cook step
and the install step are different operations with different targets, and a route submitted to the
build that never received the tarball hits trap §1(a) and scores a Tesla.

Install into every CARLA you will evaluate with, and verify each one with a live blueprint-library
query. A static `strings(1)` grep of the factory `.uasset` is necessary but **not sufficient** —
only a running server settles registration.

---

## 4. Measuring the bounding box

- **Axes are L = X, W = Y, H = Z**, matching the FBX export convention the procedures use
  (X Forward, Z Up). Getting this wrong swaps length and width and can silently reclassify an
  asset's shift level.
- **Exclude UE collision meshes from the measurement.** Any object whose name begins with `UCX_`,
  `UBX_`, `USP_`, `UCP_` or `MCDCX_` is a collision hull, not render geometry. Collision hulls are
  routinely larger than the mesh they wrap, so including them inflates the bounding box — enough,
  in practice, to push a visual-shift prop into the ambiguous band.

---

## 5. GATE-2 sampler error: disconnect Metallic

When wiring textures into the material, a recurring editor error names a texture sampler and
refuses to compile. In every case we saw, the cause was the **Metallic** input: source assets
frequently ship a packed ORM/roughness map that is not a valid single-channel metallic input, and
the material graph will not resolve it.

**Disconnect the Metallic input.** The prop renders correctly without it. This is not a
workaround for a cosmetic problem — while the sampler error stands, the material does not compile,
and an uncompiled material cooks to grey (see §6).

---

## 6. UE's FBX importer creates sibling packages — and an SM-only save loses them

Importing an FBX creates the static/skeletal mesh **and**, as *separate sibling packages*, a
material and one texture package per image. A save that targets only the mesh asset does not
persist those siblings.

The failure is silent and delayed:

1. You import, wire up the material, and save the mesh.
2. The cook runs and **exits 0**.
3. In the simulator the asset appears with the grey **WorldGridMaterial checkerboard**.

Nothing failed. The material package was simply never written, so the cook had nothing to cook.

**The fix:** enumerate every package created by the import and save each one explicitly — the mesh,
the material(s), and every texture. If you are scripting this, treat "no material package found for
this mesh" as a **hard failure**, not a warning; there is no downstream check that will catch it.

A related consequence in this UE 4.26 build: **headlessly-authored materials cook to an invalid
shader** and also render as the grey checkerboard. Only the interactive editor compiles real
shaders into the derived-data cache. If you author a material in the GUI after a previous headless
attempt, the following cook must be a **clean** cook — otherwise it reuses the empty shader cache
(`ShadersCompiled=0`) and the asset stays grey. A successful clean cook logs
`Missing cached shader map … compiling` with `ShadersCompiled > 0`.

---

## 7. `tar` exit code 2 on install is normal

`ImportAssets.sh` extracts over an existing installation with `--keep-newer-files`. Skipping shared
Engine files that are already newer makes `tar` exit **2**. This is not an error.

Judge install success by the presence of `Content/<AssetName>/` on disk in the target CARLA and by
a live blueprint-library query — never by the exit code.

---

## 8. Cook success is a verdict, not an exit code

More generally: for every Unreal or Blender step in these procedures, **do not treat the process
exit code as the result.** Both tools exit 0 on a range of partial failures. Check the artifact:
does the `.uasset` exist, does the package appear in `Dist/`, does the blueprint library list the
ID, does the spawned actor's `type_id` match. Each procedure's checkpoints are written in those
terms for this reason.

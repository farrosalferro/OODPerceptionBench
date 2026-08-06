# Stage scripts

**Artifact version: v0.9 — corresponds to arXiv v1 of the OOD-PerceptionBench paper.**

Standalone, headless helpers used by the checkpoints in the three import procedures
([static](../import_procedure_static.md) · [walker](../import_procedure_walker.md) ·
[vehicle](../import_procedure_vehicle.md)). Each one does a single job, writes a JSON verdict, and
is meant to be run by hand at the point in the procedure that names it. There is no orchestrator:
the procedure documents *are* the runbook.

---

## Configure once

Every machine-specific location comes from a site config. **There are no defaults.** A stage that
needs a path you have not configured stops and tells you which key it wants, rather than falling
back to something plausible-looking — which is how an asset silently ends up cooked into a CARLA
nobody launches.

```bash
cp site_config.example.yaml site_config.yaml
$EDITOR site_config.yaml
python site_config.py --check          # prints what resolves; non-zero if anything required is unset
```

Resolution order, first hit wins:

1. `--config <file>` on the stage script
2. `$OODPB_SITE_CONFIG`
3. `site_config.yaml` beside these scripts
4. `site_config.yaml` in the current directory

Any single key can be overridden with `OODPB_<KEY_UPPERCASED>`, e.g. `OODPB_CARLA_PKG=/opt/other`.

The config file is a flat `key: value` mapping. PyYAML is used when available and a small built-in
parser otherwise, so the same config works from Unreal's embedded python and Blender's bundled
python without installing anything into them.

---

## Layout

```
stages/
  site_config.py            resolver + `--check` preflight
  site_config.example.yaml  copy this
  secondary_ingest.sh       optional: install a cooked package into a SECOND CARLA
  static/                   static-prop stages
  walker/                   walker stages
  vehicle/                  vehicle stages
```

`walker/` and `vehicle/` import `static/common.py` for the shared verdict envelope and the resolved
toolchain paths; they do not fork it. Keep the three directories together.

## What each stage does

### `static/`
| Script | Procedure step | Runs under |
|---|---|---|
| `dimension_check.py` | classify L/W/H against the anchor; `--expect` makes it a gate | any python |
| `texture_classify.py` | sort textures into PBR roles; split packed ORM / metallicRoughness maps | python + Pillow |
| `bpy_align_export.py` | align to the anchor, uniform-scale, export FBX, render for CHECKPOINT 1 | Blender |
| `bpy_textured_render.py` | textured preview render before the cook | Blender |
| `build_master_material.py` | one-time: create the shared master material | Unreal (headless) |
| `ue_import_material_collision.py` | import mesh + textures, instance the material, set collision | Unreal (headless) |
| `tier1_collision_verify.py` | re-verify collision on an already-imported mesh | Unreal (headless) |
| `carla_probe_test.py` | CHECKPOINT 4: spawn, assert `type_id`, drive a probe into it | python + `carla` |
| `cook_package.sh` | `make package` + install into `carla_pkg` | bash |
| `mark_state.py` | update the resumable state file | any python |

### `walker/`
| Script | Procedure step | Runs under |
|---|---|---|
| `walker_sizing.py` | derive the UE capsule from target dimensions and classify it (CHECKPOINT 4) | any python |
| `ue_walker_import.py` | import skeletal mesh, skeleton, physics asset, clips, textures | Unreal (headless) |
| `ue_walker_clone.py` | scaffold `BS_`/`ABP_`/`BP_` from a template walker — naming and parent class only | Unreal (headless) |
| `ue_walker_finalize.py` | build `<Name>Map` and write `Package.json` (run with the editor closed) | Unreal (headless) |
| `carla_walker_probe.py` | CHECKPOINT 6: registration, `type_id`, animation, render | python + `carla` |
| `walker_route_run_gen.py` | emit one route XML + run script per pedestrian scenario | any python |
| `parse_route_result.py` | CHECKPOINT 8: completed **and** no fallback line **and** no skip | any python |
| `cook_walker_package.sh` | `make package` + install into `carla_pkg` | bash |
| `mark_state.py`, `mark_import_state.py` | update the resumable state files | any python |

### `vehicle/`
| Script | Procedure step | Runs under |
|---|---|---|
| `carla_vehicle_probe.py` | CHECKPOINT 7: registration, `type_id`, **`attribute_filter` preconditions**, driving, render | python + `carla` |
| `vehicle_route_run_gen.py` | emit one route XML + run script per vehicle scenario | any python |
| `parse_route_result.py` | CHECKPOINT 9: completed **and** no fallback naming *your* ID **and** no skip | any python |
| `cook_vehicle_package.sh` | `make package` + install into `carla_pkg` | bash |
| `mark_state.py` | update the resumable state file | any python |

`carla_vehicle_probe.py`'s `attribute_filter` check has no walker analogue and is the reason the
vehicle path needs its own probe: a fully-registered vehicle can still be silently replaced by
`vehicle.tesla.model3` if a scenario filters on an attribute its blueprint does not advertise. See
[`../ASSET_TRAPS.md`](../ASSET_TRAPS.md) §1(b).

---

## Two rules that apply to all of them

**1. Success is the verdict file, not the exit code.** Every stage writes
`{"stage": ..., "ok": true|false, "data": {...}, "error": ..., "ts": ...}` to `--out`. Headless
Unreal in particular segfaults on teardown after doing all its work correctly, and `tar` exits 2
on a perfectly good install. Read the JSON.

**2. Content on disk is necessary but not sufficient.** Registration is only settled by a live
blueprint-library query against a running server started from the build in question.

---

## Verified

- `python site_config.py --check` against both `site_config.example.yaml` and a filled config.
- `static/dimension_check.py --selftest --reference <benchmark>/data/fixed/static_classification.json`
  reproduces the committed classification for all 8 static props (8/8, no failures).
- `static/dimension_check.py --L … --W … --H … --expect geometric` exits 3 on a mismatch.
- `walker/walker_sizing.py` on an adult-like and a cow-like capsule returns
  `level_1_visual` / `level_2_geometric` under the published union-of-molds rule.
- `static/route_run_gen.py --no_write` renders a route XML and run script whose every absolute
  path comes from the config — no absolute developer-machine path survives into the output.
- `--help` works on every stage with **no** site config present.
- All Python compiles; all shell scripts pass `bash -n`.

**Not verified here** (they need Unreal 4.26, Blender, or a running CARLA, none of which were
available in the environment these scripts were parameterized in): `bpy_align_export.py`,
`bpy_textured_render.py`, `build_master_material.py`, `ue_import_material_collision.py`,
`tier1_collision_verify.py`, `ue_walker_*.py`, `carla_probe_test.py`, `carla_walker_probe.py`,
`carla_vehicle_probe.py`, `texture_classify.py` (needs Pillow), the three `cook_*.sh` scripts, and
`secondary_ingest.sh`. Their configuration surface was changed in exactly one place each — the
resolved-path lookup — and both the resolver and the modules that feed them are exercised above,
but treat the first real run of each as the actual test.

## Known gap

The **route/run templates** these generators read live in the benchmark repository
(`<bench_root>/routes/templates/<category>/`), not here, and the checked-in copies still contain
their author's absolute paths as defaults. The generators overwrite every one of those variables
from the site config, so the *emitted* scripts are clean — but the templates themselves should be
de-hardcoded by whoever owns that directory. Until then, do not run a template directly.

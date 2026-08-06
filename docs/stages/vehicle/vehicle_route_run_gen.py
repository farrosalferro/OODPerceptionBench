#!/usr/bin/env python3
"""
vehicle_route_run_gen.py — Stage B core: route XML + run .sh generation for a vehicle.

The vehicle analogue of pedestrian_check/stages/walker_route_run_gen.py, emitting ONE
(route XML, run .sh) pair PER vehicle scenario (default all 6). For each scenario it fills the
committed per-scenario template:

  route XML  (data/import_check/vehicle/<subfolder>/route_template.xml):
     route id   route_<N>_template          -> route_<N>_<slug>
     blueprint  vehicle.ood.template        -> <blueprint_id>
     (waypoints, town, trigger_point, distance/direction/speed/offset/frequency, weathers kept
      verbatim — these are real validated benchmark routes; only the asset suffix + blueprint change)
   -> data/import_check/vehicle/<subfolder>/route_<N>_<slug>.xml

  run .sh   (perception/import_check/vehicle/run_template.sh):
     <scenario>     -> subfolder
     <route_file>   -> route_<N>_<slug>.xml
     <vehicle_slug> -> slug   (checkpoint json filename + save-dir grouping)
     CARLA_ROOT     -> $CARLA_PKG   (the install-target rule)
   -> perception/import_check/vehicle/<subfolder>/run_<slug>.sh  (chmod +x)

NB the blueprint substitution is keyed on the VALUE token, never the attribute name: unlike the
4 pedestrian scenarios (all `pedestrian_blueprint`), the 6 vehicle scenarios carry the id under
FOUR different attribute names — front_vehicle_model, blueprint_name, parked_vehicle_model,
cut_in_vehicle_model. Keying on the value keeps one code path for all six.

Town comes from the route XML (config.town); the run script's TOWN var is vestigial.
Emits a make_verdict envelope listing every generated artifact to --out.

CLI:
  vehicle_route_run_gen.py --manifest <m.yaml> [--out_root DIR] [--out verdict.json] [--no_write]
  vehicle_route_run_gen.py --vehicle_name <V> --blueprint_id vehicle.<make>.<model> \
        [--scenarios A,B,...] [--out_root DIR] [--out verdict.json] [--no_write]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vehicle_common as vc  # noqa: E402

STAGE = "route_check"
_HINT_RE = re.compile(r"\s*<!--\s*change template to vehicle name\s*-->")
_ROUTE_ID_RE = re.compile(r'id="(route_\d+_template)"')

# The single placeholder every vehicle route template must carry. It deliberately keeps the
# `.template` suffix so the defence-in-depth check below can catch a missed substitution.
BP_PLACEHOLDER = "vehicle.ood.template"


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def render_route_xml(template_text: str, names: vc.VehicleNames,
                     spec: dict | None = None) -> tuple[str, str, str]:
    """
    Return (rendered_xml, new_route_id, route_filename).

    `spec` is the VEH_SCENARIOS row. When given, the template is cross-checked against the
    table's `template_route` and `town`. Those two values are reported in the runbook operator plan
    and in the final PASS/FAIL table, so a template silently re-pointed at a different base
    route would make the report lie about which route/town actually ran.
    """
    m = _ROUTE_ID_RE.search(template_text)
    if not m:
        raise ValueError('route template missing id="route_<N>_template"')
    template_route_id = m.group(1)

    # Exactly ONE scenario per template. parse_route_result.py reads records[0] and matches
    # skips by scenario-name prefix; both assume a single-scenario route. A second block would
    # pass every other guard here and silently make the verdict ambiguous.
    n_scen = len(re.findall(r"<scenario\s", template_text))
    if n_scen != 1:
        raise ValueError(
            f"route template {template_route_id} carries {n_scen} <scenario> blocks; this "
            f"pipeline requires exactly 1 (the parser reads records[0])"
        )

    if spec is not None:
        want_id = f"{spec['template_route']}_template"
        if template_route_id != want_id:
            raise ValueError(
                f"template route id mismatch for {spec['class_name']}: file has "
                f"{template_route_id!r}, VEH_SCENARIOS says {want_id!r}. Update the table or "
                f"re-point the template — the plan and the report quote the table."
            )
        mt = re.search(r'<route id="[^"]+" town="([^"]+)"', template_text)
        town = mt.group(1) if mt else None
        if town != spec["town"]:
            raise ValueError(
                f"template town mismatch for {spec['class_name']}: file has {town!r}, "
                f"VEH_SCENARIOS says {spec['town']!r}"
            )
    new_route_id = template_route_id.replace("_template", f"_{names.slug}")
    fname = vc.route_filename(template_route_id, names.slug)

    out = template_text
    out = out.replace(f'id="{template_route_id}"', f'id="{new_route_id}"')

    # Guard the blueprint substitution the same way the route-id one is guarded: a silent no-op
    # here would leave value="vehicle.ood.template" in the route, which is unregistered -> the
    # scenario falls back to vehicle.tesla.model3 -> the exact false pass this pipeline exists to
    # prevent.
    bp_token = f'value="{BP_PLACEHOLDER}"'
    if bp_token not in out:
        raise ValueError(
            f'route template missing the blueprint placeholder value="{BP_PLACEHOLDER}" — '
            f'refusing to emit a route that would spawn an unregistered/placeholder vehicle'
        )
    out = out.replace(bp_token, f'value="{names.blueprint_id}"')
    out = _HINT_RE.sub("", out)

    # Defense in depth: never emit a route that still carries a '.template' placeholder.
    if ".template" in out:
        raise ValueError(
            f"generated route {new_route_id} still contains a '.template' placeholder after "
            f"substitution — check the template tokens; refusing to emit"
        )
    return out, new_route_id, fname


def render_run_sh(template_text: str, subfolder: str, route_file: str, slug: str,
                  carla_root: str | None = None) -> str:
    out = template_text
    out = out.replace("<scenario>", subfolder)
    out = out.replace("<route_file>", route_file)
    out = out.replace("<vehicle_slug>", slug)
    # the install-target rule: default the client PythonAPI at the CARLA build the asset was
    # cooked into, but emit the `${CARLA_ROOT:-...}` form so a scheduler job can export
    # CARLA_ROOT=$CARLA_PKG and reuse the SAME script. Authoring and evaluation CARLAs are
    # separate installs: an asset registered in one is NOT registered in the other, so pointing
    # a cluster job at the authoring default would silently fall back to vehicle.tesla.model3.
    root = carla_root or vc.CARLA_SERVER_ROOT
    out = re.sub(
        r"^CARLA_ROOT=.*$",
        f'CARLA_ROOT="${{CARLA_ROOT:-{root}}}"',
        out,
        count=1,
        flags=re.MULTILINE,
    )
    # Every remaining absolute path in the template is site-dependent too. Rewrite them from the
    # site config so the emitted script carries no location from whoever authored the template.
    _save = f"{vc.VEH_SAVE_ROOT}/{subfolder}"
    out = re.sub(r"^GARAGE_ROOT=.*$", f"GARAGE_ROOT={vc.REPO_ROOT}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^CHECKPOINT_ENDPOINT=.*$", f"CHECKPOINT_ENDPOINT={_save}/{slug}.json",
                 out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^SAVE_PATH=.*$", f"SAVE_PATH={_save}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^ROUTES=.*$", f"ROUTES={vc.VEH_ROUTE_DATA_DIR}/{subfolder}/{route_file}",
                 out, count=1, flags=re.MULTILINE)
    return out


def _out_route_path(subfolder: str, fname: str, out_root: str | None) -> str:
    if out_root is None:
        return vc.route_out_path(subfolder, fname)
    return os.path.join(out_root, "data", "import_check", "vehicle", subfolder, fname)


def _out_run_path(subfolder: str, slug: str, out_root: str | None) -> str:
    if out_root is None:
        return vc.run_out_path(subfolder, slug)
    return os.path.join(out_root, "scripts", "import_check", "vehicle", subfolder, f"run_{slug}.sh")


def gen(names: vc.VehicleNames, scenarios: list[str], write: bool = True,
        out_root: str | None = None, variant: str | None = None) -> dict:
    """
    variant: optional condition variant, e.g. "night". Uses
    `route_template_<variant>.xml` instead of `route_template.xml`, and suffixes the slug so the
    generated route / run script / CHECKPOINT never collide with the default-condition run.
    The VEH_SCENARIOS template_route/town cross-check is skipped for a variant, because a variant
    is deliberately based on a DIFFERENT benchmark route (one with the weather we want) — the
    single-<scenario> and blueprint-placeholder guards still apply.
    """
    with open(vc.VEH_RUN_TEMPLATE) as f:
        run_tpl = f.read()

    if variant:
        names = vc.VehicleNames(**{**names.__dict__, "slug": f"{names.slug}_{variant}"})

    per_scenario = []
    for class_name in scenarios:
        spec = vc.VEH_SCENARIO_BY_CLASS[class_name]
        subfolder = spec["subfolder"]
        if variant:
            tpl_path = os.path.join(vc.VEH_ROUTE_DATA_DIR, subfolder,
                                    f"route_template_{variant}.xml")
            if not os.path.isfile(tpl_path):
                raise FileNotFoundError(
                    f"no '{variant}' variant template for {subfolder}: {tpl_path}")
        else:
            tpl_path = vc.route_template_path(subfolder)
        with open(tpl_path) as f:
            route_tpl = f.read()

        route_xml, route_id, fname = render_route_xml(route_tpl, names,
                                                      None if variant else spec)
        run_sh = render_run_sh(run_tpl, subfolder, fname, names.slug)

        route_path = _out_route_path(subfolder, fname, out_root)
        run_path = _out_run_path(subfolder, names.slug, out_root)

        if write:
            os.makedirs(os.path.dirname(route_path), exist_ok=True)
            with open(route_path, "w") as f:
                f.write(route_xml)
            os.makedirs(os.path.dirname(run_path), exist_ok=True)
            with open(run_path, "w") as f:
                f.write(run_sh)
            os.chmod(run_path, 0o755)

        per_scenario.append({
            "class_name": class_name,
            "subfolder": subfolder,
            "blueprint_attr": spec["blueprint_attr"],
            "attr_filter": spec["attr_filter"],
            # Read the town from the RENDERED route, not from VEH_SCENARIOS. For a --variant the
            # table entry describes the default-condition template and would misreport (the night
            # parked_obstacle variant is Town05, the table says Town06). The plan and the final
            # report quote this field, so it must match what actually runs.
            "town": (re.search(r'<route id="[^"]+" town="([^"]+)"', route_xml).group(1)
                     if re.search(r'<route id="[^"]+" town="([^"]+)"', route_xml) else spec["town"]),
            "route_id": route_id,
            "route_file": fname,
            "route_path": route_path,
            "run_path": run_path,
            "run_cmd": f"bash {run_path}",
            "checkpoint_path": vc.checkpoint_path(subfolder, names.slug),
            "save_dir": vc.save_dir(subfolder),
        })

    return {
        "vehicle_name": names.vehicle_name,
        "blueprint_id": names.blueprint_id,
        "slug": names.slug,
        "carla_root": vc.CARLA_SERVER_ROOT,
        "written": bool(write),
        "n_scenarios": len(per_scenario),
        "scenarios": per_scenario,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate route XML + run .sh per vehicle scenario.")
    _site_config.add_config_arg(ap)
    ap.add_argument("--manifest", default=None, help="vehicle test manifest (yaml/json)")
    ap.add_argument("--vehicle_name", default=None, help="CamelCase label (if no --manifest)")
    ap.add_argument("--blueprint_id", default=None, help="vehicle.<make>.<model> (if no --manifest)")
    ap.add_argument("--scenarios", default=None, help="comma list of scenario class names (default all 6)")
    ap.add_argument("--out_root", default=None, help="write under this temp tree instead of committed locations")
    ap.add_argument("--variant", default=None,
                    help="condition variant, e.g. night -> uses route_template_<variant>.xml "
                         "and suffixes the slug (policecar_night) so nothing collides")
    ap.add_argument("--no_write", action="store_true", help="dry run: render but do not write files")
    ap.add_argument("--out", default=None, help="path to write the JSON verdict envelope")
    args = ap.parse_args(argv)
    _site_config.apply_config_arg(args)

    try:
        if args.manifest:
            m = vc.load_vehicle_manifest(args.manifest)
            names = vc.vehicle_names_for(m.vehicle_name, m.blueprint_id)
            scenarios = m.scenario_list
        else:
            if not (args.vehicle_name and args.blueprint_id):
                raise ValueError("provide --manifest, or both --vehicle_name and --blueprint_id")
            vc.validate_camelcase(args.vehicle_name)
            vc.validate_blueprint_id(args.blueprint_id)
            names = vc.vehicle_names_for(args.vehicle_name, args.blueprint_id)
            if args.scenarios:
                scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
                unknown = [s for s in scenarios if s not in vc.VEH_SCENARIO_BY_CLASS]
                if unknown:
                    raise ValueError(f"unknown scenario(s): {unknown}; valid: {sorted(vc.VEH_SCENARIO_BY_CLASS)}")
            else:
                scenarios = list(vc.DEFAULT_SCENARIOS)

        data = gen(names, scenarios, write=not args.no_write, out_root=args.out_root,
                   variant=args.variant)
        verdict = vc.make_verdict(STAGE, True, data=data)
        ok = True
    except Exception as e:  # noqa: BLE001
        verdict = vc.make_verdict(STAGE, False, error=f"{type(e).__name__}: {e}")
        ok = False

    if args.out:
        vc.write_verdict(args.out, verdict)
    print(json.dumps(verdict, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

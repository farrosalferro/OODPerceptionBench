#!/usr/bin/env python3
"""
walker_route_run_gen.py — Stage B core: route XML + run .sh generation for a walker.

The walker analogue of import_automation/stages/route_run_gen.py, but it emits ONE
(route XML, run .sh) pair PER pedestrian scenario (default all 4). For each scenario it
fills the committed per-scenario template:

  route XML  (data/import_check/pedestrian/<subfolder>/route_template.xml):
     route id   route_<N>_template            -> route_<N>_<slug>
     blueprint  walker.pedestrian.template    -> <blueprint_id>
     (waypoints, town, trigger_point, distance/direction/crossing_angle, weathers kept verbatim —
      these are real validated benchmark routes; only the asset suffix + blueprint change)
   -> data/import_check/pedestrian/<subfolder>/route_<N>_<slug>.xml

  run .sh   (perception/import_check/pedestrian/run_template.sh):
     <scenario>    -> subfolder
     <route_file>  -> route_<N>_<slug>.xml
     <walker_slug> -> slug   (checkpoint json filename + save-dir grouping)
     CARLA_ROOT    -> $CARLA_PKG   (the install-target rule)
   -> perception/import_check/pedestrian/<subfolder>/run_<slug>.sh  (chmod +x)

Town comes from the route XML (config.town); the run script's TOWN var is vestigial.
Emits a make_verdict envelope listing every generated artifact to --out.

CLI:
  walker_route_run_gen.py --manifest <m.yaml> [--out_root DIR] [--out verdict.json] [--no_write]
  walker_route_run_gen.py --walker_name <W> --blueprint_id walker.pedestrian.<id> \
        [--scenarios A,B,...] [--out_root DIR] [--out verdict.json] [--no_write]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import walker_common as wc  # noqa: E402

STAGE = "route_check"
_HINT_RE = re.compile(r"\s*<!--\s*change template to walker name\s*-->")
_ROUTE_ID_RE = re.compile(r'id="(route_\d+_template)"')


# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def render_route_xml(template_text: str, names: wc.WalkerNames) -> tuple[str, str, str]:
    """Return (rendered_xml, new_route_id, route_filename)."""
    m = _ROUTE_ID_RE.search(template_text)
    if not m:
        raise ValueError('route template missing id="route_<N>_template"')
    template_route_id = m.group(1)
    new_route_id = template_route_id.replace("_template", f"_{names.slug}")
    fname = wc.route_filename(template_route_id, names.slug)

    out = template_text
    out = out.replace(f'id="{template_route_id}"', f'id="{new_route_id}"')

    # Guard the blueprint substitution the same way the route-id one is guarded: a silent
    # no-op here would leave value="walker.pedestrian.template" in the route, which is
    # unregistered -> the scenario falls back to a stock actor/Tesla -> the exact false pass
    # this whole pipeline exists to prevent.
    bp_token = 'value="walker.pedestrian.template"'
    if bp_token not in out:
        raise ValueError(
            'route template missing the blueprint placeholder '
            'value="walker.pedestrian.template" — refusing to emit a route that would spawn '
            'an unregistered/placeholder walker'
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


def render_run_sh(template_text: str, subfolder: str, route_file: str, slug: str) -> str:
    out = template_text
    out = out.replace("<scenario>", subfolder)
    out = out.replace("<route_file>", route_file)
    out = out.replace("<walker_slug>", slug)
    # the install-target rule: point the client PythonAPI at the local /media server.
    out = re.sub(
        r"^CARLA_ROOT=.*$",
        f"CARLA_ROOT={wc.CARLA_SERVER_ROOT}",
        out,
        count=1,
        flags=re.MULTILINE,
    )
    # Every remaining absolute path in the template is site-dependent too. Rewrite them from the
    # site config so the emitted script carries no location from whoever authored the template.
    _save = f"{wc.PED_SAVE_ROOT}/{subfolder}"
    out = re.sub(r"^GARAGE_ROOT=.*$", f"GARAGE_ROOT={wc.REPO_ROOT}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^CHECKPOINT_ENDPOINT=.*$", f"CHECKPOINT_ENDPOINT={_save}/{slug}.json",
                 out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^SAVE_PATH=.*$", f"SAVE_PATH={_save}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^ROUTES=.*$", f"ROUTES={wc.PED_ROUTE_DATA_DIR}/{subfolder}/{route_file}",
                 out, count=1, flags=re.MULTILINE)
    return out


def _out_route_path(subfolder: str, fname: str, out_root: str | None) -> str:
    if out_root is None:
        return wc.route_out_path(subfolder, fname)
    return os.path.join(out_root, "data", "import_check", "pedestrian", subfolder, fname)


def _out_run_path(subfolder: str, slug: str, out_root: str | None) -> str:
    if out_root is None:
        return wc.run_out_path(subfolder, slug)
    return os.path.join(out_root, "scripts", "import_check", "pedestrian", subfolder, f"run_{slug}.sh")


def gen(names: wc.WalkerNames, scenarios: list[str], write: bool = True,
        out_root: str | None = None) -> dict:
    with open(wc.PED_RUN_TEMPLATE) as f:
        run_tpl = f.read()

    per_scenario = []
    for class_name in scenarios:
        spec = wc.PED_SCENARIO_BY_CLASS[class_name]
        subfolder = spec["subfolder"]
        tpl_path = wc.route_template_path(subfolder)
        with open(tpl_path) as f:
            route_tpl = f.read()

        route_xml, route_id, fname = render_route_xml(route_tpl, names)
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
            "route_id": route_id,
            "route_file": fname,
            "route_path": route_path,
            "run_path": run_path,
            "run_cmd": f"bash {run_path}",
            "checkpoint_path": wc.checkpoint_path(subfolder, names.slug),
            "save_dir": wc.save_dir(subfolder),
        })

    return {
        "walker_name": names.walker_name,
        "blueprint_id": names.blueprint_id,
        "slug": names.slug,
        "carla_root": wc.CARLA_SERVER_ROOT,
        "written": bool(write),
        "n_scenarios": len(per_scenario),
        "scenarios": per_scenario,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate route XML + run .sh per pedestrian scenario for a walker.")
    _site_config.add_config_arg(ap)
    ap.add_argument("--manifest", default=None, help="walker test manifest (yaml/json)")
    ap.add_argument("--walker_name", default=None, help="CamelCase label (if no --manifest)")
    ap.add_argument("--blueprint_id", default=None, help="walker.pedestrian.<id> (if no --manifest)")
    ap.add_argument("--scenarios", default=None, help="comma list of scenario class names (default all 4)")
    ap.add_argument("--out_root", default=None, help="write under this temp tree instead of committed locations")
    ap.add_argument("--no_write", action="store_true", help="dry run: render but do not write files")
    ap.add_argument("--out", default=None, help="path to write the JSON verdict envelope")
    args = ap.parse_args(argv)
    _site_config.apply_config_arg(args)

    try:
        if args.manifest:
            m = wc.load_walker_manifest(args.manifest)
            names = wc.walker_names_for(m.walker_name, m.blueprint_id)
            scenarios = m.scenario_list
        else:
            if not (args.walker_name and args.blueprint_id):
                raise ValueError("provide --manifest, or both --walker_name and --blueprint_id")
            wc.validate_camelcase(args.walker_name)
            wc.validate_blueprint_id(args.blueprint_id)
            names = wc.walker_names_for(args.walker_name, args.blueprint_id)
            if args.scenarios:
                scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
                unknown = [s for s in scenarios if s not in wc.PED_SCENARIO_BY_CLASS]
                if unknown:
                    raise ValueError(f"unknown scenario(s): {unknown}; valid: {sorted(wc.PED_SCENARIO_BY_CLASS)}")
            else:
                scenarios = list(wc.DEFAULT_SCENARIOS)

        data = gen(names, scenarios, write=not args.no_write, out_root=args.out_root)
        verdict = wc.make_verdict(STAGE, True, data=data)
        ok = True
    except Exception as e:  # noqa: BLE001
        verdict = wc.make_verdict(STAGE, False, error=f"{type(e).__name__}: {e}")
        ok = False

    if args.out:
        wc.write_verdict(args.out, verdict)
    print(json.dumps(verdict, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

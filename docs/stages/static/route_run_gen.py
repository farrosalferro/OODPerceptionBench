#!/usr/bin/env python3
"""
route_run_gen.py — Agent 6 core: route XML + run .sh generation (see the procedure document).

Generates, from the committed templates, the two integration-check artifacts for one
imported static prop:

  1. The route XML  (from common.ROUTE_TEMPLATE):
       route id   route_2509_template -> names.route_id
       town       kept "Town12"        (the leaderboard reads town from here — the procedure document)
       blueprint  static.prop.template -> names.blueprint_id
     -> common.ROUTE_DATA_DIR/<shift_dir>/<names.route_filename>

  2. The run .sh    (from common.RUN_TEMPLATE):
       <shift_type>        -> shift_dir         (visual_shift | geometry_shift)
       <asset_route_file>  -> names.route_filename
       <asset_name>        -> names.slug        (snake; used in the checkpoint json filename
                                                 to match the worked example)
       CARLA_ROOT          -> $CARLA_PKG   (the install-target rule — replaces the
                                                           template's hardcoded default)
     -> common.RUN_SCRIPT_DIR/<shift_dir>/run_<asset_name.lower()>.sh  (chmod +x)

The town comes from the route XML (config.town); the run script's TOWN var is vestigial
(the procedure document) and is left set to Town12 for clarity.

Emits the common.make_verdict envelope with the two artifact paths to --out.

CLI:
  route_run_gen.py --asset_name DutchCrashAttenuator --shift_type visual \
      --source_dir <src> [--out_root DIR] [--out verdict.json]

`gen(manifest_or_names, shift_type, write=True, out_root=None)` lets the runbook operator
generate to a temp dir (out_root) instead of the committed locations.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import (  # noqa: E402
    SHIFT_DIR,
    Names,
    Manifest,
    names_for,
    validate_asset_name,
    make_verdict,
    write_verdict,
)

# common.ROUTE_TEMPLATE / common.RUN_TEMPLATE / common.ROUTE_DATA_DIR / common.RUN_SCRIPT_DIR / common.CARLA_SERVER_ROOT are
# site-dependent and resolve lazily through common's module __getattr__, so they are read at
# the point of use (below) rather than bound here. That keeps `--help` working on a machine
# with no site config.

STAGE = "route_check"


# --------------------------------------------------------------------------
# Resolve inputs.
# --------------------------------------------------------------------------
# --- site config bootstrap (adds --config; no machine-specific defaults anywhere) ------------
_SC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SC_ROOT not in sys.path:
    sys.path.insert(0, _SC_ROOT)
import site_config as _site_config  # noqa: E402


def _resolve_names(manifest_or_names, source_dir: str = "") -> Names:
    """Accept a Names, a Manifest, or a plain asset_name string."""
    if isinstance(manifest_or_names, Names):
        return manifest_or_names
    if isinstance(manifest_or_names, Manifest):
        return names_for(manifest_or_names.asset_name, manifest_or_names.source_dir)
    if isinstance(manifest_or_names, str):
        return names_for(manifest_or_names, source_dir)
    raise TypeError(
        f"manifest_or_names must be a Names, Manifest, or asset_name str; got {type(manifest_or_names)!r}"
    )


def _shift_dir(shift_type: str) -> str:
    """Map a manifest shift_type ('visual'|'geometric') to its on-disk dir name.

    Tolerates being handed an already-resolved dir name (e.g. 'visual_shift').
    """
    if shift_type in SHIFT_DIR:
        return SHIFT_DIR[shift_type]
    if shift_type in SHIFT_DIR.values():
        return shift_type
    raise ValueError(
        f"shift_type must be one of {sorted(SHIFT_DIR)} (or a resolved dir name "
        f"{sorted(SHIFT_DIR.values())}); got {shift_type!r}"
    )


# --------------------------------------------------------------------------
# Template fills (pure string transforms; deterministic).
# --------------------------------------------------------------------------
def render_route_xml(template_text: str, names: Names) -> str:
    """Set route id and blueprint from the template; keep town=Town12.

    Also strips the template's inline `<!-- change template to asset name-->`
    hints so the generated file matches the committed worked example.
    """
    out = template_text
    out = out.replace('id="route_2509_template"', f'id="{names.route_id}"')
    out = out.replace(
        'value="static.prop.template"', f'value="{names.blueprint_id}"'
    )
    # Drop the two authoring-hint comments left in the template.
    out = re.sub(r"\s*<!--\s*change template to asset name\s*-->", "", out)
    return out


def render_run_sh(template_text: str, names: Names, shift_dir: str) -> str:
    """Fill the run-template placeholders and apply the install-target rule's CARLA_ROOT.

    Placeholders (per ):
      <shift_type>       -> shift_dir
      <asset_route_file> -> names.route_filename
      <asset_name>       -> names.slug   (snake; checkpoint json filename)
    the install-target rule:
      CARLA_ROOT=<template default> -> common.CARLA_SERVER_ROOT ($CARLA_PKG)
    """
    out = template_text
    out = out.replace("<shift_type>", shift_dir)
    out = out.replace("<asset_route_file>", names.route_filename)
    out = out.replace("<asset_name>", names.slug)
    # the install-target rule: point the client PythonAPI at the CARLA build the asset was
    # cooked into, not at whatever the template happened to name.
    out = re.sub(
        r"^CARLA_ROOT=.*$",
        f"CARLA_ROOT={common.CARLA_SERVER_ROOT}",
        out,
        count=1,
        flags=re.MULTILINE,
    )
    # Every remaining absolute path in the template is site-dependent too. Rewrite them from the
    # site config so the emitted script carries no location from whoever authored the template.
    _save = f"{common.SAVE_ROOT}/{shift_dir}"
    _ckpt = f"{_save}/{names.slug}.json"
    out = re.sub(r"^GARAGE_ROOT=.*$", f"GARAGE_ROOT={common.REPO_ROOT}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^CHECKPOINT_ENDPOINT=.*$", f"CHECKPOINT_ENDPOINT={_ckpt}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^SAVE_PATH=.*$", f"SAVE_PATH={_save}", out, count=1, flags=re.MULTILINE)
    out = re.sub(r"^ROUTES=.*$", f"ROUTES={common.ROUTE_DATA_DIR}/{shift_dir}/{names.route_filename}",
                 out, count=1, flags=re.MULTILINE)
    return out


# --------------------------------------------------------------------------
# Output path resolution (committed locations, or under an out_root override).
# --------------------------------------------------------------------------
def _route_out_path(names: Names, shift_dir: str, out_root: str | None) -> str:
    if out_root is None:
        base = common.ROUTE_DATA_DIR
    else:
        # Mirror the committed layout under out_root so callers can diff 1:1.
        base = os.path.join(out_root, "data", "import_check", "static")
    return os.path.join(base, shift_dir, names.route_filename)


def _run_out_path(names: Names, shift_dir: str, out_root: str | None) -> str:
    run_name = f"run_{names.asset_name.lower()}.sh"
    if out_root is None:
        base = common.RUN_SCRIPT_DIR
    else:
        base = os.path.join(out_root, "scripts", "import_check", "static")
    return os.path.join(base, shift_dir, run_name)


# --------------------------------------------------------------------------
# Public entry point.
# --------------------------------------------------------------------------
def gen(manifest_or_names, shift_type, write: bool = True, out_root: str | None = None,
        source_dir: str = "") -> dict:
    """Generate the route XML and run .sh for an asset.

    Returns the verdict-`data` dict (paths + rendered text). When write=True the
    files are materialized; the run .sh is chmod +x. out_root redirects both files
    into a temp tree (mirroring the committed layout) for runbook operator/dry-run use.

    `source_dir` is threaded into name resolution so a bare-string asset_name still
    yields the correct snake slug (used in the checkpoint filename). A Names/Manifest
    input carries its own source_dir and ignores this arg.
    """
    names = _resolve_names(manifest_or_names, source_dir)
    validate_asset_name(names.asset_name)  # the procedure document: CamelCase alphanumerics only
    shift_dir = _shift_dir(shift_type)

    with open(common.ROUTE_TEMPLATE) as f:
        route_tpl = f.read()
    with open(common.RUN_TEMPLATE) as f:
        run_tpl = f.read()

    route_xml = render_route_xml(route_tpl, names)
    run_sh = render_run_sh(run_tpl, names, shift_dir)

    route_path = _route_out_path(names, shift_dir, out_root)
    run_path = _run_out_path(names, shift_dir, out_root)

    if write:
        os.makedirs(os.path.dirname(route_path), exist_ok=True)
        with open(route_path, "w") as f:
            f.write(route_xml)
        os.makedirs(os.path.dirname(run_path), exist_ok=True)
        with open(run_path, "w") as f:
            f.write(run_sh)
        os.chmod(run_path, 0o755)

    return {
        "asset_name": names.asset_name,
        "shift_type": shift_type,
        "shift_dir": shift_dir,
        "route_id": names.route_id,
        "blueprint_id": names.blueprint_id,
        "slug": names.slug,
        "route_path": route_path,
        "run_path": run_path,
        "carla_root": common.CARLA_SERVER_ROOT,
        "written": bool(write),
        # Echo the rendered text so a dry-run (write=False) is still fully inspectable.
        "route_xml": route_xml,
        "run_sh": run_sh,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Generate route XML + run .sh for a static asset.")
    _site_config.add_config_arg(ap)
    ap.add_argument("--asset_name", required=True, help="CamelCase asset name (e.g. DutchCrashAttenuator)")
    ap.add_argument("--shift_type", required=True, help="visual | geometric (or a resolved dir name)")
    ap.add_argument("--source_dir", default="", help="asset source folder; its basename is the slug")
    ap.add_argument("--out_root", default=None, help="write under this temp tree instead of committed locations")
    ap.add_argument("--no_write", action="store_true", help="dry run: render but do not write files")
    ap.add_argument("--out", default=None, help="path to write the JSON verdict envelope")
    args = ap.parse_args(argv)
    _site_config.apply_config_arg(args)

    try:
        names = names_for(args.asset_name, args.source_dir)
        data = gen(names, args.shift_type, write=not args.no_write, out_root=args.out_root)
        verdict = make_verdict(STAGE, True, data=data)
        ok = True
    except Exception as e:  # noqa: BLE001 — surface any failure as a verdict + non-zero exit
        verdict = make_verdict(STAGE, False, error=f"{type(e).__name__}: {e}")
        ok = False

    if args.out:
        write_verdict(args.out, verdict)
    print(json.dumps(verdict, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

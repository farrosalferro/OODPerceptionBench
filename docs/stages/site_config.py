"""
site_config.py — the one place every stage script learns where things are on THIS machine.

There are deliberately **no defaults**. Every path below must be supplied, either in a config
file or through an environment variable. A stage that needs a path you have not configured fails
immediately with a message naming the key and how to set it, rather than silently reading from
somewhere plausible-looking.

Resolution order (first hit wins):

  1. an explicit path passed to ``load(path)`` — what a ``--config`` flag should do
  2. ``$OODPB_SITE_CONFIG`` — path to a YAML or JSON config file
  3. ``site_config.yaml`` beside this file
  4. ``site_config.yaml`` in the current working directory

Individual keys may additionally be overridden by environment variables named
``OODPB_<KEY_UPPERCASED>`` (e.g. ``OODPB_CARLA_SRC``), which take precedence over the file.

Pure stdlib plus optional PyYAML, so it imports cleanly under the several unrelated interpreters
these stages run in (the CARLA client python, the CARLA build python, Blender's bundled python,
and Unreal's embedded python).
"""
from __future__ import annotations

import json
import os

# --------------------------------------------------------------------------------------------
# The configuration surface. Keys are documented in site_config.example.yaml.
#   required=True  -> most stages will need it
#   required=False -> only needed by the optional secondary-ingest stage
# --------------------------------------------------------------------------------------------
KEYS = {
    # toolchain
    "blender_bin":        "Path to the Blender executable.",
    "ue_root":            "Unreal Engine 4.26 (CARLA fork) root directory.",
    "carla_src":          "CARLA 0.9.15 SOURCE build root (contains Makefile, Unreal/CarlaUE4/, Dist/).",
    "carla_pkg":          "The PACKAGED CARLA you evaluate with (contains CarlaUE4.sh, Import/, ImportAssets.sh).",
    "client_python":      "Python interpreter that has the `carla` client package installed.",
    "build_python_env":   "Name of the Python 3.8 environment used by CARLA's `make` targets.",
    "conda_root":         "Root of the conda/mamba installation providing build_python_env.",
    # benchmark + working dirs
    "bench_root":         "Your clone of the OOD-PerceptionBench repository.",
    "work_root":          "Scratch directory for per-asset runs, renders and state files.",
    "results_root":       "Where validation-route output is written.",
    "mesh_src":           "Directory holding source meshes and the exported anchor FBX/TGA files.",
    "anchor_fbx":         "The static anchor FBX exported from Carla/Static/Dynamic/Construction/SM_TrafficCones_4.",
    # optional: a second CARLA to install into (e.g. a cluster), reached over ssh
    "secondary_carla_pkg":        "OPTIONAL. A second packaged CARLA to also install into.",
    "secondary_ssh_host":         "OPTIONAL. user@host that can reach secondary_carla_pkg natively.",
    "secondary_submit_partitions": "OPTIONAL. Scheduler partitions to use when installing remotely.",
}

OPTIONAL = {"secondary_carla_pkg", "secondary_ssh_host", "secondary_submit_partitions"}

_ENV_PREFIX = "OODPB_"
_cache: dict | None = None
_explicit_path: str | None = None


def _parse_flat_yaml(text: str) -> dict:
    """Minimal `key: value` parser, used when PyYAML is unavailable.

    The config is deliberately a FLAT string->string mapping, so this covers it completely.
    These stages run under several interpreters we do not control the packages of (Unreal's
    embedded python, Blender's bundled python), and requiring PyYAML in all of them would be
    a needless obstacle. Anything this parser cannot handle is a config the schema forbids.
    """
    out = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line[:1].isspace():
            raise ValueError(
                "line %d: indentation is not supported — the site config is a flat "
                "key: value mapping (install PyYAML if you need more)" % lineno
            )
        if ":" not in line:
            raise ValueError("line %d: expected 'key: value', got %r" % (lineno, raw))
        k, v = line.split(":", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def _read_file(path: str) -> dict:
    with open(path) as f:
        text = f.read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
            data = yaml.safe_load(text)
        except ImportError:
            data = _parse_flat_yaml(text)
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    unknown = set(data) - set(KEYS)
    if unknown:
        raise ValueError(
            f"{path}: unknown config keys {sorted(unknown)}. Known keys: {sorted(KEYS)}"
        )
    return data


def _candidate_paths() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    if _explicit_path:
        out.append(_explicit_path)
    if os.environ.get(_ENV_PREFIX + "SITE_CONFIG"):
        out.append(os.environ[_ENV_PREFIX + "SITE_CONFIG"])
    out.append(os.path.join(here, "site_config.yaml"))
    out.append(os.path.join(os.getcwd(), "site_config.yaml"))
    return out


def load(path: str | None = None) -> dict:
    """Load (and cache) the site config. Pass an explicit path to override discovery."""
    global _cache, _explicit_path
    if path is not None:
        _explicit_path = os.path.abspath(path)
        _cache = None
    if _cache is not None:
        return _cache
    data: dict = {}
    for p in _candidate_paths():
        if p and os.path.isfile(p):
            data = _read_file(p)
            data["_source"] = p
            break
    else:
        data["_source"] = "(no config file found; environment variables only)"
    _cache = data
    return _cache


def add_config_arg(parser) -> None:
    """Register the standard ``--config`` flag on an argparse parser."""
    parser.add_argument(
        "--config", default=None,
        help="path to the site config (YAML/JSON). Overrides $OODPB_SITE_CONFIG.",
    )


def apply_config_arg(args) -> None:
    """Call right after parse_args() when the parser used add_config_arg()."""
    if getattr(args, "config", None):
        load(args.config)


def get(key: str) -> str:
    """Resolve one config key. Raises with an actionable message if it is unset."""
    if key not in KEYS:
        raise KeyError(f"unknown site config key {key!r}; known keys: {sorted(KEYS)}")
    env = os.environ.get(_ENV_PREFIX + key.upper())
    if env:
        return env
    cfg = load()
    val = cfg.get(key)
    if val:
        return str(val)
    raise RuntimeError(
        "Missing site configuration key '{k}'.\n"
        "  What it is : {d}\n"
        "  Config read: {src}\n"
        "  Fix by either\n"
        "    - adding '{k}: <path>' to your site config, or\n"
        "    - exporting {p}{K}=<path>, or\n"
        "    - passing --config <file> to this stage.\n"
        "  Start from site_config.example.yaml. There are no built-in defaults on purpose: a\n"
        "  wrong-but-plausible default is how an asset silently ends up in the wrong CARLA."
        .format(k=key, K=key.upper(), p=_ENV_PREFIX, d=KEYS[key], src=cfg.get("_source"))
    )


def get_optional(key: str, default=None):
    """Like get(), but returns `default` instead of raising when unset."""
    try:
        return get(key)
    except RuntimeError:
        return default


def describe() -> str:
    """Human-readable dump of what is currently resolvable — useful in a preflight check."""
    cfg = load()
    lines = ["site config source: %s" % cfg.get("_source")]
    for k in sorted(KEYS):
        try:
            lines.append("  %-28s = %s" % (k, get(k)))
        except RuntimeError:
            lines.append("  %-28s = <unset>%s" % (k, " (optional)" if k in OPTIONAL else ""))
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Show the resolved site configuration.")
    add_config_arg(ap)
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if any required key is unset")
    a = ap.parse_args()
    apply_config_arg(a)
    print(describe())
    if a.check:
        missing = [k for k in KEYS if k not in OPTIONAL and get_optional(k) is None]
        if missing:
            raise SystemExit("missing required keys: %s" % ", ".join(sorted(missing)))

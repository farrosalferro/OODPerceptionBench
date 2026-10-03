"""Environment preflight: check the route interpreter before anything is launched.

The evaluator imports the agent only after it has started CARLA and waited out the simulator's
start-up, so an interpreter that cannot ``import py_trees`` -- or a bare ``python3`` that the
job's activation resolves to some other environment -- used to cost one simulator start-up per
route to discover, and then surfaced as an infrastructure failure on every route in turn.

This runs ``environment.python`` once, on the host the runner runs on, under the same
activation, ``agent.env``, ``PYTHONPATH`` and working directory a route gets (built by
:func:`oodbench.jobscript.environment_prelude`, never copied), and imports what the evaluator
imports: ``carla``, ``py_trees``, ``numpy``, ``scipy`` and the agent module, the evaluator's
way. Any failure aborts the sweep before the first route. On success the interpreter, the
versions it found, the ``agent.env`` the routes get and a fingerprint of the agent's code
(:func:`agent_fingerprint`) are written to ``<output.root>/_runner/env_provenance.json``.

Deliberately NOT checked: the GPU, CUDA, or anything the agent does in ``setup()``. Those need
the simulator and the route; this is only the part that can be known for free.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

from .jobscript import environment_prelude

if TYPE_CHECKING:  # pragma: no cover
    import logging

    from .config import Config

#: 2 added ``agent_code`` (:func:`agent_fingerprint`); the golden builder requires it.
SCHEMA = 2

#: (distribution name for ``importlib.metadata``, module name to import), in report order.
PACKAGES = (("numpy", "numpy"), ("scipy", "scipy"), ("carla", "carla"), ("py_trees", "py_trees"))

#: Generous: importing a model's agent module can pull in torch and its extensions.
TIMEOUT_S = 600

MARKER = "OODPB_ENV_PREFLIGHT_RESULT "

#: Each git call of :func:`agent_fingerprint`. git on a local checkout answers in milliseconds;
#: this only bounds a hung network filesystem.
GIT_TIMEOUT_S = 60

#: How many ``git status --porcelain`` lines :func:`agent_fingerprint` keeps as evidence.
DIRTY_LINES = 20

_ABS_PATH = re.compile(r"(?<![\w.])/[^\s'\"]+")

#: Runs inside ``environment.python``. Must stay importable by any Python 3 the runner might
#: be pointed at, so: standard library only, and no f-string features newer than 3.6.
_PROBE = r'''
import importlib, json, os, platform, sys, traceback
try:
    from importlib import metadata as _md
except ImportError:  # Python < 3.8
    _md = None

packages, failures = {}, []

def _owned_version(dist, mod):
    # The installed distribution's version, but only if the module that was imported is one of
    # its files: a copy found earlier on the path (a CARLA egg on PYTHONPATH in front of a
    # pip-installed wheel) would otherwise be reported under the wheel's version.
    target = getattr(mod, "__file__", None)
    if _md is None or not target:
        return None
    target = os.path.realpath(target)
    try:
        d = _md.distribution(dist)
        for f in d.files or ():
            if (os.path.basename(str(f)) == os.path.basename(target)
                    and os.path.realpath(str(d.locate_file(f))) == target):
                return d.version
    except Exception:
        pass
    return None

def _version(dist, mod):
    v = _owned_version(dist, mod)
    if v is None:
        v = getattr(mod, "__version__", None)
    return None if v is None else str(v)

for dist, name in json.loads(sys.argv[1]):
    try:
        packages[dist] = _version(dist, importlib.import_module(name))
    except BaseException:
        packages[dist] = None
        failures.append({"what": name, "error": traceback.format_exc(limit=3)})

# The evaluator's own import: its directory is sys.path[0] (it is run as a script), the
# agent's directory is put in front of that, and the module is the file's basename.
agent = sys.argv[2]
sys.path[0] = sys.argv[3]
sys.path.insert(0, os.path.dirname(agent))
agent_ok = True
try:
    importlib.import_module(os.path.basename(agent).split(".")[0])
except BaseException:
    agent_ok = False
    failures.append({"what": "agent " + agent, "error": traceback.format_exc(limit=5)})

print(%(marker)r + json.dumps({
    "python_executable": sys.executable,
    "python_version": platform.python_version(),
    "packages": packages,
    "agent_import_ok": agent_ok,
    "failures": failures,
}), flush=True)
''' % {"marker": MARKER}


class EnvCheckError(RuntimeError):
    """The route interpreter cannot import what the evaluator needs."""


_HINT = ("Fix `environment.python` (give it as an ABSOLUTE path to the interpreter that has "
         "the evaluator's dependencies and your agent's), `environment.activate` or "
         "`agent.pythonpath`, then re-run. --skip-env-preflight bypasses this check; every route "
         "would then fail the same way, after a simulator start-up each.")


def render(cfg: "Config") -> str:
    """The bash source of the preflight: the route's environment, then the probe."""
    argv = [cfg.environment["python"], "-c", _PROBE,
            json.dumps([list(p) for p in PACKAGES]), str(cfg.agent["entrypoint"]),
            str(cfg.evaluator_path.parent)]
    lines = ["#!/bin/bash",
             "# Generated by the OOD-PerceptionBench runner: environment preflight.",
             "# The same activation, agent.env, paths and working directory as a route attempt.",
             "set -o pipefail", ""]
    lines.extend(environment_prelude(cfg))
    lines.append("")
    lines.append(" ".join(shlex.quote(a) for a in argv))
    return "\n".join(lines) + "\n"


def _git(cwd: str, *args: str) -> str:
    """stdout of ``git -C cwd args``. Raises :class:`RuntimeError` with git's own complaint."""
    # A GIT_DIR or GIT_WORK_TREE left in the runner's environment (a git hook, an IDE) would
    # point git at some other repository than the one the agent file is in.
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")}
    try:
        p = subprocess.run(["git", "-C", cwd, *args], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env,
                           timeout=GIT_TIMEOUT_S, universal_newlines=True)
    except FileNotFoundError:
        raise RuntimeError("git is not installed") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} did not answer within {GIT_TIMEOUT_S} s") from None
    if p.returncode != 0:
        # First line only: enough to tell "not a git repository" from "dubious ownership".
        # Paths in it are masked -- "dubious ownership in repository at '/x/y'" names a
        # directory, and the provenance file must not carry one.
        lines = (p.stderr or p.stdout).strip().splitlines()
        first = _ABS_PATH.sub("<path>", lines[0]) if lines else f"exit {p.returncode}"
        raise RuntimeError(f"git {args[0]} failed: {first}")
    return p.stdout


def agent_fingerprint(entrypoint: str) -> Dict[str, Any]:
    """What the agent's code was when the preflight ran, in terms a golden builder can check.

    The agent file's own sha256 is not enough: the reference agent's entrypoint imports the
    driving code from files next to it, and an edit there leaves the entrypoint's hash alone.
    So this also records the commit of the git checkout the file is in and whether the
    checkout is clean under the entrypoint's top directory (the "scope"): no modified, staged
    or untracked file there, ignored files excepted. That is exactly the rule
    ``tests/make_golden.py`` (``reference_agent_provenance``) applies to the reference
    checkout, so a replicate's fingerprint can be compared with it field by field.

    Taken once, here: a file edited after the preflight, mid-sweep, is not caught.

    Never raises for git trouble -- an ordinary benchmark run does not need this, only golden
    building does. If git cannot answer (not installed, not a repository, "dubious
    ownership"), ``git_head``, ``entrypoint_rel``, ``scope``, ``scope_clean`` and
    ``scope_dirty`` are null and ``git_error`` says why. No absolute path is recorded.
    """
    out: Dict[str, Any] = {"entrypoint_sha256": None, "entrypoint_rel": None, "git_head": None,
                           "scope": None, "scope_clean": None, "scope_dirty": None,
                           "git_error": None}
    with open(entrypoint, "rb") as fh:
        out["entrypoint_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    folder = os.path.dirname(os.path.abspath(entrypoint))
    try:
        top = _git(folder, "rev-parse", "--show-toplevel").strip()
        # The file's directory relative to the top, as git resolved it: a symlinked directory
        # on the way would make a path computed here disagree with git's.
        prefix = _git(folder, "rev-parse", "--show-prefix").strip()
        head = _git(folder, "rev-parse", "HEAD").strip()
        rel = posixpath.join(prefix, os.path.basename(entrypoint)) if prefix \
            else os.path.basename(entrypoint)
        # Same rule as agent_scope() in tests/make_golden.py, which refuses any other.
        scope = rel.split("/")[0] if "/" in rel else "."
        dirty = _git(top, "status", "--porcelain", "--untracked-files=all", "--", scope)
    except RuntimeError as exc:
        out["git_error"] = str(exc)
        return out
    lines: List[str] = [line for line in dirty.splitlines() if line.strip()]
    out.update(entrypoint_rel=rel, git_head=head, scope=scope, scope_clean=not lines,
               scope_dirty=lines[:DIRTY_LINES])
    return out


def run(cfg: "Config", log: "logging.Logger") -> Dict[str, Any]:
    """Run the preflight, log what it found, write the provenance file and return it.

    Raises :class:`EnvCheckError` on any failure, with the evidence in the message.
    """
    runner_dir = Path(cfg.output["root"]) / "_runner"
    runner_dir.mkdir(parents=True, exist_ok=True)
    script = runner_dir / "env_preflight.sh"
    script.write_text(render(cfg), encoding="utf-8")

    python = cfg.environment["python"]
    log.info("environment preflight: importing carla, py_trees, numpy, scipy and the agent "
             "with %s", python)
    try:
        proc = subprocess.run(["bash", str(script)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              timeout=TIMEOUT_S, universal_newlines=True)
    except subprocess.TimeoutExpired:
        raise EnvCheckError(f"environment preflight did not finish within {TIMEOUT_S} s "
                            f"(script: {script}). {_HINT}") from None
    output = proc.stdout or ""
    result = None
    for line in output.splitlines():
        if line.startswith(MARKER):
            result = json.loads(line[len(MARKER):])
    tail = "\n".join(output.splitlines()[-30:])

    if result is None:
        raise EnvCheckError(
            f"environment preflight could not run {python!r} (exit {proc.returncode}; script: "
            f"{script}). Last output:\n{tail}\n{_HINT}")

    log.info("environment preflight: python %s (%s)", result["python_executable"],
             result["python_version"])
    for dist, _ in PACKAGES:
        log.info("environment preflight:   %-9s %s", dist, result["packages"].get(dist))
    if not os.path.isabs(python):
        log.warning("environment.python is %r, not an absolute path; it resolved to %s here. "
                    "Give the absolute path so every host and every route resolves the same "
                    "interpreter.", python, result["python_executable"])

    if result["failures"]:
        detail = "\n".join(f"--- {f['what']}:\n{f['error'].rstrip()}" for f in result["failures"])
        names = ", ".join(f["what"] for f in result["failures"])
        raise EnvCheckError(
            f"environment preflight: {result['python_executable']} cannot import {names}.\n"
            f"{detail}\n{_HINT}")
    if proc.returncode != 0:
        raise EnvCheckError(f"environment preflight exited {proc.returncode} (script: {script})."
                            f" Last output:\n{tail}\n{_HINT}")

    agent_code = agent_fingerprint(str(cfg.agent["entrypoint"]))
    if agent_code["git_error"] is not None:
        log.warning("environment preflight: could not fingerprint the agent's git checkout "
                    "(%s). The run is unaffected, but its output cannot be used to build a "
                    "golden bundle.", agent_code["git_error"])
    elif agent_code["scope_clean"]:
        log.info("environment preflight: agent %s at commit %s, clean under %s",
                 agent_code["entrypoint_rel"], agent_code["git_head"], agent_code["scope"])
    else:
        log.warning("environment preflight: agent %s at commit %s, but the checkout has "
                    "modified or untracked files under %s (listed in env_provenance.json). "
                    "The run is unaffected, but its output cannot be used to build a golden "
                    "bundle.", agent_code["entrypoint_rel"], agent_code["git_head"],
                    agent_code["scope"])

    provenance = {
        "schema": SCHEMA,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "python_executable": result["python_executable"],
        "python_version": result["python_version"],
        "packages": {dist: result["packages"].get(dist) for dist, _ in PACKAGES},
        "agent_entrypoint": str(cfg.agent["entrypoint"]),
        "agent_import_ok": bool(result["agent_import_ok"]),
        # As every route receives it (the runner's reserved variables are added per route and
        # are not part of it). The golden builder checks DATAGEN here.
        "agent_env": dict(cfg.agent["env"]),
        # Which code the agent was, at this moment: see agent_fingerprint(). The golden
        # builder requires it to match the reference checkout.
        "agent_code": agent_code,
    }
    path = runner_dir / "env_provenance.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    log.info("environment preflight: OK, wrote %s", path)
    return provenance

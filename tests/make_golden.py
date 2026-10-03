#!/usr/bin/env python3
"""Build a golden bundle for the smoke split from N replicate runs.

Bundle version : v0.9
Binds to       : arXiv v1

A golden is not "the numbers I got once". Closed-loop CARLA is not bit-reproducible -- frame
timing, sensor delivery and physics all vary a little between runs and a lot between machines.
A golden whose tolerance was guessed is either so tight that every honest install fails it, or
so loose that it never catches anything.

So this tool refuses to invent one. It takes >= 2 independent replicate runs of the same split
with the same agent on the same build, MEASURES the run-to-run spread of the Driving Score, and
derives the tolerance from it. The replicate values are written into the bundle so a reader can
audit the tolerance rather than trust it.

It also refuses to write a bundle if assertions A1..A3 do not hold in *every* replicate.
Goldens minted on a broken install are worse than no goldens at all: they bake the breakage in
and make the acceptance test certify it forever.

Standard library only.

Usage
-----
    python3 make_golden.py \\
        --replicate /runs/smoke_rep1 --replicate /runs/smoke_rep2 --replicate /runs/smoke_rep3 \\
        --reference-agent pdmlite \\
        --reference-agent-repo /src/carla_garage \\
        --reference-agent-commit beb3433407f42c1adced312b877a61fe04f338ba \\
        --reference-agent-entrypoint team_code/data_agent.py \\
        --carla-version 0.9.15 \\
        --content-pack-version v0.9 \\
        $(awk '{printf " --content-pack-archive %s=%s", $2, $1}' ../assets/SHA256SUMS) \\
        --out goldens/pdmlite_seed42_v0.9.golden.json

Provenance it refuses to guess
------------------------------
* The reference agent must be fetchable by a stranger: a clean git checkout (no modified or
  untracked file under the entrypoint's top directory) at the stated commit. The bundle records
  repo URL, full commit, entrypoint and the entrypoint's sha256.
* The Python that RAN the routes is read from <replicate>/_runner/env_provenance.json, which the
  runner's preflight writes. All replicates must agree. The builder's own interpreter is never
  stamped -- it ran no route.
* So are the agent.env the routes ran with (all replicates must agree, and DATAGEN must be "0":
  the carla_garage data agent shuffles the weather when it is 1) and, from
  <replicate>/_runner/report.json, the platform of the host the replicates ran on. The
  builder's own platform is never stamped either.
* The content pack is several archives. The bundle lists each archive's sha256 and a composite:
  sha256 of the canonical SHA256SUMS text (one "<sha256>  <name>\\n" line per archive, sorted by
  name in byte order). For a canonical sums file that is exactly `sha256sum SHA256SUMS`.

Exit status
-----------
    0  bundle written
    1  an assertion failed in some replicate, or the runs disagree (results, interpreter,
       packages, platform, agent.env or agent entrypoint), or a replicate ran without
       DATAGEN "0" -- nothing written
    2  usage / IO error, or provenance that cannot be established -- nothing written
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import statistics
import subprocess
import sys
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "smoke"))

from materialize import (  # noqa: E402
    BINDS_TO,
    BUNDLE_VERSION,
    DEFAULT_ROUTES_ROOT,
    DEFAULT_SPLIT,
    SplitError,
    blueprint_in_xml,
    load_split,
    sha256_of,
    verify as verify_split,
)

sys.path.insert(0, HERE)
from check_acceptance import (  # noqa: E402
    CLEAN_STATUSES,
    GOLDEN_SCHEMA_ID,
    driving_score,
    locate_result,
    read_checkpoint,
)

DEFAULT_REFERENCE_TSV = os.path.join(HERE, "reference", "pdmlite_seed42_reference.tsv")


def load_reference(path: str) -> dict:
    """Published seed-42 observations, for an informational comparison. Never gating."""
    out: dict = {}
    if not os.path.isfile(path):
        return out
    header = None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            f = line.split("\t")
            if header is None:
                header = f
                continue
            out[f[0]] = dict(zip(header, f))
    return out


class ProvenanceError(Exception):
    """Provenance that cannot be established. Carries the exit code to return."""

    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PY_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
ENV_PROVENANCE = os.path.join("_runner", "env_provenance.json")
RUN_REPORT = os.path.join("_runner", "report.json")
COMPOSITE_RULE_NOTE = (
    "environment.content_pack_sha256 = sha256 of the canonical SHA256SUMS text of "
    "environment.content_pack_archives ('<sha256>  <name>\\n' per archive, sorted by name).")
# Packages whose versions go into the bundle. The runner records more (py_trees); those are
# compared across replicates but not stamped.
STAMPED_PACKAGES = ("numpy", "scipy", "carla")


def _git(repo: str, *args: str) -> str:
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise ProvenanceError(f"git -C {repo} {' '.join(args)} failed: "
                              f"{(p.stderr or p.stdout).strip()}")
    return p.stdout


def _public_url(url: str) -> Optional[str]:
    """An https URL a stranger can fetch, or None. Local paths and file:// never qualify."""
    url = url.strip()
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+)$", url)
    if m:
        url = f"https://{m.group(1)}/{m.group(2)}"
    if not re.match(r"^https?://[^/]+/.+", url):
        return None
    return url[:-4] if url.endswith(".git") else url


def reference_agent_provenance(args) -> dict:
    """Prove the reference agent is retrievable, then describe it. Raises ProvenanceError."""
    repo = os.path.abspath(args.reference_agent_repo)
    if not os.path.isdir(repo):
        raise ProvenanceError(f"--reference-agent-repo is not a directory: {repo}")
    rel = posixpath.normpath(args.reference_agent_entrypoint.replace(os.sep, "/"))
    if posixpath.isabs(rel) or rel == "." or rel.split("/")[0] == "..":
        raise ProvenanceError("--reference-agent-entrypoint must be relative to the agent repo, "
                              "e.g. team_code/data_agent.py")

    head = _git(repo, "rev-parse", "HEAD").strip()
    try:
        want = _git(repo, "rev-parse", "--verify", "--quiet",
                    f"{args.reference_agent_commit}^{{commit}}").strip()
    except ProvenanceError:
        want = None
    if want != head:
        raise ProvenanceError(
            f"the agent checkout is at {head}, not at --reference-agent-commit "
            f"{args.reference_agent_commit}. The bundle would name a commit that did not run.")

    try:
        _git(repo, "ls-files", "--error-unmatch", "--", rel)
    except ProvenanceError:
        raise ProvenanceError(f"{rel} is not tracked in {repo} at {head}. An untracked "
                              f"entrypoint cannot be fetched by anyone else.")

    # The v0.9 golden ran untracked *_debug.py copies sitting next to the real agent. A commit
    # sha says nothing about those; `git status` does. Ignored files (__pycache__) do not count.
    scope = rel.split("/")[0] if "/" in rel else "."
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=all", "--", scope).strip()
    if dirty:
        raise ProvenanceError(
            f"the agent tree is not clean (git status --porcelain -- {scope}):\n"
            + "\n".join(f"    {line}" for line in dirty.splitlines()[:20])
            + "\n  A modified or untracked file there may be what actually ran, and nobody else "
              "can fetch it. Commit it upstream, or remove it and re-run the replicates.")

    if args.reference_agent_url:
        url = _public_url(args.reference_agent_url)
        if url is None:
            raise ProvenanceError(f"--reference-agent-url must be an https URL, "
                                  f"got {args.reference_agent_url!r}")
    else:
        try:
            origin = _git(repo, "remote", "get-url", "origin").strip()
        except ProvenanceError:
            origin = ""
        url = _public_url(origin)
        if url is None:
            raise ProvenanceError(
                "cannot derive a public URL from the agent checkout's `origin` remote. Pass "
                "--reference-agent-url https://github.com/<owner>/<repo>. A local path is not "
                "provenance.")

    with open(os.path.join(repo, rel), "rb") as fh:
        entry_sha = hashlib.sha256(fh.read()).hexdigest()
    name = url.rstrip("/").rsplit("/", 1)[-1]
    version = f"{name}@{head} {rel}"
    if args.reference_agent_version and args.reference_agent_version != version:
        raise ProvenanceError(f"--reference-agent-version {args.reference_agent_version!r} "
                              f"disagrees with the checkout, which is {version!r}")
    return {"name": args.reference_agent, "version": version, "repo": url, "commit": head,
            "entrypoint": rel, "entrypoint_sha256": entry_sha}


def replicate_environment(reps: list, rep_label: dict, entrypoint: str,
                          explicit_python: Optional[str]) -> tuple:
    """(python, packages-or-None, agent_env-or-None) that RAN the replicates.

    Reads <replicate>/_runner/env_provenance.json from every replicate. Paths in that file
    (the interpreter, the agent) are checked, never returned. Raises ProvenanceError.
    """
    if explicit_python is not None and not _PY_VERSION.match(explicit_python):
        raise ProvenanceError(f"--replicate-python must look like X.Y.Z, got {explicit_python!r}")
    seen: dict = {}
    missing: list = []
    agent_envs: dict = {}
    for d in reps:
        path = os.path.join(d, ENV_PROVENANCE)
        if not os.path.isfile(path):
            missing.append(d)
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ProvenanceError(f"{rep_label[d]}: {ENV_PROVENANCE} does not parse: {exc}")
        if doc.get("schema") != 1 or not isinstance(doc.get("packages"), dict) \
                or not isinstance(doc.get("python_version"), str):
            raise ProvenanceError(f"{rep_label[d]}: {ENV_PROVENANCE} is not schema 1")
        if doc.get("agent_import_ok") is not True:
            raise ProvenanceError(f"{rep_label[d]}: the runner preflight could not import the "
                                  f"agent; its routes did not run the agent you think", code=1)
        ran = str(doc.get("agent_entrypoint") or "").replace(os.sep, "/")
        if ran != entrypoint and not ran.endswith("/" + entrypoint):
            raise ProvenanceError(
                f"{rep_label[d]}: the replicate ran agent entrypoint "
                f"{os.path.basename(ran)!r}, which is not {entrypoint}", code=1)
        seen[d] = (doc["python_version"], doc["packages"])
        env = doc.get("agent_env")  # absent from files written before it was recorded
        if env is None:
            continue
        if not isinstance(env, dict):
            raise ProvenanceError(f"{rep_label[d]}: {ENV_PROVENANCE} agent_env is not a mapping")
        datagen = env.get("DATAGEN")
        if datagen != "0":
            how = (f"has DATAGEN={datagen!r}" if datagen is not None else
                   "has no DATAGEN, so whatever the runner's own environment held reached the "
                   "agent")
            raise ProvenanceError(
                f"{rep_label[d]}: agent.env {how}. The carla_garage data agent shuffles the "
                f"weather and attaches data-collection sensors when DATAGEN=1, so the protocol "
                f"sets DATAGEN: \"0\" explicitly. Fix agent.env and re-run the replicates.",
                code=1)
        agent_envs[d] = env

    for d, (py, _) in seen.items():
        if explicit_python is not None and py != explicit_python:
            raise ProvenanceError(f"{rep_label[d]} ran Python {py}, but --replicate-python says "
                                  f"{explicit_python}", code=1)
    pythons = {py for py, _ in seen.values()}
    if len(pythons) > 1:
        detail = ", ".join(f"{rep_label[d]}={py}" for d, (py, _) in seen.items())
        raise ProvenanceError(f"the replicates ran different Pythons ({detail}). They are not "
                              f"replicates of one environment.", code=1)
    pkgs = [json.dumps(p, sort_keys=True) for _, p in seen.values()]
    if len(set(pkgs)) > 1:
        detail = "; ".join(f"{rep_label[d]}={p}" for d, (_, p) in seen.items())
        raise ProvenanceError(f"the replicates ran different package versions ({detail})",
                              code=1)
    if len({json.dumps(e, sort_keys=True) for e in agent_envs.values()}) > 1:
        detail = "; ".join(f"{rep_label[d]}={json.dumps(e, sort_keys=True)}"
                           for d, e in agent_envs.items())
        raise ProvenanceError(f"the replicates ran with different agent.env ({detail})", code=1)
    if missing:
        names = ", ".join(rep_label[d] for d in missing)
        if explicit_python is None:
            raise ProvenanceError(
                f"no {ENV_PROVENANCE} in {names}. Pass --replicate-python X.Y.Z with the "
                f"version of the interpreter the runner executed (environment.python in the "
                f"config), NOT the one running this script.")
        print(f"WARNING: no {ENV_PROVENANCE} in {names}; Python taken from --replicate-python "
              f"and package versions are not stamped.", file=sys.stderr)
    python = explicit_python if explicit_python is not None else pythons.pop()
    packages = None
    if not missing:
        only = next(iter(seen.values()))[1]
        packages = {k: only.get(k) for k in STAMPED_PACKAGES
                    if k != "carla" or only.get(k) is not None}
    agent_env = None
    unrecorded = [d for d in reps if d not in agent_envs]
    if unrecorded:
        names = ", ".join(rep_label[d] for d in unrecorded)
        print(f"WARNING: no agent.env recorded for {names} (made by a runner that did not "
              f"record it); reference_agent.env is not stamped. Check by hand that every "
              f"replicate ran with DATAGEN \"0\".", file=sys.stderr)
    else:
        agent_env = dict(sorted(next(iter(agent_envs.values())).items()))
    return python, packages, agent_env


def replicate_platform(reps: list, rep_label: dict) -> Optional[str]:
    """Platform string of the host the replicates RAN on, or None. Raises ProvenanceError.

    Read from <replicate>/_runner/report.json, where the runner records platform.platform() of
    the host it ran on -- on the local backend the golden procedure uses, the host the routes
    ran on. The replicates of one golden are runs on one machine, so they must agree. Never the
    builder's own platform: the builder ran no route.
    """
    seen: dict = {}
    unrecorded: list = []
    for d in reps:
        try:
            with open(os.path.join(d, RUN_REPORT), encoding="utf-8") as fh:
                value = (json.load(fh).get("run") or {}).get("platform")
        except (OSError, ValueError, AttributeError):
            value = None
        if isinstance(value, str) and value:
            seen[d] = value
        else:
            unrecorded.append(d)
    if len(set(seen.values())) > 1:
        detail = ", ".join(f"{rep_label[d]}={p}" for d, p in seen.items())
        raise ProvenanceError(f"the replicates ran on different platforms ({detail}). The "
                              f"replicates of one golden are runs on one machine.", code=1)
    if unrecorded:
        names = ", ".join(rep_label[d] for d in unrecorded)
        print(f"WARNING: no platform in {RUN_REPORT} of {names}; environment.os is not "
              f"stamped.", file=sys.stderr)
        return None
    return next(iter(seen.values()))


def content_pack_digest(specs: list, cross_check: Optional[str]) -> tuple:
    """({name: sha256} sorted by name, composite). Raises ProvenanceError.

    composite = sha256 of the canonical SHA256SUMS text: one "<sha256>  <name>\\n" line per
    archive, sorted by name in byte order. Order of the command-line flags is irrelevant.
    """
    archives: dict = {}
    for spec in specs:
        name, sep, sha = spec.rpartition("=")
        name, sha = name.strip(), sha.strip().lower()
        if not sep or not name or not _SHA256.match(sha) or re.search(r"\s|/", name):
            raise ProvenanceError(f"--content-pack-archive wants NAME=SHA256 (a bare archive "
                                  f"file name and 64 hex digits), got {spec!r}")
        if name in archives and archives[name] != sha:
            raise ProvenanceError(f"--content-pack-archive {name} given twice with different "
                                  f"digests")
        archives[name] = sha
    ordered = {n: archives[n] for n in sorted(archives, key=lambda n: n.encode("utf-8"))}
    text = "".join(f"{sha}  {name}\n" for name, sha in ordered.items())
    composite = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if cross_check is not None and cross_check.strip().lower() != composite:
        raise ProvenanceError(f"--content-pack-sha256 {cross_check} does not equal the composite "
                              f"{composite} of the archives given")
    return ordered, composite


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--replicate", action="append", required=True, metavar="DIR",
                    help="output root of one smoke-split run (repeatable; >= 2 required)")
    ap.add_argument("--split", default=DEFAULT_SPLIT)
    ap.add_argument("--routes-root", default=DEFAULT_ROUTES_ROOT)
    ap.add_argument("--tier", choices=("core", "all"), default="all")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", required=True, help="golden bundle to write (*.golden.json)")

    ap.add_argument("--reference-agent", required=True,
                    help="name of the reference agent, e.g. pdmlite")
    ap.add_argument("--reference-agent-repo", required=True, metavar="DIR",
                    help="local git checkout the replicates ran the agent from. Must be clean "
                         "under the entrypoint's top directory, at --reference-agent-commit")
    ap.add_argument("--reference-agent-commit", required=True, metavar="SHA",
                    help="commit of that checkout; stamped as the full sha")
    ap.add_argument("--reference-agent-entrypoint", required=True, metavar="PATH",
                    help="agent file relative to the repo, e.g. team_code/data_agent.py")
    ap.add_argument("--reference-agent-url", default=None, metavar="URL",
                    help="public https URL of the agent repo. Default: the checkout's origin "
                         "remote, if that is a public URL")
    ap.add_argument("--reference-agent-version", default=None,
                    help="optional cross-check. The version is derived as "
                         "'<repo>@<full sha> <entrypoint>'; a different value here is an error")
    ap.add_argument("--carla-version", required=True)
    ap.add_argument("--content-pack-version", required=True,
                    help="version of the OOD content pack these goldens are valid for")
    ap.add_argument("--content-pack-archive", action="append", required=True,
                    metavar="NAME=SHA256",
                    help="one content-pack archive and its sha256 (repeatable; take them from "
                         "assets/SHA256SUMS). The bundle stamps each one plus their composite")
    ap.add_argument("--content-pack-sha256", default=None,
                    help="optional cross-check of the composite digest; a mismatch is an error")
    ap.add_argument("--replicate-python", default=None, metavar="X.Y.Z",
                    help="Python version the replicates ran under. Required only when a "
                         "replicate has no _runner/env_provenance.json; otherwise a cross-check")
    ap.add_argument("--runner-version", default=None)
    ap.add_argument("--gpu", default=None, help="e.g. 'NVIDIA RTX A6000, driver 550.54.14'")
    ap.add_argument("--notes", default=None)

    ap.add_argument("--min-tolerance", type=float, default=1.0,
                    help="floor on the DS tolerance in DS points (default 1.0). The floor "
                         "exists because replicates on one machine can agree exactly while a "
                         "different machine still differs slightly.")
    ap.add_argument("--tolerance-factor", type=float, default=2.0,
                    help="tolerance = max(min_tolerance, factor * largest observed spread)")
    ap.add_argument("--allow-single-replicate", action="store_true",
                    help="NOT RECOMMENDED. Write a bundle from one run; the tolerance is then "
                         "the unmeasured floor and the bundle records that fact.")
    args = ap.parse_args()

    print(f"OOD-PerceptionBench golden builder  [{BUNDLE_VERSION}, binds to {BINDS_TO}]")

    reps = list(dict.fromkeys(args.replicate))
    # Replicate roots are scratch directories on the machine that generated the bundle. Their
    # ABSOLUTE PATHS were written into every route record until 2026-08-12, which put 27 private
    # paths into a shipped artifact and was caught by the repository's own pre-push guard --
    # after the bundle had already passed every acceptance check, because nothing downstream
    # reads this field. It is provenance, and provenance in a published file has to be portable.
    #
    # The index is what actually carries meaning to a reader ("these are three independent
    # runs"); the local path carries none. The operator's own log maps index back to directory.
    rep_label = {d: f"replicate_{i}" for i, d in enumerate(reps, 1)}
    if len(reps) < 2 and not args.allow_single_replicate:
        print(f"\nERROR: {len(reps)} replicate given. At least 2 are required so the tolerance "
              f"can be measured rather than guessed.\n"
              f"       Three is the recommendation. Pass --allow-single-replicate only if you "
              f"accept an unmeasured tolerance, and say so in --notes.", file=sys.stderr)
        return 2
    for d in reps:
        if not os.path.isdir(d):
            print(f"\nERROR: replicate directory does not exist: {d}", file=sys.stderr)
            return 2

    try:
        agent = reference_agent_provenance(args)
        python, packages, agent_env = replicate_environment(reps, rep_label, agent["entrypoint"],
                                                            args.replicate_python)
        host_os = replicate_platform(reps, rep_label)
        archives, composite = content_pack_digest(args.content_pack_archive,
                                                  args.content_pack_sha256)
    except ProvenanceError as exc:
        print(f"\nREFUSING TO WRITE: {exc}", file=sys.stderr)
        return exc.code

    try:
        rows = load_split(args.split, args.tier)
    except (SplitError, OSError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 2
    problems = verify_split(rows, args.routes_root)
    if problems:
        print(f"\nERROR: the split does not agree with the frozen route bundle:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 2
    for r in rows:
        with open(os.path.join(args.routes_root, r["path"]), encoding="utf-8") as fh:
            r["prop_blueprint_id"] = blueprint_in_xml(fh.read())

    print(f"split      : {args.split}  (tier {args.tier}, {len(rows)} route(s))")
    print(f"replicates : {len(reps)}")
    for d in reps:
        print(f"             {d}")
    print(f"agent      : {agent['version']}  ({agent['repo']})")
    print(f"python     : {python}  (the replicates' interpreter)")
    print(f"os         : {host_os}  (the replicates' host)")
    print(f"agent env  : {json.dumps(agent_env)}")
    print(f"pack       : {composite}  ({len(archives)} archive(s))")

    reference = load_reference(DEFAULT_REFERENCE_TSV)

    failures: list[str] = []
    routes_out: dict = {}
    spreads: list[float] = []

    for row in rows:
        rel = row["path"]
        stem = os.path.splitext(os.path.basename(rel))[0]
        expected = row["prop_blueprint_id"]

        per_rep = []
        for d in reps:
            path, why = locate_result(d, rel, stem, args.seed)
            if path is None:
                failures.append(f"{rel}: replicate {d}: {why}")
                continue
            cp = read_checkpoint(path, stem)
            if cp.record is None:
                failures.append(f"{rel}: replicate {d}: {cp.error}")
                continue
            rec = cp.record
            ttr = rec.get("ttr_dar")
            if not isinstance(ttr, dict):
                failures.append(f"{rel}: replicate {d}: A2 -- no ttr_dar block in the record")
                continue
            observed = ttr.get("agent_type")
            if observed != expected:
                failures.append(
                    f"{rel}: replicate {d}: A1 -- route asks for {expected!r}, actor that "
                    f"spawned is {observed!r}")
                continue
            status = rec.get("status")
            if status not in CLEAN_STATUSES:
                failures.append(f"{rel}: replicate {d}: A3 -- status {status!r}")
                continue
            ds = driving_score(rec)
            if ds is None:
                failures.append(f"{rel}: replicate {d}: no scores.score_composed")
                continue
            scores = rec.get("scores") or {}
            per_rep.append({
                "replicate": rep_label[d],
                "status": status,
                "driving_score": round(float(ds), 4),
                "route_completion": scores.get("score_route"),
                "infraction_penalty": scores.get("score_penalty"),
                "observed_agent_type": observed,
            })

        if len(per_rep) != len(reps):
            continue  # already recorded in failures

        statuses = {p["status"] for p in per_rep}
        if len(statuses) > 1:
            failures.append(
                f"{rel}: replicates disagree on status {sorted(statuses)}. An unstable route "
                f"cannot be a golden -- investigate before pinning it.")
            continue

        vals = [p["driving_score"] for p in per_rep]
        spread = max(vals) - min(vals)
        spreads.append(spread)
        median_ds = round(statistics.median(vals), 4)

        entry = {
            "route_sha256": row["sha256"],
            "expected_blueprint_id": expected,
            "observed_agent_type": per_rep[0]["observed_agent_type"],
            "status": per_rep[0]["status"],
            "driving_score": median_ds,
            "route_completion": per_rep[0]["route_completion"],
            "infraction_penalty": per_rep[0]["infraction_penalty"],
            "replicates": per_rep,
            "ds_spread": round(spread, 4),
        }
        ref = reference.get(rel)
        if ref:
            try:
                entry["published_seed42_driving_score"] = float(ref["driving_score"])
                entry["published_seed42_delta"] = round(
                    median_ds - float(ref["driving_score"]), 4)
            except (KeyError, TypeError, ValueError):
                pass
        routes_out[rel] = entry

    if failures:
        print(f"\nREFUSING TO WRITE: {len(failures)} assertion failure(s) across the "
              f"replicates.\n")
        for f in failures:
            print(f"  {f}")
        print("\nA golden minted on an install that fails A1..A3 would certify the breakage "
              "forever.\nFix the install (start with probe_blueprints.py), then re-run the "
              "replicates.")
        return 1

    max_spread = max(spreads) if spreads else 0.0
    tolerance = max(args.min_tolerance, round(args.tolerance_factor * max_spread, 4))

    bundle = {
        "schema": GOLDEN_SCHEMA_ID,
        "bundle_version": BUNDLE_VERSION,
        "binds_to": BINDS_TO,
        "reportable": False,
        "split": {
            "name": "smoke",
            "tier": args.tier,
            "path": os.path.basename(args.split),
            "sha256": sha256_of(args.split),
            "n_routes": len(rows),
        },
        "reference_agent": dict(agent, env=agent_env),
        "environment": {
            "carla_version": args.carla_version,
            "content_pack_version": args.content_pack_version,
            "content_pack_sha256": composite,
            "content_pack_archives": archives,
            "gpu": args.gpu,
            "os": host_os,
            "python": python,
            "python_packages": packages,
            "runner_version": args.runner_version,
        },
        "protocol": {
            "seed": args.seed,
            "repetitions": 1,
            "n_replicates": len(reps),
        },
        "tolerance": {
            "driving_score_abs": tolerance,
            "policy": (
                f"max({args.min_tolerance}, {args.tolerance_factor} x largest observed "
                f"run-to-run spread). Largest spread across {len(reps)} replicate(s) was "
                f"{round(max_spread, 4)} DS points."
                + ("  UNMEASURED: built from a single replicate, so the floor is doing all "
                   "the work." if len(reps) < 2 else "")
            ),
            "max_observed_spread": round(max_spread, 4),
        },
        "generated": {
            "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "by": "tests/make_golden.py",
            "procedure": "tests/goldens/GENERATING.md",
            "notes": " ".join(filter(None, [COMPOSITE_RULE_NOTE, args.notes])),
        },
        "routes": routes_out,
    }

    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(bundle, fh, indent=2)
        fh.write("\n")

    print(f"\nwrote {out}")
    print(f"  routes             : {len(routes_out)}")
    print(f"  max observed spread: {max_spread:.4f} DS points")
    print(f"  tolerance          : +/-{tolerance:.4f} DS points")
    drifted = [(k, v["published_seed42_delta"]) for k, v in routes_out.items()
               if abs(v.get("published_seed42_delta") or 0.0) > tolerance]
    if drifted:
        print(f"\n  INFO: {len(drifted)} route(s) differ from the published seed-42 sweep by "
              f"more than the tolerance:")
        for k, d in drifted:
            print(f"        {k}  delta={d:+.2f}")
        print("        This is not an error -- different hardware and a different agent build "
              "legitimately\n        move closed-loop scores. It is worth understanding before "
              "you publish the bundle.")
    print("\nNext: commit the bundle, then re-run check_acceptance.py -- it should now exit 0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

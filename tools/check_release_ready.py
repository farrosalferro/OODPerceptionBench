#!/usr/bin/env python3
"""Pre-tag gate: refuse to tag a release while the scaffold is still a scaffold.

This repository is assembled from several independent workstreams. The skeleton,
licence metadata and overlay land first; routes, records, runner, procedures,
tests and the content pack are delivered into it afterwards. Between those two
points the repository *looks* complete — every directory exists and explains
itself — which is exactly when an accidental tag does damage: a README promising
475 routes next to an empty `routes/`.

It is also more than a file-presence check. It *runs* the repository's own
verification programs — the route freeze validator, the records/manifest
reconciliation, the acceptance-harness self-tests, and the two leak scanners —
because "the file exists" and "the file passes" are different claims and only the
second one is worth tagging on.

    python3 tools/check_release_ready.py                      # gate: non-zero if not ready
    python3 tools/check_release_ready.py --paper-repo PATH     # + the paper-coupled records checks
    python3 tools/check_release_ready.py --allow-todo "why"    # documented pre-tag override
    python3 tools/check_release_ready.py --fast                # presence checks only

Three outcomes per check:

    PASS   verified here, now.
    TODO   a known, unmet release requirement. **Blocks.**
    SKIP   could not be attempted in this environment (a missing optional input,
           not a defect). Does not block, but downgrades the verdict to
           CONDITIONAL and is listed by name, so "we never ran it" can never be
           mistaken for "it passed".

Exit codes
----------
    0  READY TO TAG, or READY (CONDITIONAL) with un-attempted checks listed,
       or an explicit --allow-todo override
    1  NOT READY — at least one TODO
    2  usage error

`--allow-todo REASON` is the documented escape hatch for intentional pre-tag
states (the repo is red by design for weeks while workstreams land). It demands a
written reason, prints it in a banner, and never prints READY. Push CI uses it;
a tag build must not.

Historical note: `--strict` used to be the flag that made this program block, and
its absence meant "print TODOs and exit 0" — so the default could never fail a
release. That is inverted: blocking is the default. `--strict` is accepted as a
no-op so existing runbooks keep working.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASS, TODO, SKIP = "PASS", "TODO", "SKIP"

# A check that shells out gets its own budget; none of these are long-running,
# and a hang in CI is indistinguishable from a failure nobody diagnoses.
SUBPROCESS_TIMEOUT_S = 900


def has_files(subdir: str, pattern: str = "**/*") -> int:
    root = os.path.join(REPO, subdir)
    if not os.path.isdir(root):
        return 0
    n = 0
    for p in glob.glob(os.path.join(root, pattern), recursive=True):
        if os.path.isfile(p) and os.path.basename(p) not in {"README.md", ".gitkeep"}:
            n += 1
    return n


def _records_meta_agrees() -> tuple[bool, str]:
    """Check every artifact claim in records/*.meta.json against the real files."""
    import csv
    import hashlib
    import json

    metas = glob.glob(os.path.join(REPO, "records", "*.meta.json"))
    if not metas:
        return False, "no meta.json"
    problems: list[str] = []
    for meta_path in metas:
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"unreadable: {exc}")
            continue
        base = os.path.dirname(meta_path)
        for kind, art in (meta.get("artifacts") or {}).items():
            fp = os.path.join(base, art.get("path", ""))
            if not os.path.isfile(fp):
                problems.append(f"{kind}: missing")
                continue
            blob = open(fp, "rb").read()
            if art.get("bytes") is not None and art["bytes"] != len(blob):
                problems.append(f"{kind}: size {len(blob)} != declared {art['bytes']}")
            if art.get("sha256") and hashlib.sha256(blob).hexdigest() != art["sha256"]:
                problems.append(f"{kind}: sha256 differs")
        # The column list is the schema contract; a stale one silently breaks any
        # consumer that selects by name (it already broke reconcile_with_manifest).
        cols = meta.get("columns")
        csv_art = (meta.get("artifacts") or {}).get("csv", {}).get("path")
        if cols and csv_art and os.path.isfile(os.path.join(base, csv_art)):
            with open(os.path.join(base, csv_art), newline="", encoding="utf-8") as fh:
                header = next(csv.reader(fh))
            if header != cols:
                only_meta = [c for c in cols if c not in header]
                problems.append("columns differ" + (f", meta-only {only_meta}" if only_meta else ""))
        if meta.get("n_columns") is not None and cols is not None \
                and meta["n_columns"] != len(cols):
            problems.append(f"n_columns {meta['n_columns']} != len(columns) {len(cols)}")
    return (not problems), ("; ".join(problems) if problems else "all claims verified")


def _goldens_present() -> int:
    """Real golden bundles. EXAMPLE.golden.json is documentation and never counts."""
    return len([p for p in glob.glob(os.path.join(REPO, "tests", "goldens", "*.golden.json"))
                if not os.path.basename(p).startswith("EXAMPLE")])


def _goldens_claim_is_honest() -> tuple[str, str]:
    """The README feature table and tests/VERSION must agree with the filesystem.

    The v0.9 feature table used to advertise `Acceptance goldens | base level only`
    while `tests/goldens/` held nothing but a self-described non-golden with an
    all-zero split hash. A shipped feature table is a promise; this makes the
    promise mechanically checkable so it cannot drift back.
    """
    n = _goldens_present()
    problems: list[str] = []

    version_path = os.path.join(REPO, "tests", "VERSION")
    if os.path.isfile(version_path):
        txt = open(version_path, encoding="utf-8").read()
        m = re.search(r"^goldens_present:\s*(\S+)", txt, re.M)
        if not m:
            problems.append("tests/VERSION does not declare goldens_present")
        else:
            declared = m.group(1).strip().lower() in {"true", "yes"}
            if declared != (n > 0):
                problems.append(f"tests/VERSION says goldens_present: {m.group(1)} "
                                f"but {n} bundle(s) exist")

    readme = os.path.join(REPO, "README.md")
    if os.path.isfile(readme):
        for line in open(readme, encoding="utf-8"):
            if not line.lstrip().startswith("| Acceptance goldens"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            v09 = cells[1] if len(cells) > 1 else ""
            if n == 0 and "none" not in v09.lower():
                problems.append("README feature table advertises acceptance goldens at v0.9, "
                                "but no golden bundle exists")
    return (PASS if not problems else TODO), ("; ".join(problems) if problems else
                                              f"{n} bundle(s); README and tests/VERSION agree")


def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd or REPO, capture_output=True, text=True,
                           timeout=SUBPROCESS_TIMEOUT_S)
    except FileNotFoundError as exc:
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {SUBPROCESS_TIMEOUT_S}s"
    tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-6:])
    return p.returncode, tail


# Full tail of any gate that failed, so the verdict can show why without turning
# the summary table into a wall of text.
GATE_OUTPUT: dict[str, str] = {}


def _gate(label: str, cmd: list[str], group: str,
          skip_if_missing: str | None = None) -> tuple[str, str, str, str]:
    """Run one of the repository's own verification programs as a release gate."""
    if skip_if_missing and not os.path.exists(os.path.join(REPO, skip_if_missing)):
        return label, SKIP, group, f"{skip_if_missing} not present"
    rc, tail = _run(cmd)
    if rc != 0:
        GATE_OUTPUT[label] = f"$ {' '.join(cmd)}\n{tail}"
    if rc == 0:
        return label, PASS, group, "exit 0"
    # A missing third-party import is an environment gap, not a repository
    # defect. Say so instead of reporting a red gate the maintainer cannot fix
    # by editing the repo.
    if "ModuleNotFoundError" in tail or rc == 127:
        return label, SKIP, group, f"dependency unavailable here ({tail.splitlines()[-1][:80]})"
    return label, TODO, group, f"exit {rc}: {tail.splitlines()[-1][:120] if tail else 'no output'}"


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _repo_files import repo_files  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strict", action="store_true",
                    help="accepted for backwards compatibility; blocking is now the default")
    ap.add_argument("--allow-todo", metavar="REASON", default=None,
                    help="documented override for an intentional pre-tag state; "
                         "exits 0 with the reason printed, never prints READY")
    ap.add_argument("--fast", action="store_true",
                    help="presence checks only; do not run the repository's verification programs")
    ap.add_argument("--paper-repo", metavar="PATH", default=None,
                    help="path to the paper repository, enabling the two records checks that "
                         "replay the paper's own statistics pipeline (records/verify.sh)")
    ap.add_argument("--work-dir", metavar="PATH", default=None,
                    help="scratch directory for --paper-repo (default: a temporary directory)")
    args = ap.parse_args()

    if args.allow_todo is not None and not args.allow_todo.strip():
        ap.error("--allow-todo requires a non-empty reason")

    # (label, state, group, detail)
    checks: list[tuple[str, str, str, str]] = []

    def add(label: str, ok: bool, group: str, detail: str = "") -> None:
        checks.append((label, PASS if ok else TODO, group, detail))

    # --- landed in this workstream ---------------------------------------
    for name in ("README.md", "LICENSE", "NOTICE", "CITATION.cff", "VERSION", "setup.sh"):
        add(f"{name} present", os.path.isfile(os.path.join(REPO, name)), "scaffold")

    n_patches = len(glob.glob(os.path.join(REPO, "patches", "*.patch")))
    add(f"patches/ populated ({n_patches} patches)", n_patches >= 26, "scaffold")
    add("patches/UPSTREAM.txt pins a 40-char SHA",
        bool(re.search(r"CARLA_GARAGE_SHA\s*=\s*[0-9a-f]{40}",
                       open(os.path.join(REPO, "patches", "UPSTREAM.txt")).read())),
        "scaffold")
    add("CI workflow present",
        os.path.isfile(os.path.join(REPO, ".github", "workflows", "setup.yml")), "scaffold")

    # --- delivered by other workstreams ----------------------------------
    n_routes = len(glob.glob(os.path.join(REPO, "routes", "*", "*", "*", "*.xml")))
    add(f"routes/ has exactly 475 XMLs (found {n_routes})", n_routes == 475, "routes")
    add("routes/MANIFEST.tsv present",
        os.path.isfile(os.path.join(REPO, "routes", "MANIFEST.tsv")), "routes")
    add("routes/EXCLUSIONS.md present",
        os.path.isfile(os.path.join(REPO, "routes", "EXCLUSIONS.md")), "routes")

    add("records/ populated", has_files("records") > 0, "records")
    add("runner/run_benchmark.py present",
        os.path.isfile(os.path.join(REPO, "runner", "run_benchmark.py")), "runner")
    add("docs/replacing-props.md present",
        os.path.isfile(os.path.join(REPO, "docs", "replacing-props.md")), "docs")
    n_proc = len(glob.glob(os.path.join(REPO, "docs", "**", "import_procedure_*.md"),
                           recursive=True))
    add(f"all three import procedures present (found {n_proc})", n_proc >= 3, "docs")
    add("import stage scripts present",
        has_files("docs/stages") > 0 or has_files("docs/procedures") > 0, "docs")
    add("tests/ populated", has_files("tests") > 0, "tests")
    add("assets/INSTALL.md present",
        os.path.isfile(os.path.join(REPO, "assets", "INSTALL.md")), "assets")
    add("assets/ content pack populated", has_files("assets") > 1, "assets")

    # --- the repository's own verification programs, actually run ---------
    if args.fast:
        for label in ("routes/validate_routes.py passes",
                      "records reconcile against routes/MANIFEST.tsv",
                      "tests/selftest.py passes",
                      "no cluster paths (tools/check_no_cluster_paths.py)",
                      "no forbidden source token (tools/check_forbidden_tokens.py)"):
            checks.append((label, SKIP, "gates", "--fast: not attempted"))
    else:
        checks.append(_gate("routes/validate_routes.py passes",
                            [sys.executable, "routes/validate_routes.py"], "gates",
                            skip_if_missing="routes/MANIFEST.tsv"))

        rec_csv = sorted(glob.glob(os.path.join(REPO, "records", "*.csv")))
        if len(rec_csv) != 1:
            checks.append(("records reconcile against routes/MANIFEST.tsv", TODO, "gates",
                           f"expected exactly one records CSV, found {len(rec_csv)}"))
        else:
            checks.append(_gate("records reconcile against routes/MANIFEST.tsv",
                                [sys.executable, "records/reconcile_with_manifest.py",
                                 "--records", os.path.relpath(rec_csv[0], REPO),
                                 "--manifest", "routes/MANIFEST.tsv"], "gates",
                                skip_if_missing="routes/MANIFEST.tsv"))

        checks.append(_gate("tests/selftest.py passes",
                            [sys.executable, "tests/selftest.py"], "gates",
                            skip_if_missing="tests/selftest.py"))
        checks.append(_gate("no cluster paths (tools/check_no_cluster_paths.py)",
                            [sys.executable, "tools/check_no_cluster_paths.py"], "gates"))
        checks.append(_gate("no forbidden source token (tools/check_forbidden_tokens.py)",
                            [sys.executable, "tools/check_forbidden_tokens.py", "--quiet"],
                            "gates"))

    # The two records checks that replay the paper's own statistics pipeline need
    # the paper repository, which is private until arXiv and is not on a hosted
    # runner. Not attempted is NOT the same as passed, so it is reported as SKIP
    # and named in the verdict rather than quietly omitted.
    if args.fast:
        checks.append(("records/verify.sh (paper-coupled: frozen CSVs + Table 1)", SKIP,
                       "gates", "--fast: not attempted"))
    elif args.paper_repo:
        import tempfile
        work = args.work_dir or tempfile.mkdtemp(prefix="release_ready_")
        os.makedirs(work, exist_ok=True)
        checks.append(_gate("records/verify.sh (paper-coupled: frozen CSVs + Table 1)",
                            ["bash", "records/verify.sh", os.path.abspath(args.paper_repo), work],
                            "gates", skip_if_missing="records/verify.sh"))
    else:
        checks.append(("records/verify.sh (paper-coupled: frozen CSVs + Table 1)", SKIP,
                       "gates", "needs --paper-repo; run it before tagging"))

    # --- cross-cutting -----------------------------------------------------
    citation = open(os.path.join(REPO, "CITATION.cff")).read()
    add("CITATION.cff arXiv id filled in (placeholder removed)",
        "XXXX.XXXXX" not in citation, "metadata")

    # The asset binaries are hosted outside git. Until that host exists, the repo
    # tells users to download from a placeholder, which is worse than saying
    # nothing: assets/ documents an install flow whose first step cannot be run.
    placeholder = "HUGGINGFACE_REPO" + "_URL_TBD"
    unresolved_host = [
        os.path.relpath(p, REPO)
        for p in glob.glob(os.path.join(REPO, "assets", "*.md"))
        if placeholder in open(p, encoding="utf-8").read()
    ]
    add(f"asset-pack download URL resolved ({len(unresolved_host)} file(s) "
        f"still say {placeholder})", not unresolved_host, "metadata")

    # records/*.meta.json is the published provenance of the published numbers.
    # If it describes a different artifact than the one beside it — different
    # checksum, size, or column set — then every claim made about the records was
    # measured against a file nobody can obtain.
    meta_ok, meta_why = _records_meta_agrees()
    add("records meta.json matches the shipped artifacts", meta_ok, "metadata")

    # A feature table is a promise. Keep it checkable.
    g_state, g_why = _goldens_claim_is_honest()
    checks.append(("golden-bundle claims match the filesystem", g_state, "metadata", g_why))

    # Every markdown link to a repo-local file must resolve.
    # Only markdown WE ship. Globbing the tree also linted the vendored upstream docs that
    # setup.sh installs under third_party/, whose relative targets were never ours to satisfy
    # -- 45 "broken" links on any correctly-installed checkout. See tools/_repo_files.py.
    broken: list[str] = []
    for rel in repo_files(REPO):
        if not rel.endswith(".md"):
            continue
        md = os.path.join(REPO, rel)
        base = os.path.dirname(md)
        for target in re.findall(r"\]\(([^)#:]+?)\)", open(md, encoding="utf-8").read()):
            if target.startswith(("http", "mailto", "#")):
                continue
            if not os.path.exists(os.path.join(base, target)):
                broken.append(f"{os.path.relpath(md, REPO)} -> {target}")
    add(f"no broken repo-local markdown links ({len(broken)} broken)", not broken, "metadata")

    # --- report ------------------------------------------------------------
    width = max(len(c[0]) for c in checks)
    group = None
    for label, state, grp, detail in checks:
        if grp != group:
            print(f"\n[{grp}]")
            group = grp
        suffix = f"   ({detail})" if detail and state != PASS else ""
        print(f"  {state}  {label:<{width}}{suffix}")

    if broken:
        print("\nbroken links:")
        for b in broken:
            print(f"  {b}")

    if not meta_ok:
        print("\nrecords meta.json disagreements:")
        for why in meta_why.split("; "):
            print(f"  {why}")

    if unresolved_host:
        print(f"\nfiles still naming {placeholder}:")
        for f in unresolved_host:
            print(f"  {f}")

    todo = [c for c in checks if c[1] == TODO]
    skipped = [c for c in checks if c[1] == SKIP]
    ready = len(checks) - len(todo) - len(skipped)
    print(f"\n{ready}/{len(checks)} verified; {len(todo)} outstanding; "
          f"{len(skipped)} not attempted")

    if skipped:
        print("\nnot attempted here (NOT the same as passed):")
        for label, _, _, detail in skipped:
            print(f"  {label}  —  {detail}")

    if args.strict:
        print("\nnote: --strict is now the default and is accepted only for compatibility.")

    if todo:
        print("\nNOT READY TO TAG")
        for label, _, _, detail in todo:
            print(f"  TODO  {label}" + (f"  —  {detail}" if detail else ""))
            for line in GATE_OUTPUT.get(label, "").splitlines():
                print(f"        | {line}")
        if args.allow_todo is not None:
            print("\n" + "=" * 72)
            print("OVERRIDE ACTIVE — this is not a release-readiness verdict.")
            print(f"reason: {args.allow_todo}")
            print("The TODOs above are unresolved. Do not tag from this run.")
            print("=" * 72)
            return 0
        return 1

    if skipped:
        print("\nREADY TO TAG (CONDITIONAL) — every check that could run passed, but the")
        print("checks listed under 'not attempted' were never executed. Run them before")
        print("tagging, or accept the tag without them knowingly.")
        return 0

    print("\nREADY TO TAG")
    return 0


if __name__ == "__main__":
    sys.exit(main())

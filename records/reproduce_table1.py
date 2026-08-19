#!/usr/bin/env python3
"""ACCEPTANCE TEST — regenerate Table 1 from the published records.

This directory's output IS the published numbers, so the only meaningful test is
end-to-end: drive the paper's own, unmodified statistics pipeline from the
released records instead of the 766 GB result tree, and require that every
headline figure and the emitted LaTeX come out identical.

Method (deliberately zero-drift): the paper's statistics scripts are COPIED
UNMODIFIED into scratch trees whose `eval/` is materialised from the records
CSV. Those scripts compute `REPO = parents[2]`, so a copy at
`<work>/docs/stats/*.py` reads `<work>/eval/` and writes `<work>/docs/stats/`
and `<work>/tables/`. Nothing in the paper repo is written. No statistic is
reimplemented here, so this cannot drift from what the paper actually ran.

Three comparisons are made, in increasing strictness:

  A. HEADLINE   `final_stats_summary.json` vs the committed frozen file, full
                nested deep-diff. Every published aggregate lives here.
  B. LATEX      `tables/table1_headline.tex` and `table_percell_granular.tex`
                vs the committed .tex. This is Table 1 literally.
  C. PER-CELL   `final_stats_cells.csv` vs a SAME-INTERPRETER recomputation
                from the frozen paper CSVs. This isolates "did the records
                change the data" from "does today's scipy round Wilcoxon
                differently than the interpreter that wrote the committed
                file". Both are checked and reported separately.

    python reproduce_table1.py --records ood_perceptionbench_records_v0.9.csv \
                              --paper-repo /path/to/ood-perception-corl2026 \
                              --work-dir /tmp/w8_table1

Exits non-zero unless A, B and C all pass.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_paper_eval_csvs import export as export_analysis_csvs  # noqa: E402

CATEGORIES = ["pedestrian", "static", "vehicle"]

STATS_SCRIPTS = [
    "compute_abstract_placeholders.py",   # -> final_stats_cells.csv / _summary.json
    "compute_pdmlite_ceiling.py",         # -> pdmlite_ceiling_summary.json
    "compute_group_analysis.py",          # imports the first; cohort cross-check
]
TABLE_SCRIPTS = ["build_table1_headline.py"]
# Imported as modules by the scripts above, never executed directly.
SUPPORT_MODULES = ["build_table_pdmlite.py"]
TABLE_OUTPUTS = ["table1_headline.tex", "table_percell_granular.tex"]

# ---- the locked headline figures of the paper -----------------------------
# 3-seed average-per-route re-lock (paper commit a52528d, docs/stats/
# final_stats_summary.json). These SUPERSEDE the seed-42 figures shipped in the
# first v0.9 records: a 4.93->5.047, b 13.221->12.802, b/a 2.7->2.537, K 12->9,
# geo-deeper cells 46->45, frac 0.902->0.882, d_z 1.14->1.052, and the OOD-hit
# rates. Comparison A's full deep-diff against the committed summary is the
# authoritative check; this list is the human-readable, per-figure assertion.
LOCKED = [
    ("a  (mean DS drop, visual)",          lambda h: h["a"],                                   5.047, 0.05),
    ("b  (mean DS drop, geometric)",       lambda h: h["b"],                                  12.802, 0.05),
    ("b/a ratio",                          lambda h: h["b_over_a"],                            2.537, 0.05),
    ("n_cells",                            lambda h: h["n_cells"],                              51,   0),
    ("n_models",                           lambda h: h["n_models"],                             17,   0),
    ("K visually robust",                  lambda h: h["K"],                                     9,   0),
    ("cells geo deeper",                   lambda h: h["hierarchy_gamma_test"]["n_cells_geo_deeper"], 45, 0),
    ("frac cells geo deeper",              lambda h: h["hierarchy_gamma_test"]["frac_cells_geo_deeper"], 0.882, 0.001),
    ("gamma p_two_sided < 0.001",          lambda h: h["hierarchy_gamma_test"]["p_two_sided"] < 1e-3, True, 0),
    ("paired Cohen's d_z",                 lambda h: h["hierarchy_gamma_test"]["cohens_dz"],   1.052, 0.005),
    ("OOD-hit base (pp)",                  lambda h: h["ood_hit_rate"]["hit_rate_base_pp"],   18.55, 0.05),
    ("OOD-hit visual (pp)",                lambda h: h["ood_hit_rate"]["hit_rate_visual_pp"], 29.46, 0.05),
    ("OOD-hit geometric (pp)",             lambda h: h["ood_hit_rate"]["hit_rate_geometric_pp"], 46.16, 0.05),
    ("visual delta-hit (pp)",              lambda h: h["ood_hit_rate"]["mean_delta_hit_visual_pp"], 10.91, 0.05),
    ("visual delta-hit p ~ 3.2e-7",        lambda h: h["ood_hit_rate"]["p_delta_hit_visual"] / 3.2e-7, 1.0, 0.02),
]


def stars(p) -> str:
    if pd.isna(p):
        return ""
    return "***" if p < 0.001 else ("**" if p < 0.01 else ("*" if p < 0.05 else ""))


def run_pipeline(work: Path, src_stats: Path, python: str,
                 scripts: list[str], quiet: bool = False) -> bool:
    (work / "docs" / "stats").mkdir(parents=True, exist_ok=True)
    (work / "tables").mkdir(parents=True, exist_ok=True)
    for name in list(scripts) + SUPPORT_MODULES:
        shutil.copy2(src_stats / name, work / "docs" / "stats" / name)
    for name in scripts:
        r = subprocess.run([python, str(work / "docs" / "stats" / name)],
                           cwd=work, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"      FAILED: {name}\n{r.stderr[-3000:]}")
            return False
        if not quiet:
            print(f"      ok: {name}")
    return True


def deep_diff(x, y, path="", tol=1e-12):
    out = []
    if isinstance(x, dict) and isinstance(y, dict):
        for k in sorted(set(x) | set(y)):
            out += deep_diff(x.get(k), y.get(k), f"{path}/{k}", tol)
    elif isinstance(x, list) and isinstance(y, list):
        if x != y:
            out.append((path, x, y))
    elif isinstance(x, float) and isinstance(y, float):
        if not abs(x - y) <= tol * max(1.0, abs(x), abs(y)):
            out.append((path, x, y))
    elif x != y:
        out.append((path, x, y))
    return out


def frame_equal(a: pd.DataFrame, b: pd.DataFrame) -> tuple[bool, str]:
    try:
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12, atol=1e-15)
        return True, ""
    except AssertionError as exc:
        return False, str(exc)[:1500]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, type=Path)
    ap.add_argument("--paper-repo", required=True, type=Path)
    ap.add_argument("--work-dir", required=True, type=Path)
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()

    src_stats = args.paper_repo / "docs" / "stats"
    src_tables = args.paper_repo / "tables"
    frozen_eval = (args.paper_repo / "eval").resolve()

    work: Path = args.work_dir.resolve()
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    ref = work.parent / (work.name + "_reference")
    if ref.exists():
        shutil.rmtree(ref)
    ref.mkdir(parents=True)

    all_scripts = STATS_SCRIPTS + TABLE_SCRIPTS

    print(f"[1/5] exporting analysis CSVs from {args.records.name}")
    written = export_analysis_csvs(args.records, work / "eval")
    print(f"      {len(written)} (model, category) CSVs, "
          f"{sum(written.values())} rows total")

    print("[2/5] running the paper's UNMODIFIED pipeline against the records")
    if not run_pipeline(work, src_stats, args.python, all_scripts):
        return 1

    print("[3/5] running the SAME pipeline against the frozen paper CSVs "
          "(same-interpreter reference)")
    (ref / "eval").symlink_to(frozen_eval)
    if not run_pipeline(ref, src_stats, args.python, all_scripts, quiet=True):
        return 1
    print("      ok")

    ok_a = ok_b = ok_c = True

    # ---------------- A. headline aggregates ------------------------------
    print("\n[4/5] A. headline aggregates vs committed final_stats_summary.json")
    mine = json.loads((work / "docs" / "stats" / "final_stats_summary.json").read_text())
    frozen = json.loads((src_stats / "final_stats_summary.json").read_text())
    diffs = deep_diff(mine, frozen)
    print(f"      full nested deep-diff: {len(diffs)} difference(s)")
    for p, x, y in diffs[:40]:
        print(f"        {p}: records={x!r}  committed={y!r}")
    ok_a &= not diffs

    h = mine["headline"]
    pm_mine = pd.read_csv(work / "docs" / "stats" / "final_stats_per_model.csv")
    n_sig_mine = int((pm_mine["p_geometric_pooled"] < 0.05).sum())
    print("\n      locked headline figures of the paper:")
    for label, get, expected, tol in LOCKED:
        got = get(h)
        if isinstance(expected, bool):
            ok = bool(got) == expected
            shown = got
        else:
            ok = abs(float(got) - float(expected)) <= tol
            shown = round(float(got), 4)
        ok_a &= ok
        print(f"        [{'PASS' if ok else 'FAIL'}] {label:34s} "
              f"got {shown}  expected {expected}")
    ok_sig = n_sig_mine == 17
    ok_a &= ok_sig
    print(f"        [{'PASS' if ok_sig else 'FAIL'}] "
          f"{'sig. geometric regression':34s} got {n_sig_mine}/17  expected 17")

    # ---------------- B. the LaTeX tables themselves ----------------------
    print("\n      B. emitted LaTeX vs committed tables/")
    for tex in TABLE_OUTPUTS:
        got = (work / "tables" / tex).read_text()
        want = (src_tables / tex).read_text()
        same = got == want
        ok_b &= same
        print(f"        [{'PASS' if same else 'FAIL'}] {tex} "
              f"{'byte-identical' if same else 'DIFFERS'}")
        if not same:
            import difflib
            for line in list(difflib.unified_diff(
                    want.splitlines(), got.splitlines(),
                    "committed", "from-records", lineterm=""))[:40]:
                print(f"           {line}")

    # ---------------- C. per-cell table -----------------------------------
    print("\n[5/5] C. per-cell final_stats_cells.csv")
    c_mine = pd.read_csv(work / "docs" / "stats" / "final_stats_cells.csv")
    c_ref = pd.read_csv(ref / "docs" / "stats" / "final_stats_cells.csv")
    c_committed = pd.read_csv(src_stats / "final_stats_cells.csv")

    same_ref, msg = frame_equal(c_mine, c_ref)
    ok_c &= same_ref
    print(f"      [{'PASS' if same_ref else 'FAIL'}] vs same-interpreter "
          f"recomputation from the frozen paper CSVs "
          f"({'identical' if same_ref else 'DIFFERS'})")
    if not same_ref:
        print(msg)

    # Provenance note: the committed file was written by a different scipy.
    # That is not a records problem (proved by the check above), but it must
    # not move a single significance verdict.
    star_changes = 0
    pcell_diffs = 0
    for col in ("p_visual", "p_geometric"):
        d = (c_committed[col] - c_mine[col]).abs() > 1e-12
        pcell_diffs += int(d.sum())
        for i in c_committed.index[d]:
            if stars(c_committed.loc[i, col]) != stars(c_mine.loc[i, col]):
                star_changes += 1
                print(f"        STAR MOVED {c_committed.loc[i,'model']} "
                      f"{c_committed.loc[i,'category']} {col}: "
                      f"{c_committed.loc[i,col]} -> {c_mine.loc[i,col]}")
    ok_stars = star_changes == 0
    ok_c &= ok_stars
    print(f"      [{'PASS' if ok_stars else 'FAIL'}] per-cell significance "
          f"verdicts unchanged vs the committed file "
          f"({star_changes} of 102 stars moved; {pcell_diffs} p-values differ "
          f"by scipy rounding on small-n cells)")

    for col, want in (("p_visual", (c_committed["p_visual"] > 0.05).sum()),
                      ("p_geometric", (c_committed["p_geometric"] < 0.05).sum())):
        got = ((c_mine[col] > 0.05) if col == "p_visual"
               else (c_mine[col] < 0.05)).sum()
        ok = int(got) == int(want)
        ok_c &= ok
        print(f"      [{'PASS' if ok else 'FAIL'}] {col} threshold count: "
              f"{int(got)} (committed {int(want)})")

    passed = ok_a and ok_b and ok_c
    print("\n" + "=" * 78)
    print(f"  A headline aggregates .......... {'PASS' if ok_a else 'FAIL'}")
    print(f"  B Table 1 LaTeX ............... {'PASS' if ok_b else 'FAIL'}")
    print(f"  C per-cell table .............. {'PASS' if ok_c else 'FAIL'}")
    print("RESULT:", "PASS - Table 1 regenerates EXACTLY from the published records"
          if passed else "FAIL - records do NOT reproduce the paper")
    print("=" * 78)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

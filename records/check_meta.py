#!/usr/bin/env python3
"""Check 0 — `*.meta.json` describes the artifacts that are actually here.

This check exists because the failure it catches has already happened once: the
CSV and parquet were regenerated without regenerating `meta.json`, so the file
went on declaring a sha256, a byte count and a 65-entry column list that no
longer matched anything on disk. Nothing else in the bundle noticed, because
nothing else read `meta.json`.

`meta.json` is written by `build_records.py` in the same pass that writes the
artifacts, so it cannot drift **if the generator is the only thing that ever
writes them**. This script is the tripwire for the case where it isn't.

Hard-checked (any mismatch exits non-zero):

  * csv     sha256 + byte size
  * parquet sha256 + byte size
  * `n_rows`    == the CSV's data-row count == the parquet's row count
  * `n_columns` == the CSV header width
  * `columns`   == the CSV header, in order (an ordering change is a schema
                  change and must be deliberate)
  * `VERSION` agrees with `meta.json` on version / binds_to / schema / n_rows

Reported but NOT fatal:

  * `generator_sha256` vs the bundled `build_records.py`. A mismatch means the
    generator has been edited since the artifacts were produced — re-run it
    before shipping. This is a warning rather than a failure on purpose: making
    every comment edit a hard release blocker gets the check disabled, and the
    invariant that actually protects the published numbers is the artifact
    digest above.

    python check_meta.py --records ood_perceptionbench_records_v0.9.csv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check(label: str, got, want, ok: bool, results: list) -> None:
    results.append(ok)
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"          declared: {want!r}")
        print(f"          actual:   {got!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True, type=Path,
                    help="the released CSV; meta/parquet are found beside it")
    args = ap.parse_args()

    csv_path: Path = args.records.resolve()
    here = csv_path.parent
    stem = csv_path.name[: -len(".csv")]
    meta_path = here / f"{stem}.meta.json"
    pq_path = here / f"{stem}.parquet"

    for p in (csv_path, meta_path):
        if not p.exists():
            print(f"FATAL: missing {p}")
            return 2

    meta = json.loads(meta_path.read_text())
    results: list[bool] = []

    print(f"meta: {meta_path.name}")
    print(f"  schema {meta.get('schema')}  version {meta.get('version')}  "
          f"binds_to {meta.get('binds_to')}  generated {meta.get('generated_utc')}")

    # ---- artifact digests ------------------------------------------------
    declared = meta.get("artifacts", {})
    for key, path in (("csv", csv_path), ("parquet", pq_path)):
        d = declared.get(key)
        if d is None:
            if key == "parquet" and not pq_path.exists():
                print(f"  [SKIP] no parquet declared and none on disk")
                continue
            check(f"{key}: declared in meta.json", "absent", "present", False, results)
            continue
        if not path.exists():
            check(f"{key}: file present", "missing", d.get("path"), False, results)
            continue
        check(f"{key}: filename", path.name, d.get("path"),
              path.name == d.get("path"), results)
        size = path.stat().st_size
        check(f"{key}: byte size", size, d.get("bytes"), size == d.get("bytes"), results)
        digest = sha256_of(path)
        check(f"{key}: sha256", digest, d.get("sha256"),
              digest == d.get("sha256"), results)

    # ---- shape + column list --------------------------------------------
    import csv as _csv

    with csv_path.open(newline="") as fh:
        reader = _csv.reader(fh)
        header = next(reader)
        n_data_rows = sum(1 for _ in reader)

    check("n_rows == CSV data rows", n_data_rows, meta.get("n_rows"),
          n_data_rows == meta.get("n_rows"), results)
    check("n_columns == CSV header width", len(header), meta.get("n_columns"),
          len(header) == meta.get("n_columns"), results)

    declared_cols = list(meta.get("columns") or [])
    same_cols = declared_cols == header
    results.append(same_cols)
    print(f"  [{'PASS' if same_cols else 'FAIL'}] columns list matches the CSV "
          f"header exactly and in order ({len(header)} columns)")
    if not same_cols:
        only_meta = [c for c in declared_cols if c not in header]
        only_csv = [c for c in header if c not in declared_cols]
        print(f"          declared but absent from the CSV: {only_meta}")
        print(f"          present in the CSV but undeclared: {only_csv}")
        if not only_meta and not only_csv:
            print("          same set, different order")

    if pq_path.exists():
        try:
            import pyarrow.parquet as pq  # noqa: PLC0415

            pf = pq.ParquetFile(pq_path)
            n_pq = pf.metadata.num_rows
            n_pq_cols = pf.metadata.num_columns
            check("parquet row count == n_rows", n_pq, meta.get("n_rows"),
                  n_pq == meta.get("n_rows"), results)
            check("parquet column count == n_columns", n_pq_cols,
                  meta.get("n_columns"), n_pq_cols == meta.get("n_columns"), results)
        except ImportError:
            print("  [SKIP] pyarrow not installed; parquet shape unchecked")

    # ---- VERSION stamp ---------------------------------------------------
    vpath = here / "VERSION"
    if vpath.exists():
        v = {}
        for line in vpath.read_text().splitlines():
            if ":" in line and not line.startswith(" "):
                k, _, val = line.partition(":")
                v[k.strip()] = val.strip()
        for key, mkey in (("version", "version"), ("binds_to", "binds_to"),
                          ("schema", "schema")):
            if key in v:
                check(f"VERSION.{key} agrees with meta.json", v[key], meta.get(mkey),
                      v[key] == str(meta.get(mkey)), results)
        if "n_rows" in v:
            check("VERSION.n_rows agrees with meta.json", v["n_rows"],
                  str(meta.get("n_rows")), v["n_rows"] == str(meta.get("n_rows")),
                  results)

    # ---- generator provenance (warning only; see the module docstring) ----
    gen_declared = meta.get("generator_sha256")
    gen_path = here / "build_records.py"
    print()
    if gen_declared and gen_path.exists():
        gen_actual = sha256_of(gen_path)
        if gen_actual == gen_declared:
            print(f"  [PASS] generator_sha256 matches the bundled "
                  f"build_records.py ({gen_declared[:12]}..)")
        else:
            print("  [WARN] build_records.py has changed since these artifacts "
                  "were generated")
            print(f"          meta.json declares: {gen_declared}")
            print(f"          bundled script is: {gen_actual}")
            print("          -> re-run build_records.py before shipping, so the "
                  "artifact provably comes from the bundled code.")
    else:
        print("  [WARN] meta.json carries no generator_sha256; artifact-to-code "
              "provenance is unverifiable.")

    ok = all(results)
    print("\nRESULT:", "PASS - meta.json describes exactly what ships"
          if ok else "FAIL - meta.json does not describe the shipped artifacts")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

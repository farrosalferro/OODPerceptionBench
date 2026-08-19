# Release checklist — v0.9.0

**Version stamp: 0.9.0, binds to arXiv v1.**
Run `python3 tools/check_release_ready.py --strict` before tagging. It mechanises most of what
is below; this file carries the parts a script cannot judge.

---

## Landed (scaffold workstream)

- [x] Directory skeleton, each directory with a `README.md` stating its purpose and its
      non-purpose
- [x] `LICENSE` — MIT, with an explicit scope note (it does **not** cover the assets)
- [x] `NOTICE` — CARLA CC-BY attribution, Bench2Drive citation and non-redistribution statement,
      carla_garage MIT, all six shipped assets with author + licence, NonCommercial term flagged
      at the top rather than buried in the table
- [x] `CITATION.cff` — paper metadata, three authors, version bound to the tag
- [x] `VERSION` — version stamp and the two-version discipline note
- [x] `patches/` — 26 patches against a pinned upstream SHA, with `MANIFEST.md` (what and why)
      and `EXCLUDED.md` (what was left out and why)
- [x] `setup.sh` — clone pinned upstream, dry-run, apply, verify post-conditions; idempotent;
      fails loudly on patch rot
- [x] CI — `setup.sh` on a clean checkout, patches reverse cleanly, route coverage, cluster-path
      scrub, metadata parse, NOTICE completeness, no purchase recipe
- [x] `tools/check_route_coverage.py`, `tools/check_no_cluster_paths.py`,
      `tools/check_release_ready.py`

## Landed at integration (2026-08-04)

- [x] `routes/` — 475 XMLs + `MANIFEST.tsv` (sha256) + `EXCLUSIONS.md` + `validate_routes.py`
      (**38/38 checks green**, all 475 checksums verified)
- [x] `records/` — 24,700 rows × 64 columns, CSV + `load.py`, seeds 42/43/44, 17 models + PDM-Lite (PDM-Lite seed 42)
- [x] `runner/` — `run_benchmark.py`, worker pool, resume, SLURM backend (**222 tests green**;
      local backend exercised against real CARLA; multi-GPU/full-scale open and SLURM broken —
      see `runner/STATUS.md`)
- [x] `docs/` — `replacing-props.md`, `ASSET_TRAPS.md`, the three `import_procedure_*.md`
      and the parameterised `stages/` scripts
- [x] `classifier/` — the three dimension-checker notebooks (the admissibility rule itself)
- [x] `tests/` — smoke split + harness + self-tests (**34 tests green**) plus the measured
      nine-route PDM-Lite v0.9 golden
- [x] `assets/` — install / verify / attribution / checksums for the six shippable props

### Scope notes recorded at integration

- **`docs/design.md`, `docs/protocol.md` and `docs/metrics.md` were never written.** The
  earlier skeleton listed them. The protocol lives in the top-level `README.md`, the column
  semantics in `records/SCHEMA.md`, and the class rule in `docs/replacing-props.md` plus
  `classifier/`. Either write them or stop promising them — the READMEs no longer do.
- **`docs/procedures/` was collapsed.** The procedures live at the top level of `docs/`.
- **The asset binaries are not in git.** `assets/` ships the installer, checksums, attribution
  and verifier; the three tarballs (166.6 MB) are hosted separately.

## Blockers found at integration — must be closed before the first push

- [x] **RESOLVED 2026-08-04 — `records/*.meta.json` now matches its artifacts.** The rename was
      moved inside `build_records.py`, the generator re-run, and meta.json regenerated from the
      artifacts in the same pass (now also binding `generator_sha256` + `rename_map_sha256`).
      `verify.sh` exits 0 with all four checks green; `reconcile_with_manifest.py` no longer
      raises `KeyError` and cross-checks 8,541 rows with 0 disagreements.
- [ ] **The asset-pack download URL is a placeholder** (`https://huggingface.co/datasets/farrosalferro24/OODPerceptionBench`) in
      `assets/README.md` and `assets/INSTALL.md`. Create the host, upload the three tarballs,
      substitute the URL. Gated by `tools/check_release_ready.py`.
- [x] **`docs/import_procedure_vehicle.md` ships behind a DRAFT banner** by decision. Confirm
      the banner is still the first thing in the file before tagging. (2026-08-16: confirmed —
      the `⚠ DRAFT — NOT COMPLETE` banner is the first content in the file.)

## Before tagging — judgement calls a script cannot make

- [ ] **arXiv ID** substituted into `CITATION.cff` (the placeholder block is marked) and, if the
      paper is public, linked from `README.md`.
- [ ] **Paper title** confirmed. The title in `CITATION.cff` is marked provisional.
- [x] **Runnability table re-derived, not copied.** Re-counted from the integrated `routes/`
      tree on 2026-08-04, not copied from the plan: static **30**/70, pedestrian **126**/162,
      vehicle **81**/243, total **237**/475; **145** base-level route files (covering the 55
      distinct base routes); `firefighter` is **18** routes, not 19 — `NOTICE` corrected. Redo
      this count if any prop's licence verdict changes.
- [x] **`tools/dev/check_notice_assets.py --assets-tsv <private audit>` passes.** This is the
      only check that can prove no non-redistributable asset leaked into `assets/` or into the
      NOTICE attributions. It cannot run in public CI because the audit is private. (2026-08-16:
      PASS against the private `ASSETS.tsv` — audit reports 6 ship, 12 replace.)
- [ ] **Asset pack contains exactly six props.** Verify by enumeration, not by trusting the
      build.
- [ ] **The `EXCLUDED.md` §D judgement calls reviewed by a human.** Three files were excluded on
      a judgement call rather than a rule; a reviewer should agree before the set is frozen.
- [x] **Fresh GitHub clone validation** completed 2026-08-11/12: `setup.sh` applied all 26
      patches and was idempotent; the release gate/hygiene checks and 222 runner tests passed;
      explicit config paths drove real smoke routes and the nine-route golden flow. The stricter
      “internal mounts do not exist on the host” H10 proof remains open and is not claimed here.
- [x] **Push CI green on `master`** as of 2026-08-12: both `overlay-setup` and `acceptance`
      passed on the pre-H6 head. The H6 commit must receive the same two green checks before this
      ticket closes. **Install the hook in every clone:**
      `ln -sf ../../tools/pre-push .git/hooks/pre-push`.
- [ ] **First scheduled `overlay-setup` after the 2026-08-10 repair is green.** The latest
      scheduled run is still the pre-repair failure; push CI is green, but a scheduled event has
      not run on the repaired history yet. Do not silently count the old red schedule as green.
- [ ] **The numpy pin decision reviewed.** Upstream has merged a numpy ≥ 1.24 compatibility fix
      *after* our pinned SHA. We stay on the older commit for reproducibility, which means users
      on modern numpy must pin `numpy<1.24`. If the runner or the environment documentation ends
      up specifying a numpy version, it has to agree with `patches/UPSTREAM.txt` — verified, all
      26 patches also apply to the newer tip, so advancing the pin is available if the
      reproducibility argument is judged to be outweighed.

## Explicitly out of scope for v0.9

- Tier C (re-running the 17 baselines) — deferred to `contrib/`.
- Multi-seed records — v0.9 ships all three seeds (42/43/44), matching the paper's 3-seed
  average-per-route headline. (PDM-Lite ceiling and the OOD-collision metric are seed-42-based;
  see the records `SCHEMA.md`.)
- Replacement assets for the twelve non-redistributable props — that is v1.0, and it requires a
  re-run and a re-stamp.
- Accepting third-party result rows into `records/` — governance deferred to v1.0.

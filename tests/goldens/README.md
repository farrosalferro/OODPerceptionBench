# `tests/goldens/` — expected outputs for the smoke split

**Bundle version:** v0.9 · **Binds to:** arXiv v1

A golden bundle holds the expected results for the smoke split. `check_acceptance.py` uses it for
assertion A4. With a compatible bundle it can exit 0; without one it runs A1–A3 and exits 3
(`INCONCLUSIVE`), never 0.

`pdmlite_seed42_v0.9.golden.json` was measured with CARLA 0.9.15 and the v0.9 content pack: three
one-worker replicates from separate output roots, all nine route scores 100.0, spread 0.0 DS,
tolerance ±1.0 DS. The bundle records its full environment and agent provenance.

| File | What it is |
|---|---|
| [`GENERATING.md`](GENERATING.md) | the procedure, using PDM-Lite as reference agent |
| [`golden_schema.json`](golden_schema.json) | the file format, field by field |
| [`EXAMPLE.golden.json`](EXAMPLE.golden.json) | a worked example with placeholder numbers — **not usable**; `check_acceptance.py` skips any file starting with `EXAMPLE` |
| [`pdmlite_seed42_v0.9.golden.json`](pdmlite_seed42_v0.9.golden.json) | the measured v0.9 smoke-split bundle |

## What a golden is

- a **median over ≥ 2 independent replicate runs**, with every replicate value kept in the file;
- a **tolerance from the measured run-to-run spread**, not a guess;
- **stamped with its environment**: CARLA version, content-pack version and sha256, reference
  agent version, GPU;
- **bound to one smoke split** by that split's sha256. A bundle for a different split is rejected.

`make_golden.py` enforces all of this, and writes nothing if A1–A3 fail in any replicate.

## Naming

```
goldens/<agent>_seed<seed>_<bundle_version>.golden.json
e.g. goldens/pdmlite_seed42_v0.9.golden.json
```

`check_acceptance.py` auto-discovers a single `*.golden.json` here. If there are two or more, it
stops and asks you to pick one with `--goldens`.

## Validity

A golden is valid **only** for the content-pack version it was made with. A v1.0 asset
replacement changes the stimulus, so scores are not comparable even where the blueprint id is
the same. A stale bundle is a loud error: the bundle records `bundle_version`,
`environment.content_pack_version` and the split sha256.

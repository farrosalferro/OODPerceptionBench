# `tests/goldens/` — expected outputs for the smoke split

**Bundle version:** v0.9 · **Binds to:** arXiv v1

## Status at v0.9: **empty by design**

There is no golden bundle in this directory. Goldens require a GPU, a running CARLA 0.9.15 and
the installed content pack; none of that exists in CI or in the environment this release was
assembled in. Shipping a fabricated one would be worse than shipping none.

What ships instead:

| File | What it is |
|---|---|
| [`GENERATING.md`](GENERATING.md) | the procedure, using PDM-Lite as reference agent |
| [`golden_schema.json`](golden_schema.json) | the file format, field by field |
| [`EXAMPLE.golden.json`](EXAMPLE.golden.json) | a worked example — **not usable**, see below |

`check_acceptance.py` skips any file starting with `EXAMPLE`, so the example cannot be picked up
by accident. Its numbers are placeholders and its provenance fields say so.

## Consequence: the harness cannot report a pass

With no golden bundle present, `check_acceptance.py` still runs assertions A1–A3 — which is
where nearly all of the defensive value is, because A1 is the one that catches a missing asset —
but it prints an `INCONCLUSIVE` banner and **exits 3**. Never 0.

That is deliberate. A benchmark harness that exits 0 when it could not check anything is exactly
the failure mode this directory exists to prevent, one level up.

## What a golden is

A golden is not "the numbers we got". It is:

- a **median over ≥ 2 independent replicate runs**, with every replicate value retained in the
  file so a reader can audit it;
- a **tolerance derived from the measured run-to-run spread**, not a guess;
- **stamped with the environment it was measured in** — CARLA version, content-pack version and
  sha256, reference agent version, GPU;
- **bound to one smoke split**, by that split's sha256. A bundle generated for a different split
  is rejected rather than silently applied.

`make_golden.py` enforces every one of those. It also refuses to write a bundle at all if
assertions A1–A3 do not hold in every replicate, because a golden minted on a broken install
pins the breakage and makes the acceptance test certify it forever.

## Naming

```
goldens/<agent>_seed<seed>_<bundle_version>.golden.json
e.g. goldens/pdmlite_seed42_v0.9.golden.json
```

`check_acceptance.py` auto-discovers a single `*.golden.json` here. Two or more, and it stops and
asks you to name one with `--goldens` rather than guessing which is authoritative.

## Validity

A golden is valid **only** for the content-pack version it was generated against. A v1.0 asset
replacement changes the visual and geometric stimulus, so scores are not comparable even for
routes whose blueprint id did not change. The bundle records `bundle_version`,
`environment.content_pack_version` and the split sha256 so that using a stale one is a loud
error, not a quiet wrong answer.

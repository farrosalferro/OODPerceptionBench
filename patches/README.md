# `patches/` — the overlay

**Purpose:** this directory *is* the contribution. Everything OOD-PerceptionBench changes in the
simulation harness lives here as a patch against a pinned upstream commit, so that the harness
itself is never redistributed.

**What belongs here:** one `.patch` per modified or added upstream file, plus
[`UPSTREAM.txt`](UPSTREAM.txt) (the pin), [`MANIFEST.md`](MANIFEST.md) (what each patch does and
why) and [`EXCLUDED.md`](EXCLUDED.md) (what was deliberately left out).

**What does not belong here:** anything that is not a change to the pinned upstream. Our own
standalone code goes in `runner/`, `tools/` or `tests/`.

---

## Applying

Do not apply these by hand. Use [`../setup.sh`](../setup.sh), which pins the SHA, dry-runs the
whole set before touching anything, applies in filename order, and then verifies post-conditions
(the twelve scenario classes, the metrics plumbing, and that every patched file still parses).

```bash
../setup.sh --upstream-dir /path/to/carla_garage
```

## Naming and ordering

`NNN-<path with / replaced by _>.patch`. Numeric prefixes are the apply order:

| Range | Layer |
|---|---|
| `0xx` | scenario-runner core — event types, criteria, behaviours, helpers |
| `1xx`–`2xx` | scenario definitions |
| `3xx` | leaderboard — statistics, checkpointing, evaluator, agent base class |
| `4xx` | `team_code` configuration (weather determinism) |
| `9xx` | our scenarios that the canonical 475 routes do **not** use (see MANIFEST) |

The patches are file-disjoint, so the order is documentation rather than a hard dependency —
except that it keeps a partial failure readable.

## Regenerating

`../tools/dev/regenerate_patches.sh` rebuilds the whole set from a working checkout, driven by
`../tools/dev/patch_manifest.tsv`. It is a maintainer tool and requires `--source-root`.

It stages each selected file into a scratch git index seeded from the pinned upstream tree
before diffing, rather than running `git diff`. **This is not incidental.** Seven of the twenty
scenario modules the canonical route set depends on were *untracked* in the private working tree
— `git diff` cannot see them at all. A patch set built the obvious way is missing seven files
and fails at route-build time, or worse, part-way through a sweep. See
[`MANIFEST.md`](MANIFEST.md).

## If a patch stops applying

That is patch rot, and it means upstream moved. **Do not force it and do not `--fuzz` it.**
Open an issue with the failing patch names. The pinned SHA and the patch set are versioned
together and have to be updated together, followed by a re-run of the acceptance tests — a patch
that applies with fuzz can land a hunk in the wrong place and produce plausible wrong numbers.

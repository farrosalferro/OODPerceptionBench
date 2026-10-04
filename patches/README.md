# `patches/` — the overlay

Everything OOD-PerceptionBench changes in the simulation harness lives here, as patches against a
pinned upstream commit. The harness itself is never redistributed.

- one `.patch` per modified or added upstream file;
- [`UPSTREAM.txt`](UPSTREAM.txt): the pinned commit;
- [`MANIFEST.md`](MANIFEST.md): what each patch does and why;
- [`EXCLUDED.md`](EXCLUDED.md): what was left out on purpose.

Our own standalone code goes in `runner/`, `tools/` or `tests/`, not here.

## Applying

Do not apply these by hand. Use [`../setup.sh`](../setup.sh). It pins the SHA, dry-runs the whole
set first, applies in filename order, then checks the result (the twelve scenario classes, the
metrics plumbing, and that every patched file still parses).

```bash
../setup.sh --upstream-dir /path/to/carla_garage
```

## Naming and ordering

`NNN-<path with / replaced by _>.patch`. The number is the apply order:

| Range | Layer |
|---|---|
| `0xx` | scenario-runner core — event types, criteria, behaviours, helpers |
| `1xx`–`2xx` | scenario definitions |
| `3xx` | leaderboard — statistics, checkpointing, evaluator, agent base class |
| `4xx` | `team_code` configuration (weather determinism) |
| `9xx` | our scenarios that the canonical 475 routes do **not** use (see MANIFEST) |

Each file is touched by only one patch, so the order is not a hard dependency; it keeps a partial
failure readable.

## If a patch stops applying

Upstream moved. **Do not force it and do not `--fuzz` it**: a fuzzed hunk can land in the wrong
place and give plausible wrong numbers. Open an issue with the failing patch names. The pinned SHA
and the patches must be updated together, then the acceptance tests re-run.

## Regenerating (maintainers)

`../tools/dev/regenerate_patches.sh` rebuilds the set from a working checkout, driven by
`../tools/dev/patch_manifest.tsv`, and requires `--source-root`. It builds each patch from a
scratch git index seeded from the pinned upstream tree, not from `git diff`: seven of the twenty
scenario modules the routes need are new, untracked files that `git diff` cannot see. See
[`../tools/dev/README.md`](../tools/dev/README.md).

# `tools/dev/` — maintainer tools

Not part of the user-facing flow. These regenerate release artifacts from a private working
checkout that a user does not have.

| File | Purpose |
|---|---|
| `patch_manifest.tsv` | The **selection**: which files from the working tree become patches, their tier, their git state, and one line of justification each. Edit this, not the patches. |
| `regenerate_patches.sh` | Rebuilds `patches/*.patch` from `--source-root <carla_garage checkout>` against the SHA in `patches/UPSTREAM.txt`. |
| `check_notice_assets.py` | Cross-checks `NOTICE` against the private asset-licence audit: every `ship` row attributed, no `replace` row leaked. |

## Why `regenerate_patches.sh` does not use `git diff`

Because `git diff` is wrong here, silently.

Seven of the twenty scenario modules that the canonical route set depends on were **untracked**
in the working tree. `git diff` — and `git diff HEAD`, and `git format-patch` — report none of
them. A patch set built the obvious way looks complete, applies cleanly, passes review, and then
fails at route-build time for 60% of the benchmark.

So the script stages each selected path into a **scratch git index seeded from the pinned
upstream tree** (`git read-tree <base>` + `git add -f <path>`) and diffs the index against the
base. That sees tracked-modified, tracked-new, and untracked files identically.

The corresponding trap in the other direction: `statistics_manager.py` was *committed* locally,
so it is absent from `git status` and from the uncommitted diff, while being essential. Neither
"what's uncommitted" nor "what's untracked" is the right question — the right question is "what
differs from the pinned upstream", which is what the manifest answers file by file.

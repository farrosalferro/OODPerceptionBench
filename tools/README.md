# `tools/` — checks and utilities

Small standalone programs that check the repository. Nothing here is part of a benchmark run.
They run on a laptop with a clean Python 3.10, no CARLA and no GPU.

| Tool | What it does |
|---|---|
| `check_route_coverage.py` | Asserts the route set is exactly 475 / 70 / 162 / 243 with valid level dirs, and that every scenario type named in a route resolves to a class in the patched upstream. Skips cleanly if `routes/` is empty. |
| `check_no_cluster_paths.py` | Fails on any absolute private-cluster path, node name, jump host, private conda env, internal planning-document reference, or credential pattern. |
| `check_forbidden_tokens.py` | Fails if the repository names the upstream source of a non-redistributable prop. Works from salted digests in `forbidden_tokens.txt`, so the check itself names nothing. |
| `forbidden_tokens.txt` | The salted denylist the above reads. Digests only — no words. |
| `check_release_ready.py` | The pre-tag gate. **Exits non-zero while any TODO remains.** |
| `pre-push` | Git hook that runs the two leak checks before a push. Not installed by cloning. |
| `dev/` | Maintainer-only. Regenerates `patches/` from a working checkout. |

Record processing lives in `../records/` (`build_records.py`, `reproduce_table1.py`,
`verify.sh`), route generation and validation in `../routes/` (`make_manifest.py`,
`validate_routes.py`), and content-pack verification in `../assets/tools/verify_pack.py`.

## For maintainers

**Install the `pre-push` hook once per clone.** Git does not clone hooks. A leak that is pushed
cannot be recalled, so catch it before the push. `git push --no-verify` bypasses the hook; CI
still runs both checks.

```bash
ln -sf ../../tools/pre-push .git/hooks/pre-push
```

**`check_release_ready.py`** runs `routes/validate_routes.py`, `records/reconcile_with_manifest.py`,
`tests/selftest.py`, `check_no_cluster_paths.py` and `check_forbidden_tokens.py`, and checks that
the README's golden-bundle claim matches `tests/goldens/`. Each check is `PASS`, `TODO` (blocks
the tag) or `SKIP` (could not run here; the verdict drops to `READY (CONDITIONAL)` and lists it).
The two records checks that replay the paper's statistics need `--paper-repo`; without it they
are `SKIP`, never passed.

```bash
python3 tools/check_release_ready.py                      # the gate; non-zero if not ready
python3 tools/check_release_ready.py --paper-repo PATH     # + the paper-coupled records checks
python3 tools/check_release_ready.py --allow-todo "why"    # documented pre-tag override
python3 tools/check_release_ready.py --fast                # presence checks only, no subprocesses
```

`--allow-todo REASON` prints the reason, still lists every TODO and never prints `READY`. Push
and PR CI use it; a tag build must not. `--strict` is accepted as a no-op.

**`check_forbidden_tokens.py`** compares salted SHA-256 digests of normalised tokens (lowercase;
non-alphanumeric runs become word separators; words joined by `-`), so `Some Slug`, `some_slug` and `SOME-SLUG` match one entry. A hit prints a file,
line and digest prefix, never the text. This is obfuscation, not secrecy: the salt is in the
file. A missing, truncated or mis-salted denylist exits **2**, and a built-in canary token must
hit before each scan. To hash a new token without putting it in shell history:

```bash
printf '%s' 'the slug' | python3 tools/check_forbidden_tokens.py --hash-token
```

## What runs where

| Check | `overlay-setup` | `acceptance` | tag build |
|---|---|---|---|
| `setup.sh` against the pinned SHA | ✅ | — | ✅ |
| `check_route_coverage.py` | ✅ | — | ✅ |
| `check_no_cluster_paths.py` | ✅ | — | ✅ |
| `check_forbidden_tokens.py` | ✅ | — | ✅ |
| `tests/selftest.py` + smoke-split integrity | — | ✅ | ✅ |
| `routes/validate_routes.py` | — | ✅ | ✅ |
| `records/reconcile_with_manifest.py` (check 2/3) | — | ✅ | ✅ |
| `records/verify.sh` checks 1/3 and 3/3 | — | dispatch only | **manual** |
| `check_release_ready.py` | — | advisory | **blocking** |

CI cannot run two things. The paper-coupled records checks need the paper repository, which is
private until arXiv: dispatch `acceptance` with the `paper_repo` input on a runner that has it,
or run `records/verify.sh` and `check_release_ready.py --paper-repo` locally before tagging. The
A1–A4 acceptance assertions need a GPU, CARLA 0.9.15 and the content pack; CI runs only the
harness's self-tests.

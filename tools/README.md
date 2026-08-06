# `tools/` — checks and utilities

**Purpose:** small standalone programs that verify the repository or process the published
records. Nothing here is part of a benchmark run.

**What belongs here (present)**

| Tool | What it does |
|---|---|
| `check_route_coverage.py` | Asserts the route set is exactly 475 / 70 / 162 / 243 with valid level dirs, and that every scenario type named in a route resolves to a class in the patched upstream. Catches a patch set missing a scenario module. Skips cleanly if `routes/` is empty. |
| `check_no_cluster_paths.py` | Fails on any absolute private-cluster path, node name, jump host, private conda env, internal planning-document reference, or credential pattern. |
| `check_forbidden_tokens.py` | Fails if the repository names the upstream source of a non-redistributable prop. Works from salted digests in `forbidden_tokens.txt`, so the check itself names nothing. |
| `forbidden_tokens.txt` | The salted denylist the above reads. Digests only — no words. |
| `check_release_ready.py` | The pre-tag gate. Runs the repository's own verification programs, not just a file-presence sweep, and **exits non-zero while any TODO remains**. |
| `dev/` | Maintainer-only. Regenerates `patches/` from a working checkout; not part of the user flow. |

## `check_release_ready.py` — the gate

It reports each check in one of three states, and the distinction is the point:

| State | Meaning | Blocks? |
|---|---|---|
| `PASS` | verified here, now | — |
| `TODO` | a known, unmet release requirement | **yes** |
| `SKIP` | could not be attempted in this environment — a missing optional input, not a defect | no, but the verdict drops to `READY (CONDITIONAL)` and every skipped check is listed by name |

```bash
python3 tools/check_release_ready.py                      # the gate; non-zero if not ready
python3 tools/check_release_ready.py --paper-repo PATH     # + the paper-coupled records checks
python3 tools/check_release_ready.py --allow-todo "why"    # documented pre-tag override
python3 tools/check_release_ready.py --fast                # presence checks only, no subprocesses
```

`--allow-todo REASON` is the override for an intentional pre-tag state — the repository is
assembled in stages and is legitimately red for weeks. It requires a written reason, prints it in
a banner, still lists every TODO, and never prints `READY`. Push and PR CI runs under it; a tag
build must not.

> **This inverts the old behaviour, deliberately.** `--strict` used to be the flag that made the
> program block, so the *default* invocation printed a list of TODOs and exited 0 — it could not
> fail a release, which meant it was not a gate. Blocking is now the default and `--strict` is
> accepted as a no-op so existing runbooks keep working.

Beyond file presence it executes `routes/validate_routes.py`, `records/reconcile_with_manifest.py`,
`tests/selftest.py`, `check_no_cluster_paths.py` and `check_forbidden_tokens.py`, and it checks
that the README's golden-bundle claim matches what is actually in `tests/goldens/`. The two
records checks that replay the paper's own statistics pipeline need `--paper-repo`; without it
they are reported as `SKIP`, never as passed.

## `check_forbidden_tokens.py` — why it hashes

Two OOD props are missing from this release because their upstream meshes were third-party game
IP. A check that greps for their source slugs has to write those slugs down, which republishes
the association — which prop was a copy of what — in a file anyone can read, and it has to exclude
itself from its own scan, which is a hole.

So the denylist holds salted SHA-256 digests of normalised tokens. The scanner normalises the
repository's text the same way (lowercase, non-alphanumeric runs become word separators, words
joined by `-`), so `Some Slug`, `some_slug` and `SOME-SLUG` all match one entry. A hit prints a
file, a line number and a digest prefix — never the matched text, because printing it would
reproduce the leak in the CI log.

This is **obfuscation, not secrecy**, and the file says so: the salt sits next to the digests.
The property being bought is the narrow one that matters — the repository never *states* the
association, and a regression still turns CI red.

Two failure modes are handled explicitly, because a leak checker that fails open is worse than
none:

- a missing, truncated or mis-salted denylist exits **2**, not 0 — `count` is declared in the
  file and must match the number of entries;
- before every scan the checker rebuilds a synthetic canary token, plants it in a probe string
  and requires a hit. If the salt, the normaliser or the matcher were broken, the run stops
  instead of reporting a clean repository.

Adding a token, without putting it in shell history:

```bash
printf '%s' 'the slug' | python3 tools/check_forbidden_tokens.py --hash-token
```

**Where the other utilities ended up**

Record processing and route generation are *not* here — they live beside the data they produce,
because each is only meaningful with that directory's conventions in hand:

- raw result JSON → the tidy table, and the table → the paper's numbers: `../records/`
  (`build_records.py`, `reproduce_table1.py`, `verify.sh`).
- route manifest generation and the 38-check bundle validator: `../routes/`
  (`make_manifest.py`, `validate_routes.py`).
- content-pack verification against a live CARLA: `../assets/tools/verify_pack.py`.

**What does not belong here.** Anything that hardcodes a machine, and anything that needs a GPU.
Tools must run on a laptop with a clean Python 3.10 and no CARLA.

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

Two things are honestly outside CI's reach and are named as such wherever they are reported:

- **the paper-coupled records checks** — replaying the paper's own statistics pipeline needs the
  paper repository, which is private until arXiv. Dispatch `acceptance` with the `paper_repo`
  input on a runner that has it, or run `records/verify.sh` and
  `check_release_ready.py --paper-repo` locally before tagging.
- **the acceptance assertions themselves** — A1–A4 need a GPU, CARLA 0.9.15 and the installed
  content pack. CI runs the harness's self-tests, which is not the same thing and does not claim
  to be.

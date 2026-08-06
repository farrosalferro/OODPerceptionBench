# Runner status — FIRST CUT

> **Artifact version:** runner v0.9.0.dev0, for **OOD-PerceptionBench release v0.9**, which
> binds to **arXiv v1**.
>
> **Read this before trusting the runner with GPU-hours.**

A production runner for this benchmark is 1–2 weeks of work and cannot be finished without a
machine that runs CARLA. What exists now is the part that is expensive to change later — the
design decisions in `DESIGN.md` — plus a working implementation of them whose *logic* is
covered by 153 automated tests, and none of whose *simulator interaction* has ever executed.

Nothing below should be read as "tested" unless it says so explicitly.

### Seven defects found and fixed on review

Every one was found by **reading** the code — two on a first correctness pass, five more in an
independent cross-review of that pass. Each was reproduced with a failing probe before being
fixed, and each is now locked down by a regression test that fails against the pre-fix code.

None of them violates the exit contract, which is why nothing else in the suite noticed them,
and none of them changes how a route is *scored* — so none can move a published number. What
they change is whether a route gets its fair attempt, and whether two workers can quietly end
up sharing something they must not share.

**Result accounting** — all fail in the same direction: *something that is not a benchmark
result gets accounted as one*.

1. **A launch that never happened destroyed the record it was going to replace.** Both backends
   deleted the checkpoint *before* confirming the launch. A route holding a retryable record
   (`Failed - Agent crashed`, budget remaining) is planned as RUN, so it arrives at `submit()`
   with a real result on disk; a busy port — or on SLURM a refused `sbatch`, which is routine —
   then wiped it. Now taken aside and restored on every failure path.
2. **Ctrl-C spent the interrupted route's infrastructure retry budget.** With the shipped
   `infra_budget: 3`, three interrupted sweeps permanently marked an in-flight route
   `skip_exhausted` — "this route has NOT produced a benchmark result" — for a route that never
   actually failed. Interrupts now charge nothing and do not count toward worker quarantine.
3. **A failed launch charged the *record* budget instead of the infra budget.** The fix for (1)
   restores the pre-existing record, so the accounting step was then reading an *earlier*
   attempt's output as if this attempt had produced it. Infrastructure faults spent the model's
   retries, the quarantine detector never fired (a "record" reset the consecutive-failure
   counter), and once the record budget ran out the route was marked finished with the stale
   record frozen in as its answer — having never actually been retried. A failed launch is now
   infrastructure unconditionally, and exhausting the infra budget this way says so in the
   report rather than exiting quietly.
4. **The interrupt fix was only half done.** It covered the *no record* path, but killing the
   worker's process group takes CARLA down with it and the evaluator's crash handler writes a
   final `Failed - Simulation crashed` record on the way out — so the commonest shape of an
   interrupted route is a route *with* a retryable record, which was still charged. A few
   Ctrl-Cs could spend a route's real retries and then freeze that interrupt artefact in as the
   benchmark result. Interrupts now charge no budget of any kind; an *accepted* record is still
   accepted, because that route genuinely finished.

**Isolation and determinism** — all silent by construction: nothing errors when they go wrong.

5. **`agent.env` was emitted *after* the runner's own exports**, so a model config containing
   `PORT`, `TM_PORT`, `SEED` or `CUDA_VISIBLE_DEVICES` won. That is simultaneously an isolation
   hole (two workers on one simulator) and a determinism hole (a per-model seed, while the
   result filename still claims `_seed42`). Reserved names are now rejected at config load with
   the field to use instead, *and* the runner's exports are emitted last — two defences, because
   either alone is one refactor away from being lost.
6. **SLURM: `slurm.max_parallel` gated nothing**, because the supervision loop read
   `execution.workers` regardless of backend; and an out-of-range slot was wrapped with
   `pairs[worker % len(pairs)]`, which hands two *concurrently running* jobs the same RPC and
   traffic-manager ports. Backends now declare their own `concurrency`, the loop reads only
   that, and an out-of-range slot is refused rather than wrapped.
7. **`gpus:` validation deduped only the CUDA index, not the Vulkan adapter.** Two entries could
   therefore share `-graphicsadapter`, putting both CARLA servers on one physical GPU while
   their agents ran on different ones — the exact failure the two-index design exists to
   prevent. Both indices are now unique-checked.

That defects of this kind survived in code this heavily commented is the argument *for* hardware
validation, not against it. Items 5–7 in particular are the shape of bug that a real sweep
surfaces as "throughput is oddly bad" long before anyone suspects correctness.

---

## 1. What is implemented and covered by automated tests

All of these run with no GPU, no CARLA, no network and no third-party packages:

```
python -m unittest discover -s tests -t .    ->  153 tests, OK, ~39 s
```

| Area | Covered by | Notes |
|---|---|---|
| Port allocator | `test_ports.py` | Determinism; no duplicate port at 1/2/5/8/16/33/**64** workers; RPC windows never touch at the minimum legal stride; RPC/TM block overlap rejected in both directions; >65535 and privileged bases rejected; probe reports a bound port busy and an unbound one free. Satisfies the "N > physical GPU count" criterion *for the allocator*, which is hardware-independent by construction. |
| Finalization predicate | `test_results.py` | Missing / malformed / in-progress / `[0,0]`-progress / empty-records checkpoints all correctly not-final. |
| Status taxonomy | `test_results.py` | Every status observed in ~4,000 real seed-42 records is known; disposition sets are disjoint; the retry set matches the internal orchestrator exactly; `sensors were invalid` is fatal; unknown statuses are flagged rather than absorbed. |
| Resume predicate | `test_results.py`, `test_plan.py` | `skip_terminal` vs `skip_any_final` vs `none`; a crashed route is re-run under the default and skipped under the legacy mode; unfinalized is never skipped. |
| Path mirroring | `test_plan.py` | The `{scenario}/{level}/` component survives whether `--routes` points at the tree root or a category; keys, job scripts and log paths are unique across the tree; the seed is in the filename. |
| Manifest integrity | `test_plan.py` | An **edited** route XML is detected by sha256; missing and extra routes detected. |
| Budget accounting | `test_plan.py` | Persisted budgets are honoured across restarts; the infra and record budgets cannot block each other. |
| Exit contract | `test_report_and_state.py` | Exhaustive over the flag combinations: **no path with an incomplete route yields 0**; model-side failures exit 0; interrupt is never 0. |
| **Result preservation and budget attribution** | `test_result_preservation.py` (14 tests) | A launch that fails on a busy port leaves an existing record byte-identical; a launch that succeeds still starts from a clean slate; a failed launch spends the **infra** budget and never the record budget, never adopts a stale accepted record, never freezes a preserved record as "the result", and still counts toward quarantine; repeated interrupts never exhaust the infra budget, never charge the record or tickruntime budget, never quarantine a healthy worker, and leave the route planned as RUN; a route that genuinely completed before the interrupt is still accepted; genuine infra failures and genuine retryable records are both still charged. |
| Ledger | `test_report_and_state.py` | Atomic write leaves no `.tmp`; a corrupt ledger is preserved, not silently reset to zero budgets; unknown fields from a future version are ignored. |
| Config validation | `test_config_and_jobscript.py` | Every required field is genuinely required and named in the error; unknown sections and mistyped keys are errors, not silent defaults; `workers > gpus` needs explicit opt-in; **both** the CUDA index and the Vulkan adapter are unique-checked, including when one side was defaulted; `agent.env` is rejected for every runner-owned name with the field to use instead, while composable ones (`PYTHONPATH`, `LD_LIBRARY_PATH`) stay allowed; digest is stable and sensitive. |
| Job script | `test_config_and_jobscript.py` | Both CUDA **and** the Vulkan adapter are pinned; allocated ports are used verbatim; `--resume` is never passed; nothing is detached; the seed is independent of worker and port; `agent.pythonpath` wins the ordering; every runner-owned export is emitted **after** `agent.env`, and a reserved name smuggled past config validation still loses to the runner; `environment.activate` still precedes `agent.env`; `bash -n` clean, including paths containing spaces. |
| **Backend concurrency** | `test_backend_concurrency.py` (9 tests) | The loop opens exactly the *backend's* slots, never `execution.workers`, in both directions (over- and under-parallelising); SLURM concurrency is `slurm.max_parallel` with one reserved port pair per slot, all disjoint; an out-of-range slot is refused without disturbing an existing record; `max_parallel < 1` is a config error; setting `execution.workers` under SLURM warns that it is ignored. No scheduler is contacted. |
| **Supervision loop, end to end** | `test_integration_local.py` (19 tests) | Driven against a stand-in evaluator: full sweep exits 0; a re-run skips completed routes without re-executing them; a simulated interruption re-runs exactly the missing routes; the seed reaches the child identically across 3 workers; workers get disjoint, non-overlapping ports; a route that never writes a record exhausts the **infra** budget without touching the record budget and exits 1; an unfinalized checkpoint counts as infra; a retryable record is retried then accepted as the result; TickRuntime is not retried by default; a flaky route recovers; a hanging route is killed by the wall clock; `sensors were invalid` aborts the sweep at ≤1 route; a restart does not buy a fresh budget; a worker is quarantined; a busy port block is a preflight error with nothing executed; `--dry-run` writes no report; `resume.mode: none` requires `--force`; a strict-manifest mismatch refuses to run. |

Also verified by hand:

- The de-hardcoding gate — a case-insensitive recursive grep for the internal storage
  mounts, cluster hostnames, submit host and conda environment names — returns **nothing** over
  this tree. There is not a single site-specific default anywhere in it. (The gate pattern is
  deliberately not reproduced here, so that quoting it does not make this file match.)
- `--check-gpus` runs and produces a usable CUDA/Vulkan comparison. On the machine this was
  written on it surfaced a real instance of the hazard the design is built around: a host with
  **one** physical GPU enumerates **two** Vulkan adapters, index 1 being the `llvmpipe`
  software rasterizer. `vulkan == cuda` is an assumption, not a fact.
- `run_benchmark.py --help`, missing `--config` and a nonexistent config all behave (exit 2).
- The reference agent byte-compiles.

---

## 2. What is NOT done, and must be validated on real hardware

These are the acceptance criteria that no amount of local testing can discharge. **None
of them has been attempted.**

| # | Must validate | Why it cannot be faked | Risk if wrong |
|---|---|---|---|
| H1 | A real route runs end to end: CARLA boots from `carla.root`, the agent binds, criteria attach, a finalized checkpoint lands in the mirrored path | Needs a CARLA server and a GPU | The whole thing is untested plumbing |
| H2 | `--workers N` really runs N concurrent CARLA servers with no port collision, **demonstrated with N > physical GPU count** | The *allocator* is proven at N=64; whether CARLA honours the RPC port it is given under concurrency is empirical | Two routes silently share a simulator; results are garbage but plausible |
| H3 | The **Vulkan adapter mapping** actually pins the simulator to the intended GPU | Requires watching `nvidia-smi` while N routes run | Every simulator lands on one GPU: throughput collapses, nothing errors |
| H4 | Orphan reaping by RPC port finds and kills a real orphaned `CarlaUE4-Linux-Shipping` | The regex is written against the observed command line but never matched against a live process | VRAM piles up across retries until the node wedges |
| H5 | `find_free_port` really returns our port unchanged when it is free | Read from the vendored source, not observed | The evaluator wanders into the next worker's window |
| H6 | Real `Ctrl-C` mid-sweep reaps children *and their CARLA servers*, then resumes correctly | The integration test simulates interruption by deleting result files, which is **not** the same thing | Orphans + a torn view of what completed |
| H7 | A real `Failed - TickRuntime` / `Agent crashed` route flows through retry and reporting as designed | Only synthetic records have been seen | Retry accounting may misclassify real failures |
| H8 | The full 475-route set runs to completion at the expected ~0.12 GPU-h/route | — | Unknown scaling behaviour, undiscovered leaks |
| H9 | **The entire SLURM backend** | Never executed against a scheduler | Everything |
| H10 | A fresh checkout on a machine with none of the internal storage runs the smoke split from the config file alone | The frozen routes and the smoke split now exist and are wired into `configs/reference_agent.yaml`, but the end-to-end run has never been executed | The headline acceptance criterion is unmet |

**Recommended validation order** (cheapest first, each gates the next):

1. `--dry-run` against the real route tree — confirms discovery and the manifest check. Free.
2. Reference agent, `--limit 1`, `--workers 1`. Covers H1, H3 (partially), H5. ~15 min.
3. Reference agent, `--limit 8`, `--workers 4`, watching `nvidia-smi`. Covers H2, H3, H4. ~30 min.
4. `Ctrl-C` mid-run, then re-run. Covers H6. ~15 min.
5. A real model on one category (70 static routes). Covers H7, H8. ~8 GPU-h.
6. Only then the SLURM backend, one route at a time.

---

## 3. Known gaps and deferred work

| Gap | Impact | Suggested disposition |
|---|---|---|
| **Smoke split never run through this runner** | The smoke split now exists (`tests/smoke/SMOKE_SPLIT.tsv`, materialized by `tests/smoke/materialize.py`), and `configs/reference_agent.yaml` is wired to it — but the headline acceptance criterion ("a fresh checkout runs the smoke split end to end") has still never been executed, because nothing here has touched a real CARLA server. | Run it on a machine with CARLA 0.9.15 before the tag. This is the single highest-value outstanding check. |
| **No liveness probe** | A CARLA server that hangs without exiting is caught only by `execution.route_timeout_s`, so a hung route burns the full timeout. A checkpoint-mtime stall detector would cut that to minutes. | Designed, not implemented. Worth adding before a 475-route sweep. |
| **`environment.activate` is unvalidated shell** | A typo produces a confusing failure inside the job script rather than a config error. | Add a preflight that runs the activation lines plus `python -c "import carla"` once, before the sweep. Cheap and high value. |
| **No CARLA version assertion** | The content pack is hard-locked to CARLA 0.9.15 (the factory assets overwrite base content). A user on 0.9.14 gets missing props, which **fail silently with a plausible score**. | Add a preflight that reads the CARLA version and hard-fails on mismatch. This is the §3.3 failure class and belongs in the runner, not only in the acceptance harness. |
| **No blueprint-spawn assertion** | Same failure class: the runner cannot tell whether the intended OOD prop actually spawned. | That is what `tests/` is for. The runner should eventually surface that check as an opt-in flag. |
| **An unexpected exception mid-sweep exits 2 and writes no report** | `main()` catches everything not already handled and returns `EXIT_CONFIG`. Non-zero, so the exit contract holds and `state.json` is still saved — but exit 2 means "configuration or preflight error", which sends an operator debugging their config when the sweep actually died 300 routes in. The per-route results on disk are fine and a re-run resumes correctly; only the diagnosis and the report are lost. | Split the handler: before the sweep starts keep exit 2; once `runner.run()` is under way, build and write the report, then exit 1. Small, worth doing before a 475-route sweep. |
| SLURM: no array jobs | One `sbatch` per route is 475 submissions. Works, but an array job would be kinder to the scheduler. | Deferred. |
| SLURM: node-level port collisions | Ports come from the deterministic allocator, but two *independent* runs by different users on one node could still collide. | Document; consider deriving the base from the SLURM job ID. |
| No structured progress output | Progress is log lines only; long sweeps want a machine-readable heartbeat. | `state.json` is already written continuously and can be polled. Good enough for v0.9. |
| Windows / macOS | `/proc` scanning and `killpg` are Linux-only. | Out of scope; CARLA + this benchmark are Linux. |

---

## 4. One decision that needs user sign-off

**`resume.mode` defaults to `skip_terminal`, not to the internal tool's `skip_if_final`
semantics.**

The brief says to preserve `--skip_if_final`. It *is* preserved, exactly, as
`resume.mode: skip_any_final`. But it is not the default, because it has a silent
data-corruption path: interrupt a sweep while a route holds a `Failed - Agent crashed`
checkpoint, resume, and that route is accepted forever without ever being retried — even though
the same run *would* have retried it had it not been interrupted. Within-run and across-run
retry semantics disagree under the legacy default; `skip_terminal` makes them agree.

This does not change any published number (it changes whether a route is *retried*, not how it
is scored), and the legacy behaviour is one config line away. But it is a deliberate divergence
from the internal orchestrators' behaviour, and should be confirmed rather than discovered.

---

## 5. Honest summary

- **Design:** complete, and the expensive-to-change decisions are written down with rationale.
- **Implementation:** a working first cut. The supervision logic — resume, retry accounting,
  worker isolation, exit semantics, seed determinism — is implemented and genuinely exercised,
  including end to end against a stand-in evaluator.
- **Review:** two reading passes — a correctness pass and then an independent cross-review of
  that pass — found **seven** real defects (see "Seven defects found and fixed on review",
  above). Four destroyed, abandoned or froze valid results; three could have put two workers on
  one simulator, one GPU, or a different seed. Not one tripped the exit contract. That they
  existed in code this carefully commented is the argument for hardware validation, not
  against it: the classes of bug that survive review are exactly the ones only a real sweep
  surfaces. **The cross-review found more than the first pass did, which is itself a reason to
  treat the untested surfaces below as likelier to be wrong than they look.**
- **Validation:** zero hardware validation. Every interaction with CARLA is written from a
  careful reading of the vendored evaluator, not from observation.
- **Not started:** SLURM execution, the smoke split, the CARLA-version and blueprint-spawn
  preflights.

Estimated remaining effort to the acceptance bar: **3–5 days of hands-on time on a
machine with GPUs**, most of it in items H1–H6, plus whatever the first real sweep exposes.

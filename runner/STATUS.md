# Runner status — local backend hardware-validated first cut

> **Artifact version:** runner v0.9.0.dev0, for **OOD-PerceptionBench release v0.9**, which
> binds to **arXiv v1**.
>
> **Read this before trusting the runner with GPU-hours.**

A production runner for this benchmark includes scale and multi-GPU evidence that this release
does not yet have. What exists now is the part that is expensive to change later — the design
decisions in `DESIGN.md` — plus a working local implementation whose *logic* is covered by 222
automated tests and whose *simulator interaction* was exercised against CARLA 0.9.15 on
2026-08-11 and 2026-08-12. Single-route execution, two-worker one-GPU stacking, exact port
isolation, real Ctrl-C/reaping/resume, failure accounting, and a nine-route PDM-Lite golden were
observed. The table in §2 states the remaining limits; notably multi-GPU placement, the full
475-route scale, and SLURM are not validated.

Nothing below should be read as "tested" unless it says so explicitly.

### Seven defects found and fixed on review

*(Twenty-three in total, once the four later review rounds below are counted. The numbering
runs straight through: 1–7 here, 8–11 from the cross-review, 12–17 from the verification pass,
18–19 from the round that verified **that**, and 20–23 from the different-family cross-review of
**that**. Every round found real defects in the one before it, without exception; the honest
reading of the sequence is that the count is a lower bound, not a total.)*

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

### Four more, from a second (different-family) cross-review

A later cross-review by two models from other labs returned BLOCKING on nine findings; four of
them landed in the retry-accounting and settle path that items 1–4 above had already been
repaired once. They were four symptoms of one missing definition, so they were fixed as one
model rather than four patches — `DESIGN.md` §6A is that model, and it is normative.

8. **A timeout or fault kill charged the *record* budget** (the same defect as item 4, on the
   path items 2–4 did not cover: `local.py` reaches TIMEOUT/FAULT through the same `killpg` as
   Ctrl-C, but with the interrupt flag clear). A kill-manufactured `Simulation crashed` could
   spend the model's retries, reset the quarantine detector, and freeze in as the result.
9. **`decide()` ignored the infra budget whenever a final retryable record existed**, so such a
   route retried past its infrastructure limit for ever.
10. **An infra-exhausted failed launch still exited 0.** Item 3's repair correctly stopped the
    failed launch from destroying or settling the preserved record — but the *report* derived
    completeness from `rec.final` read off disk, and a preserved record is final. Completeness
    is now `final AND settled`.
11. **A documented report-time seed re-check did not exist.** Now implemented, over the result
    *tree* rather than over the planned paths (which carry the seed by construction and are
    already checked at plan time), as a warning naming the stray files.

Adversarial review of the fix itself then changed it in four further places, each recorded in
§6A.6: an abnormal end holding a crash-shaped record gets a **bounded** ambiguity budget rather
than the never-settling infra one (charging infra there would have made six *published* v0.9
result rows unreproducible at exit 1); the same for an unrecognised status; `NEVER_STARTED`
outranks the teardown flag in a strict precedence order; and the report's status breakdown was
split so a route can never be counted as both a result and a gap.

### Six more, from an independent verification pass over that repair

The repair above was then verified by a pass that had not written it, and returned six defects.
Two are the same *class* as item 11 — a rule stated in one artifact and not implemented in the
other — so the response includes one structural change and not only six fixes.

12. **The `FAULT` demotion ignored the child's exit status.** The demotion exists because the
    CARLA server shares the attempt's stderr, so a UE4 crash during *shutdown* must not condemn
    an evaluator that finished on its own. Written with no test on `rc`, it also swallowed the
    evaluator itself dying (SIGSEGV / SIGABRT / OOM) with a record already on disk, handing that
    ambiguous record to the model's own record budget — item 8 again, by a third door. The
    demotion now requires `rc == 0`. A non-zero exit *alone* is still a clean exit, because the
    vendored evaluator exits `-1` from its own crash paths.
13. **The code and the model table disagreed on the ambiguous cell.** The model as pinned sent
    `ABNORMAL_END × RETRY_RECORD` to the never-settling `infra` budget; the code charges the
    bounded `killed` axis. The code stands (it is what the table's own dead-end invariant
    requires, and `infra` there would make six published rows unreproducible), and `DESIGN.md`
    was moved to it — **and the 24-cell table is now parsed out of `DESIGN.md` by the test
    suite, which drives the settle path for every cell.** Doc/code drift in this table is a test
    failure from here on, in either direction.
14. **The fix for item 10 created a permanent, unrecoverable exit 1.** A route holding a genuine
    model record whose *infra* budget was spent by failed launches could never settle — on that
    run or any resume, because budgets are persisted — which violates the model's own invariant
    that no cell holding a final record may be a dead end. Closed without re-opening item 10
    (an unrun route still never reports complete): `attempts_infra` now counts *consecutive*
    failures, so scattered hiccups cannot gate a healthy route mid-run; and the gate has a
    lossless, first-class release, `--retry-infra-exhausted`, which every message that reports
    the gate names. `DESIGN.md` §6A.5 now carries the termination argument, with a bound.
15. **`decide(..., killed_budget=0)` was an unsafe default a caller could reach by forgetting a
    keyword** — it settled the route on a record the kill may have manufactured. Neither
    direction of a default is safe there, so the parameter is now required.
16. **Adding `retry.killed_budget` changed the config digest of every pre-existing config**,
    which made every existing output root announce "produced by a DIFFERENT configuration ...
    use a fresh output root" — advice that throws away 475 routes of finished work over a key
    nobody set. A key holding its pinned introduction value is now excluded from the digest;
    setting it to anything else still moves the digest.
17. **`unsettled_reason` collapsed two of its three values.** `rec.final` was tested before
    "was this route ever reached", so `not_reached` was unreachable for any route with a record
    on disk — precisely the routes it is for. Now decided ledger-first.

Each of the six has a regression test in `tests/test_verification_findings.py`, and **each of
those tests was run against the pre-fix tree and observed to fail first**; the recorded failure
is quoted in the test's own docstring. That is called out because four guards shipped in the
previous round were never seen red, and a test that has only ever been green does not
distinguish "the code is right" from "the test and the code were written by the same hand".

### Two more, from a third round over that verification

The verification pass was itself verified, by three independent agents. Two of them re-derived
the same two defects from different directions and then failed to refute them. Both are the
familiar shape: a rule stated correctly and implemented over the wrong quantity.

18. **The hard-death gate hung on a stderr substring, and two of the substrings were wrong.**
    Item 12 gated the *demotion* on `rc == 0`, which is right — but the demotion is only ever
    reached inside `if fault:`, and `fault` came from matching literal text against a stream.
    `"Aborted (core dumped)"` was written with a single space and **had never matched anything**,
    because a shell pads the signal name into a fixed column; SIGKILL's message is the bare word
    `Killed`, which was in no pattern at all. So an evaluator that died of SIGABRT or was taken
    by the OOM killer reached `if not fault:` and was recorded as *the process decided to stop* —
    its crash-shaped record charged to the model's own record budget and settled as the model's
    verdict, at exit 0, with no warning. Item 8 for the fourth time. Two operationally identical
    events were accounted opposite ways purely on which signal it was; under
    `configs/reference_agent.yaml`, which ships `record_budget: 1`, the wrong branch settles on
    the **first** attempt with no retry at all. Death by signal is now read from the exit status,
    where it needs no text and no locale — `rc < 0`, or the shell's `128+N` bounded above by
    `NSIG` so that the evaluator's own `sys.exit(-1)` → 255 stays a self-terminated verdict.
    The dead patterns were repaired too, because they are still consulted while an attempt is
    *running*, where there is no exit status yet.
19. **`retry.infra_budget: 0` was a total dead end** — the very thing item 14 was written to
    remove, re-created by an off-by-one rather than by a rule. The infra gate is the only one
    evaluated *before* the axis it bounds has been charged, so `0 >= 0` fired on a virgin ledger
    and every route was skipped before anything ran: exit 1, zero executions, on a completely
    healthy machine. Nothing could release it — `--retry-infra-exhausted` clears a counter that
    was never charged, and deleting the ledger does not help because the gate never consulted it.
    The value is legal, `tickruntime_budget` ships at `0` so it reads as the idiomatic "no
    retries", and the shipped config already carries a non-default `infra_budget: 1`. Both gates
    now use the guarded idiom the killed gate one screen above already used.

Two smaller ones from the same round: `--dry-run --retry-infra-exhausted` spent the recovery it
was asked to preview (the clear is now rolled back before the ledger is written), and the
exit-contract tests only ever ran on the diagonal where `final` and `settled` agree, so the
off-diagonal — a record preserved by a failed launch, which is the case the whole distinction
exists for — is now asserted directly.

### Four more, from the different-family cross-review of round three

Round three was reviewed by two models from other labs (`gpt-5.6-luna` @ xhigh via codex,
`cursor-grok-4.5-high` via cursor); cursor returned **BLOCKING**. Record:
the maintainers' review record (kept internal). All four findings were escalated to the user rather than
fixed by the agent that found them, and the user ruled on each.

20. **The demotion's discriminator was a proxy, and round three made the proxy bite.** Item 12's
    `rc == 0` stands for *"our process did not die hard"*, but the vendored evaluator's own crash
    path is `sys.exit(-1)` → status **255**, a self-terminated verdict. Item 18 then made the
    padded `Aborted` pattern match — correctly — which moved `Failed - Simulation crashed` and
    `Failed - Agent couldn't be set up` (the status family of **four of the six published v0.9
    rows**) from the model's record budget onto the ambiguity axis. The discriminator is now
    `signalled is None`, the exact test item 18 had just created. This **inverts** a round-two
    regression test; the inversion and its argument are written into that test rather than
    applied quietly.
21. **The accounting model is now versioned in the ledger** and a resume across epochs warns.
    Raised independently by both reviewers *and* by round three's own auditor. The config digest
    deliberately does not cover it — a key nobody set is not a setting they changed — but the
    §6A model changed alongside that key, and nothing compared it, so an old output root
    resumed under new rules in silence.
22. **`--dry-run` now writes nothing at all.** It was never inert: planning materialises a task
    entry per route, moves settlement bits, and the digest is replaced on load. The sharp
    consequence, which is codex's and not obvious: previewing under a changed config *erased*
    the "produced by a DIFFERENT configuration" warning the real run would have shown.
23. **The `128 + N` signal inference is documented as an assumption**, not closed — it is safe
    for the pinned evaluator (only `-1` and `0`) but not for any evaluator you configure. The
    clean fix is a `trap` in the generated job script and waits for hardware.

**What this round is actually evidence of.** Four rounds of same-family review preceded it, each
believing itself thorough; two different-family reviewers found four more in one pass, one of
them a regression the previous round had just introduced. That is the argument for the
cross-review gate being mandatory rather than advisory, and it is why the record of *this*
review exists before any of it is committed.

**None of this is hardware validation and none of it upgrades anything below.** These are logic
tests against a stand-in evaluator. In particular, "an attempt killed by the wall clock while
holding a final record" — the case the whole `killed` axis exists for — has never been observed
against real CARLA by this runner. It is reasoned from the evaluator's source, which is why the
accounting for it is bounded in both directions instead of confident in either. Item 18 sharpens
that caveat rather than lifting it: no evaluator has ever segfaulted, aborted or been OOM-killed
under this runner, the exit statuses it reasons about are read from `leaderboard_evaluator.py`
and from a shell's documented conventions rather than measured against CARLA, and the stderr
patterns it no longer relies on are precisely the ones that turned out to be wrong for three
rounds without anyone noticing. **The lesson items 18 and 20 actually teach is that logic tests
over a stand-in cannot tell you what a real process writes to a real stream — and that fixing
that blindly moves a population you did not intend to move.**

---

## 1. What is implemented and covered by automated tests

All of these run with no GPU, no CARLA, no network and no third-party packages:

```
python -m unittest discover -s tests -t .    ->  222 tests, OK, ~76 s
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
| **Attempt accounting (`DESIGN.md` §6A)** | `test_settlement_model.py` (13 tests) | One test per finding: a wall-clock kill holding a `Simulation crashed` record charges neither the record budget nor the quarantine reset and does not settle; `decide()` refuses to re-run past the infra budget when a final retryable record exists; an infra-exhausted failed launch is reported incomplete at a non-zero exit with the record still byte-identical on disk; a result file carrying a foreign `_seedN` is named in a warning and never counted. Plus the guards on the fix itself: a killed route holding a crash record still settles in *finite* attempts; `NEVER_STARTED` outranks the teardown flag and never reads the restored record (neither an `ACCEPT` nor a `FATAL`); a kill cannot fabricate `ACCEPT` or `TickRuntime`, so those still settle; a degenerate `TickRuntime` row re-planned without a ledger is still complete at exit 0; a fault pattern in the *shared* stderr does not condemn a process that exited on its own with a final record; the status breakdown and the totals cannot contradict each other. |
| **Verification-pass defects** | `test_verification_findings.py` (22 tests) | One test per defect, each first observed failing against the pre-fix tree: a fault pattern with a **non-zero** exit is still a fault (and a clean exit still demotes, and a non-zero exit alone still does not); the 24-cell normative table in `DESIGN.md` §6A.5 is parsed and every cell driven through `_settle`, checking the budget charged **and** the settlement bit in both directions; a route holding a genuine record whose infra budget is spent is reported unsettled at a non-zero exit and is then recoverable losslessly, in bounded attempts, end to end through `main()`; scattered infra failures no longer accumulate into a gate, while an ambiguous kill still clears nothing; `decide()` refuses to run without `killed_budget`; a key holding its introduction default does not move the config digest while any real change still does; `unsettled_reason` distinguishes all three of its cases. Plus the trap, re-checked on the new path — at unit level (a degenerate `TickRuntime` row and a bare-`Failed` row both still settle at exit 0 when the attempt ends abnormally) and end to end through `main()`, against a stand-in evaluator that segfaults into the stderr it shares with the runner: a three-route `TickRuntime` sweep still exits 0 with no retries, and a **self-terminated** crash record (exit 255 — the evaluator's own `sys.exit(-1)`) is charged to the MODEL's record budget rather than to the ambiguity axis, because a process that terminated itself did not die hard. Of those last two, only the `TickRuntime` sweep is a guard. **This row asserted the opposite until the 2026-08-09 review:** round four inverted that test (the demotion keys on `signalled is None`, not `rc == 0`), the inversion was written into the test's own docstring, and it was never propagated here — doc/code drift of exactly the kind §6A exists to end. |
| **Hard death and the infra-zero dead end** | `test_hard_death_and_infra_zero.py` (27 tests) | Round three, one test per hunk, all red first: an evaluator killed by SIGABRT or by the OOM killer — neither of which any stderr pattern matches — is a fault and not a clean exit, so its record goes to the bounded ambiguity axis and not the model's; a signalled job script (negative `rc`) likewise; `sys.exit(-1)` → 255 is still a self-terminated verdict; the shell's column-padded `Aborted` line matches a pattern that had never matched anything, while a bare `Killed` deliberately still does not; `retry.infra_budget: 0` runs the route instead of gating every one of them before a single attempt, and one real infra failure at that budget still gates and is still releasable; `--dry-run --retry-infra-exhausted` no longer spends the recovery it was asked to preview. Guards: the shared-stderr demotion still applies to a clean exit, the gate is unchanged for every positive budget × spend, and a degenerate `TickRuntime` row whose process is **signalled** still exits 0. Round four adds: `--dry-run` leaves an existing ledger byte-identical, creates none on a fresh tree, and does not swallow the changed-configuration warning; a ledger written under an earlier accounting epoch warns on resume while an ordinary resume stays quiet. |
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

## 2. Hardware-validation criteria and current evidence

These criteria require observation rather than a stand-in evaluator. Dates and states below are
the hardware evidence as of 2026-08-12. **CLOSED** means the stated criterion was observed;
**PARTIAL** and **OPEN** name exactly what remains.

| # | State | Observed evidence | Remaining limit or risk |
|---|---|---|---|
| H1 | **CLOSED — 2026-08-11** | A real route reached `Completed`, DS 50.0, `duration_game` 41.85 s, attached criteria and populated `ttr_dar`; its finalized checkpoint and reference-agent log landed in the mirrored paths. | This proves plumbing, not useful model quality. |
| H2 | **CLOSED — 2026-08-11** | Two workers ran eight real routes on one physical GPU twice. Both CARLA processes were live together; worker 0 exclusively owned RPC 20000–20002/TM 30000 and worker 1 owned 20010–20012/TM 30010. | Larger worker counts and multi-node execution are unmeasured. |
| H3 | **OPEN / one-GPU partial — 2026-08-11** | On the one-GPU host, both agent and simulator used CUDA 0/Vulkan 0 and both CARLA commands carried `-graphicsadapter=0`. | Requires at least two physical GPUs to prove distinct mappings do not collapse onto adapter 0. |
| H4 | **CLOSED — 2026-08-11/12** | Normal teardown and a real mid-sweep Ctrl-C reaped both evaluator/CARLA trees. No process or assigned listener survived before resume. | A brief self-clearing busy port was measured separately; refusing that dirty launch was correct. |
| H5 | **CLOSED — 2026-08-11** | Live listener and generated-job observations showed the evaluator retained each configured free RPC/TM base exactly; no silent relocation crossed a worker window. | Only the local backend was measured. |
| H6 | **CLOSED — 2026-08-12** | Ctrl-C exited 3 after writing state/report; interrupted routes charged no retry axis and stayed unfinished. Resume ran exactly the unfinished routes, then a third invocation launched nothing. | None for the measured local two-worker case. |
| H7 | **CLOSED — 2026-08-11/12** | Three real `Failed - TickRuntime` records were settled and reported complete at the configured zero retry budget. Eight deliberate setup failures each charged exactly one record attempt; rerun changed no counters and still exited 0 with all routes settled. | Hard-death/fault-pattern cases were not induced on hardware. |
| H8 | **OPEN** | The largest run was nine smoke routes. Eight-route constant-velocity sweeps measured 0.044–0.079 physical GPU-hours/route. | The full 475-route run and the ~0.12 GPU-h/route figure for an inference model remain unvalidated. |
| H9 | **OPEN — known broken** | No scheduler run was attempted. Stand-in-scheduler review already proves fatal defects described below. | Do not use `execution.backend: slurm`. |
| H10 | **PARTIAL — 2026-08-11/12** | A fresh GitHub clone ran setup twice (26/26 patches, idempotent), 222 runner tests, a strict 475-route dry run, real smoke routes, and the nine-route PDM-Lite golden/acceptance flow with every path supplied by config. | The host still had the maintainers' internal mounts available; the stronger “those mounts do not exist” portability proof must be repeated externally. |

> ### ⚠ The SLURM backend is BROKEN. Do not use it.
>
> H9 above said "never executed against a scheduler", which was true and not the whole truth. A
> review on 2026-08-09 drove `SlurmBackend` against a stand-in scheduler and found defects that
> need no cluster to demonstrate. Two are fatal on their own:
>
> 1. **`submit()` never creates the `results/` directory** its own `--checkpoint` argument points
>    into. `task.mkdirs()` is called only from `jobscript.write()`, which the SLURM path does not
>    use — it calls `jobscript.render()` directly. On a fresh output root **every** job dies at
>    its first checkpoint write and the sweep produces zero results while blaming the cluster.
>    No test catches it because the stand-in evaluator creates the directory itself
>    (`tests/test_integration_local.py:55`); the real `statistics_manager.py` contains no `mkdir`.
> 2. **A job killed by a signal is classified `EXITED`.** `_sacct_state` requests `State` and
>    never `ExitCode`, and `FAILED` is not in the fault list — so OOM kills, SIGSEGV and SIGABRT
>    arrive as CLEAN_EXIT and any crash-shaped record they left is charged to the **model's**
>    record budget and published as the model's verdict at exit 0. This is cross-review finding 2,
>    the defect four review rounds were spent closing on the local backend, entirely alive here.
>    **The same agent, on the same routes, can settle on a different axis and a different final
>    status depending on which backend ran it.**
>
> Six further majors were confirmed: `route_timeout_s` is measured from `sbatch` submission so
> queue time counts as route runtime (a job that never started is cancelled, charged to infra,
> and the checkpoint it was going to replace is already gone); every non-terminal `sacct` state
> including `RUNNING` reads as `EXITED`, so one transient `squeue` failure settles a live job and
> submits a second for the same route, ports and checkpoint path; `scancel` is asynchronous but
> settlement does not wait for it; a successful `sbatch` whose output does not end in digits is
> recorded as a launch failure while the job really runs, unsupervised and outside `shutdown()`;
> worker quarantine treats SLURM slot indices as machines, so one cluster-wide transient retires
> every slot at exit 4; and the job script hard-codes `CUDA_VISIBLE_DEVICES=0` / `--gpu-rank 0`,
> overriding whatever the scheduler allocated, with the config's `gpus:` list silently ignored.
>
> Full record kept internally by the maintainers. **Until these are fixed, `execution.backend:
> slurm` should be treated as unimplemented.** The local backend is unaffected — every one of
> these lives in `slurm.py` or in a path only it takes.

### Hardware-validation measurement notes

All observations above came from one **single-GPU** host (1× RTX 3090, driver 580.82.09,
CARLA 0.9.15). The PDM-Lite acceptance bundle was generated on 2026-08-12 from three forced,
sequential, one-worker replicates in separate roots. All nine routes completed at DS 100.0 in
all three replicates; maximum spread was 0.0 and the bundle tolerance is ±1.0 DS. Removing one
shipped static asset and rerunning its route produced a plausible `Completed`, DS 100.0 result
with a Tesla fallback, which A1 correctly rejected. The active defects and decisions found by
these runs are consolidated in [`../docs/HARDWARE_VALIDATION_ISSUES.md`](../docs/HARDWARE_VALIDATION_ISSUES.md).

**Measured 2026-08-12, and it changed a shipped default.** At the previously-shipped
`infra_budget: 1`, a worker's own RPC port was still occupied after the reaper ran *and* after
`post_kill_cooldown_s` elapsed. Refusing to launch on a dirty port is correct; exhausting the
whole infrastructure budget on that one self-clearing event is not. The route was left unsettled,
the sweep exited 1, and recovery needed a manual `--retry-infra-exhausted`. Frequency was about
1 launch in 30 — a 475-route sweep would meet it a dozen times. `configs/reference_agent.yaml`
now uses the schema defaults (3/3); see the comment there for the reasoning.

Throughput, for planning only: **0.044–0.079 physical GPU-hours per route** across the two
sweeps. That is *below* the 0.12 planning figure, but it was measured with the constant-velocity
reference agent, which does no inference — it does **not** confirm the figure for a real model,
and should not be quoted as if it did.

**Remaining validation order** (cheapest first, each gates the next):

1. Repeat the nine-route acceptance run on a host where the maintainers' internal mounts do not
   exist. Closes the remaining H10 wording.
2. Repeat the two-worker live GPU observation on a host with at least two physical GPUs. Closes
   H3 if agents and simulators spread together.
3. Run a real inference model on one category, then the full 475-route set if stable. Measures
   the remaining H8 scale and throughput claim.
4. Do not attempt SLURM until its known defects are deliberately repaired and reviewed.

---

## 3. Known gaps and deferred work

| Gap | Impact | Suggested disposition |
|---|---|---|
| **Fresh-host portability is only partially closed** | The fresh GitHub clone ran real smoke routes and the golden flow, but on a host where internal mounts still existed. Config paths were explicit and no internal default was observed. | Repeat the nine-route flow on a genuinely external host; tracked as HV-09. |
| **No liveness probe** | A CARLA server that hangs without exiting is caught only by `execution.route_timeout_s`, so a hung route burns the full timeout. A checkpoint-mtime stall detector would cut that to minutes. | Designed, not implemented. Worth adding before a 475-route sweep. |
| **`environment.activate` does not prove interpreter selection** | H5 measured bare `python3` resolving to system Python even after activation, leading to no-record retries and quarantine. | Decide on exact-interpreter documentation or a dependency preflight; tracked as HV-01. |
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
- **Validation:** the local backend has real CARLA evidence for single-route plumbing,
  two-worker one-GPU stacking and ports, reaping/interrupt/resume, real settled failures, and the
  nine-route PDM-Lite acceptance bundle. The exact boundaries are the §2 table, not a general
  production claim.
- **Not started or still open:** multi-GPU placement, the full 475-route scale run, a genuinely
  external-host H10 repeat, and all SLURM execution. CARLA-version and blueprint-spawn runner
  preflights remain design gaps; the separate acceptance probe has been exercised live.

The next decisions are listed in
[`docs/HARDWARE_VALIDATION_ISSUES.md`](../docs/HARDWARE_VALIDATION_ISSUES.md); this consolidation
does not silently turn any of them into implementation work.

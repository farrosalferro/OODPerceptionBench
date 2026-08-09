# Exhaustive review — runner/ at `3a7a0c9` — 2026-08-08/09

**Asked for:** *"It seems that you have fixed the errors from the previous session. To be more
sure about this, let's do one more round of cross-review. Make the review exhaustive."*

Four independent efforts, deliberately chosen so that each covers something the others
structurally cannot.

| # | Effort | Reviewers / method | Verdict |
|---|---|---|---|
| A | Cross-review of the change set `c67a955..3a7a0c9` | codex `gpt-5.6-luna` @xhigh, cursor `grok-4.5-high` | codex **BLOCKING**, cursor concerns — 5 findings |
| B | Cross-review of the **whole runner package** as complete files | same two | **both BLOCKING** — 13 findings |
| C | Ten-dimension sweep + adversarial refutation | in-family agents, each finding refuted by running code | **10/10 dimensions completed** across two runs; SLURM **BROKEN**, doc-parity **BROKEN**, THE TRAP clean |
| D | Mutation testing of the 219-test suite | 48 mutants, one load-bearing rule reverted each | **37 killed / 11 survived** |

---

## The question that was actually asked, answered first

**Yes — every defect from the five previous rounds is genuinely fixed, and defended.**

That is not "the tests pass", which proves little about a suite grown alongside its own repairs.
It is effort **D**: each fix was re-broken in a scratch copy and the suite re-run. Every one goes
red.

| rule re-broken | result |
|---|---|
| demotion back to `rc == 0` (round 4) | **killed** |
| hard death from the stream only / the status only (round 3) | **killed** |
| `describe_exit_signal` calls 255 a signal — the `NSIG` bound | **killed** |
| both infra gates lose the charged guard (round 3) | **killed** |
| a ledger with no epoch reads as the current epoch (round 4) | **killed** |
| `--dry-run` saves the ledger again (round 5) | **killed** |
| `complete` drops `settled` (cross-review finding 6) | **killed** |
| `TIMEOUT` / `KILLED` no longer abnormal (finding 2) | **killed** |
| `attempts_infra` reverts to a lifetime tally (round 3 dead end) | **killed** |
| `unsettled_reason` order (round 2 minor 6) | **killed** |
| `skip_terminal` behaves as `skip_any_final` | **killed** |

**The exhaustive part then found a new layer.** Widening past the diff was the whole point, and
it is where nearly everything below came from: five rounds had reviewed *changes*, so ~2/3 of the
package — `slurm.py` above all — had never been read by a reviewer in its own right.

---

## 1. The SLURM backend is BROKEN — two critical, six major, all confirmed

Dimension verdict: **BROKEN**. Both critical findings were reproduced by driving the real
`SlurmBackend` against a stand-in scheduler; neither needs a cluster.

**C1 — `submit()` never creates the `results/` directory** its own `--checkpoint` points into.
`task.mkdirs()` is called only from `jobscript.write()`; the SLURM path calls `jobscript.render()`
directly. On a fresh output root every job dies at its first checkpoint write and the sweep
produces zero results while blaming the cluster.

> **Why no test caught it, and this is the finding behind the finding:** the stand-in evaluator
> creates the directory itself (`tests/test_integration_local.py:55`,
> `Path(args.checkpoint).parent.mkdir(parents=True, exist_ok=True)`). The real
> `statistics_manager.py` contains no `mkdir` at all. **The fixture is more forgiving than
> reality.** This is the same class as round 3's seventeen-spaces defect — a stand-in that
> writes whatever the test author told it to — and it is now the second confirmed instance.

**C2 — a signal-killed job is classified `EXITED`.** `_sacct_state` requests `State` and never
`ExitCode`; `FAILED` is not in the fault list. So OOM kills, SIGSEGV and SIGABRT arrive as
CLEAN_EXIT and a crash-shaped record they left is charged to the **model's** record budget and
published as its verdict at exit 0. This is cross-review finding 2 — the defect four rounds were
spent closing on the local backend — entirely alive here. **The same agent on the same routes can
settle on a different axis and a different final status depending on the backend.**

Six further majors, all confirmed: `route_timeout_s` measured from `sbatch` submission, so queue
time counts as route runtime and a never-started job is cancelled, charged to infra, with the
aside checkpoint already discarded; every non-terminal `sacct` state including `RUNNING` reads as
`EXITED`, so one transient `squeue` failure settles a live job and submits a second for the same
route/ports/checkpoint; `scancel` is asynchronous but settlement does not wait; a successful
`sbatch` whose output does not end in digits is recorded LAUNCH_FAILED while the job really runs
unsupervised and outside `shutdown()`; quarantine treats slot indices as machines, so one
cluster-wide transient retires every slot at exit 4; and the job script hard-codes
`CUDA_VISIBLE_DEVICES=0` / `--gpu-rank 0`, overriding the scheduler, with `gpus:` silently ignored.

**Action taken:** `STATUS.md` now carries a "SLURM is BROKEN, do not use it" box. No `slurm.py`
code was changed — see *What was and was not fixed*.

---

## 2. Confirmed defects on the local path

Each was reproduced by running the code. Grouped by what an operator would see.

### Silently publishes the wrong answer

- **A preserved `ACCEPT` is re-adopted after a failed forced re-run.** `--resume-mode none
  --force` clears `finished`; if that run then fails before launching, the record is restored and
  correctly reported unsettled — but the next normal resume calls `should_skip_on_resume()`, sees
  a final ACCEPT and returns `SKIP_DONE settled=True`. The sweep exits 0 on the answer the
  operator asked to replace. *(codex pass B, confirmed; corroborated by surviving mutant
  `M07_no_unsettle_on_replan` — deleting `st.finished = False` changes nothing in the suite.
  Reproduced independently by me.)*
- **A result file rewritten after settlement is republished at exit 0**, silently: the report
  re-reads disk but never compares against the ledger's `last_status`, which it already holds.
- **`Decision.FATAL` never clears `TaskState.finished`,** and the planning-loop `break` skips
  every later route — so the report credits the run with settled results it never produced,
  including the fatal route's own `Failed - Agent's sensors were invalid`, counted in
  `totals.by_status` as a benchmark result.
- **Two runners on one output root** each drive every route and each report a complete sweep at
  exit 0, having deleted and overwritten each other's result files. There is no lock.

### Loses a real benchmark result

- **A `record`-axis retry destroys the model verdict it just produced.** `_copy_record_aside` is
  called only on the `killed` axis. If the retry then writes nothing, the route ends with no
  record, no forensic copy, and `unsettled_reason: no_record` — the runner loses a result and
  then denies it existed. *(Corroborated by surviving mutant `M15_no_record_copy_aside`.)*
- **One stale `Failed - Agent's sensors were invalid` record aborts every future run** of that
  output root, in all three resume modes, before any route runs, diagnosing an agent this run
  never executed, with no named recovery.
- **`RunState.save()` uses a fixed temp filename**, so two savers on one root write the same temp
  inode, leaving `state.json` invalid at rest — which the next load discards along with every
  budget and settlement bit.

### Reports a false gap

- **The epoch-1 → epoch-2 migration re-gates routes.** Old ledgers stored `attempts_infra` as a
  *lifetime* tally; epoch 2 reads it as a *streak* **and** applies the infra gate on the
  final-record branch, which epoch 1 did not. A route the old runner would have re-run is now
  `SKIP_EXHAUSTED settled=False`. **Both pass-A reviewers found this independently, and I
  reproduced it.** The epoch warning I added narrates only the killed/record split and never
  mentions it; `_aged_ledger` never builds the case. *This is a defect in round 4's fix for a
  round-3 finding.*
- **A worker's own RPC port sits in `TIME_WAIT` ~62 s after every attempt**, and the pre-launch
  probe charges that to the route's infra budget *and* the worker's quarantine counter — a
  healthy machine reported as a wedged GPU, advancing one route per invocation.
- **`--resume-mode none --force` runs against a ledger whose counters are already spent,** so
  `_settle`'s gates fire on the first attempt: the re-run gets zero retries and can publish a
  worse status at exit 0.

### Ledger / dry-run

- **A run that does no work still writes the ledger**, permanently erasing both the
  `config_changed()` and accounting-epoch warnings. Round 4's `--dry-run` fix was keyed on the
  flag, not on the invariant.
- **`--dry-run` does still write to the ledger path:** `load_or_create` renames a corrupt ledger
  aside before any dry-run branch, and a dry run writes no report, so the "previous ledger was
  unreadable" note is consumed by the preview and never reaches anyone. *(Third consecutive round
  with a dry-run finding.)*

---

## 2b. THE TRAP is clean — the one unambiguously good result

The dimension that audited it exhaustively — every `{TickRuntime, bare Failed, Agent couldn't be
set up, Agent crashed, Simulation crashed}` × `{exit 0, exit 1, exit 255, SIGABRT, SIGKILL,
wall-clock timeout, launch failure}` × `{fresh, resume, resume without a ledger,
--retry-infra-exhausted}` combination it could construct — returned **no critical and no major
findings**. A model that legitimately fails every route still reports complete at exit 0.

That matters more than any single fix below. It is the property four separate rounds nearly
broke, it is the one that would silently invalidate a published baseline (ADMLP is exactly this
shape), and it is now the most heavily attacked surface in the component. Its three minors are
about the *tests*, not the behaviour: chiefly that the suite is not port-hermetic, so an
intermittently red `TestTheTrapUnderHardDeath` is indistinguishable from a real regression.

---

## 2c. `records/` — `verify.sh` is much weaker than it reads, and I over-relied on it

**This is the only component in this review that touches published paper data, and the finding
is about the checking harness, not (so far as anything here shows) about the shipped numbers.**
Table 1 does still regenerate exactly and every §1.6 headline figure matches. What the sweep
established is that `ALL CHECKS PASSED` would also be printed over several classes of corruption:

- **A fabricated 19th model with 475 perfect-score rows passes all four checks.** Check 2 never
  looks at models outside its own hardcoded list, and check 3 adapts its expectation to whatever
  cohort it finds. The cohort is defined by *two independently duplicated 18-name lists*
  (`build_records.ALL_MODELS` and `validate_against_frozen.MODELS`, in different orders); they
  agree today. Adding a model to one and not the other is exactly the edit v1.0 requires.
- **A missing metric column is detected, printed, and does not fail the check** —
  `validate_against_frozen` exits 0 still claiming the records reproduce the frozen CSVs *on
  every metric column*. Four of the thirteen `METRIC_COLS` sit outside `ANALYSIS_COLUMNS`, so for
  those this is the only thing between a dropped published column and a green release.
- **20 of the 64 shipped columns are read by no check at all** — the entire Success-Rate block,
  all 13 raw infraction counts, and `ood_agent_hit`. `records/README.md` §3 says these were
  "cross-validated independently … 8,550 rows compared, 0 mismatches".
- **The shipped parquet silently drops 232 values the CSV carries.** `reaction_value` /
  `reaction_threshold` are typed NUMERIC but hold categorical strings for lane-change reactions,
  and `to_numeric(errors="coerce")` nulls them. No check compares the two artifacts. The README
  sells the parquet as "the same data with a real dtype schema".

**My own error, recorded deliberately:** twice this session I ran `records/verify.sh`, reported
"ALL CHECKS PASSED — all four stages", and offered it as evidence the published artifact was
unaffected. The command output was accurate; the weight I put on it was not. It is a strong
check of *Table 1's regeneration path* and a weak check of *the artifact as a whole*, and I did
not distinguish those.

---

## 2d. The in-flight fault branch kills healthy routes

`LocalBackend.poll`'s still-running branch applies **no** shared-stream demotion and **no** record
guard — the protections rounds 3–5 built for the post-exit path were never extended to it. One
fault-shaped substring in a stream the runner's own documentation calls untrustworthy kills a
healthy route, charges infra, and advances worker quarantine.

The realistic scenario is a Tier-B user, which is the whole audience for this runner: PyTorch
prints `DataLoader worker (pid N) is killed by signal: Bus error.` once per route on a box with
small `/dev/shm`. Under the shipped `configs/reference_agent.yaml` (`infra_budget: 1`,
`worker_quarantine_after: 2`) that is two routes to quarantine the only worker and abort the
sweep at **exit 4, "no usable GPU"**, on hardware that is fine.

Corroborated by surviving mutant `M25` — deleting the in-flight kill entirely breaks no test.
After round 4 made the post-exit path exit-status-driven, this branch is the *only* remaining
justification for `FAULT_PATTERNS` existing, and it is both untested and unguarded.

---

## 3. Two of my own claims were overstated

Recorded because the point of this file is that it is not a summary written by the implementer.

- **`TestPatternIndependence` asserts less than its docstring claims.** I described it last
  session as "the invariant that would have caught round three's mistake". It asserts only
  `attempts_killed == 0` per cell — never the full `budgets()` vector, never equality across
  `stderr_text` for fixed `(status, rc)`. A regression that mis-routed into `infra`, or failed to
  charge `record`, still passes it. *(cursor pass A, confirmed.)*
- **The machine-checked §6A.5 table does not check `requeue` on `sets_finished: no` rows.** A bug
  returning `requeue=False` on the first infra charge, or `True` on TORN_DOWN, leaves the table
  green — and within-run retry count is part of the termination argument the table is supposed to
  pin. *(cursor pass A, confirmed.)*

**And three doc/code drifts, all introduced by me in rounds 3–4 — the exact defect class §6A
exists to end, committed inside the section that ends it:**

1. `local.py` and `DESIGN.md` §6A.2 both still asserted *"all three conditions are load-bearing,
   and `rc == 0` is the one that was missing"* — two paragraphs after §6A.2 supersedes `rc == 0`.
2. `STATUS.md`'s coverage row described the round-4-**inverted** test as still asserting the
   opposite of what it now asserts. The inversion was written into the test's docstring and never
   propagated.
3. `README.md` called `infra_budget` "one off from the others" in a sentence whose own example
   shows it is not.

---

## Mutation testing — 11 survivors

37/48 killed. Every surviving mutant is a rule **nothing in the suite defends**:

`M07` drop `st.finished = False` on re-plan *(a real defect — see §2)* · `M15` ambiguous record
not copied aside *(real — see §2)* · `M25` in-flight fault pattern no longer kills *(after round
4 this branch is the **only** remaining justification for `FAULT_PATTERNS` existing, and it is
untested)* · `M36` `assert_seed_consistency` never raises *(the plan-time half of the seed guard)*
· `M29` ledger write not atomic · `M11` `_clear_infra_debt` stops resetting the quarantine streak
· `M14` tickruntime branch stops clearing the infra debt · `M26` `clear_infra_exhaustion` loses
its `>0` guard · `M38` retries go to the head of the queue *(starvation)* · `M46` report drops
the global interrupted flag · `M01` `complete` drops `final` *(benign: `settled` implies `final`
in practice — redundancy, not a hole)*.

---

## Coverage — what this review did NOT cover

Stated because "exhaustive" is a claim, and an unqualified one here would be false.

- **Effort C completed 10 of 10 dimensions** across two runs (the first lost four to a session
  limit; the resume recovered them). **What did not complete: the `testquality` sweep and the
  completeness critic**, plus roughly half the adversarial refuters, all to session limits.
  - The `testquality` gap is largely covered by effort **D**, which is the empirical form of the
    same question — mutation testing answers "do the tests defend these rules" by measurement
    rather than by reading. Its 11 survivors are in this file.
  - The **completeness critic never ran**, so nothing systematically asked what all ten
    dimensions missed *between* them. Seams between dimensions are therefore unexamined, and the
    repo-root `tests/` acceptance harness, `setup.sh`, the patches and the classifier notebooks
    were never in any dimension's scope.
  - Unrefuted findings are marked as such rather than promoted; where a refuter did run it is
    noted. **Eight of nine refuters that completed returned CONFIRMED**; the single REFUTED was
    an out-of-scope deferred item.
- **No hardware.** Unchanged and unchangeable here: `STATUS.md` §2, H1–H10.
- Multi-worker interleaving, real signal delivery into a live sweep, `repetitions > 1`, `--limit`
  against settlement, and crash-consistency of the ledger were all reasoned about but not
  executed. The settlement auditor listed these itself.
- The five findings deferred by earlier user decision were out of scope and are not re-reported.

---

## What was and was not fixed

**Fixed** — documentation only, because it was *false* and cannot change a produced number:
the three drifts in §3, and a new `STATUS.md` box stating that SLURM is broken rather than merely
unvalidated. Suite still 219 passed / 87 subtests.

**Not fixed — deliberately.** Everything in §1 and §2 changes behaviour, and several are
model-level decisions. Six rounds have now established the pattern beyond argument: *every*
confident repair pass in this component has contained the next round's defect, including round
4's fix for a round-3 finding, which is in §2 above. Fixing eighteen interacting defects blind,
in a component with zero hardware validation, is how the seventh round gets its material.
Escalated to the user as a set, grouped by decision rather than by severity.

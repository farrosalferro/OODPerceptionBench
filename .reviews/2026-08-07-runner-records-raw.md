# Cross-provider review

Reviewed by codex: `gpt-5.6-luna` (effort xhigh), cursor: `cursor-grok-4.5-high`

- codex: verdict `blocking` — The records path is careful about the shipped artifact, but the runner can reuse stale results or mis-handle SLURM jobs while still reporting success. The biggest numerical risk is cache reuse after model/checkpoint/route changes, compounded by scheduler failures that can cause duplicate last-writer-wins outputs.
- cursor: verdict `blocking` — records/ faithfully regenerates the paper table under a frozen-CSV lock; runner/ still has a settle-path hole that can freeze kill-manufactured Simulation crashed records as benchmark results and exit 0. Biggest risk to third-party numbers is wall-clock timeout/fault kills charging the record budget and defeating quarantine—the same failure class already fixed for Ctrl-C.

## Intent drift

- [codex] requested: Add a note to use the cross-review skill by the end of W2, W5, and W8.
  - implemented: The diff documents that an independent cross-review happened in STATUS.md, but contains no W2/W5/W8 milestone note or cross-review-skill instruction.
  - impact: The requested review-process reminder is absent, so the release does not preserve the mandated future checkpoints. This does not directly change numerical outputs.
- [cursor] requested: Preserve internal orchestrator resume behavior (brief/STATUS: preserve --skip_if_final).
  - implemented: resume.mode defaults to skip_terminal; skip_any_final is opt-in. Deliberate divergence, flagged for sign-off in STATUS.md §4.
  - impact: Does not change scoring of a finished record, but changes which crash checkpoints third parties keep vs retry after interrupt/resume—can change published route outcomes versus internal sweeps unless users set skip_any_final.

## Flagged by both reviewers

### Resume cache is not invalidated by model, checkpoint, or route-content changes

- source: codex | category: state_determinism | severity: high | confidence: confirmed
- changes numbers: **True**
- location: runner/oodbench/plan.py; runner/run_benchmark.py:runner/oodbench/plan.py:51-57, 280-282; runner/run_benchmark.py:527-536
- evidence: Route identity is only `f"{self.rel_dir.as_posix()}/{self.stem}_seed{self.seed}"`, and finalized records immediately return `Decision.SKIP_DONE`. A changed configuration is only logged as `"Resuming will mix two sets of settings..."`; execution continues.
- why wrong: Changing the agent implementation or checkpoint contents at the same path does not change the config digest, and even a changed config only produces a warning. Existing final records are then skipped and the run can exit 0 while claiming results for the new model/config; edited routes are similarly reusable when manifest strictness is off.
- suggested fix: Include content hashes for the agent, checkpoint/config files, evaluator/CARLA build, and route manifest in the run identity, and refuse resume or require explicit re-evaluation when that identity changes.

### Timeout/fault kills charge record budget when crash handler writes Simulation crashed

- source: cursor | category: state_determinism | severity: high | confidence: likely
- changes numbers: **True**
- location: runner/run_benchmark.py:325-447
- evidence: produced_record = record.final and not launch_failed
...
# interrupt-only guard:
if interrupted:
    ...  # no budget charged
    return False
# else RETRY_RECORD / UNKNOWN path increments attempts_record and may set st.finished = True

# local.py kill path (same killpg as Ctrl-C):
self.kill(attempt, reason)
attempt.outcome = AttemptOutcome.FAULT if fault else AttemptOutcome.TIMEOUT
- why wrong: DESIGN/STATUS state wall-clock timeout is infrastructure, and the interrupt fix documents that killpg makes the evaluator write a final Failed - Simulation crashed record. Timeout and fault kills use that same terminate_process_tree path but pass interrupted=False, so the manufactured crash is treated as a real model outcome: record budget is spent, consecutive_infra is reset (quarantine never fires), and after record_budget the interrupt artifact is accepted as the route result. Integration tests only hang without writing a record, so they miss this.
- suggested fix: Treat AttemptOutcome.TIMEOUT / FAULT / KILLED like interrupt when the on-disk final status is in RETRY_STATUSES (especially Simulation crashed): charge infra (or charge nothing), do not reset quarantine on that record, and never mark finished from a kill-manufactured retryable status. Add a unit probe that plants Simulation crashed under TIMEOUT and asserts record budget unchanged.

### Runner reports hard-coded release metadata instead of the configured release

- source: codex | category: config_drift | severity: medium | confidence: confirmed
- changes numbers: **True**
- location: runner/oodbench/config.py; runner/oodbench/report.py:runner/oodbench/config.py:426-430; runner/oodbench/report.py:119-123
- evidence: Configuration accepts any `benchmark.release` and `benchmark.arxiv_version` values, while report serialization always emits `"release": BENCHMARK_RELEASE` and `"arxiv_version": ARXIV_VERSION`.
- why wrong: A config targeting another release can be logged as that release during execution but produce a report stamped v0.9/arXiv v1. This misbinds the written result artifact to the wrong benchmark protocol.
- suggested fix: Validate configured release identifiers against the runner target and serialize the validated config values into the report; reject incompatible values rather than silently substituting constants.

### Infra-exhausted failed launches still yield exit 0 via preserved retryable record

- source: cursor | category: state_determinism | severity: medium | confidence: confirmed
- changes numbers: **True**
- location: runner/run_benchmark.py:362-374
- evidence: if record.final:
    msg = (... "EARLIER attempt; it was preserved, not refreshed.")
    ...
    return False  # st.finished stays False

# report.py RouteOutcome.complete == rec.final from disk
# exit_code: if self.incomplete: EXIT_PARTIAL else EXIT_OK
- why wrong: After repeated LAUNCH_FAILED with a preserved Failed - Agent crashed (or similar) on disk, settle warns and stops re-queueing, but report completeness is solely final-on-disk. The sweep exits 0 even though this run never successfully retried the route. test_result_preservation asserts the warning and st.finished is False, not exit code or incomplete_routes. Operators scripting on exit 0 will treat unretriable-looking crashes as a finished benchmark.
- suggested fix: If infra budget exhausts on launch_failed while a retryable disposition remains on disk, mark the route incomplete (or force EXIT_PARTIAL / a dedicated non-zero) unless record budget is also exhausted under normal retry semantics; extend the preservation tests to assert report.exit_code() != 0.

### Documented report-time seed re-check is not implemented

- source: cursor | category: config_drift | severity: low | confidence: confirmed
- changes numbers: **False**
- location: runner/oodbench/report.py:258-302
- evidence: DESIGN.md: "At report time the runner re-derives the expected seed for every result file it counts and fails the report if a file's embedded seed disagrees with the configured one."
report.build() only reads status/score from disk; assert_seed_consistency runs only on planned RouteTask filenames at plan time.
- why wrong: The defense claimed against mixing seeds in an output tree is absent at the report boundary. Normal path encodes seed in the filename so silent mix is hard, but the documented fail-loud check does not exist—config/docs drift that removes a promised audit tripwire.
- suggested fix: In report.build, parse _seedN from each result_path name (and/or task.key) and fail or hard-warn on mismatch with task.seed / configured base+rep.

## Flagged by one reviewer

### SLURM query failures are interpreted as completed jobs

- source: codex | category: state_determinism | severity: high | confidence: confirmed
- changes numbers: **True**
- location: runner/oodbench/backends/slurm.py:201-225
- evidence: `squeue` exceptions are converted to `out = ""`; then `_sacct_state()` may also return `None` on error, and the fallback path sets `attempt.outcome = AttemptOutcome.EXITED` with `detail = f"SLURM state {state or 'unknown'}"`.
- why wrong: A transient scheduler/accounting failure, or a missing `sacct`, releases the slot as if the job finished. The same route can be submitted again while the original job is still running, allowing concurrent writers to the same checkpoint and making the final metric depend on which job writes last.
- suggested fix: Treat scheduler-query errors as an unknown/infrastructure state and keep polling with backoff. Require a confirmed terminal SLURM state and verify the expected checkpoint before settling or reusing the slot.

### SLURM jobs have no node-local reserved-port probe

- source: codex | category: state_determinism | severity: high | confidence: confirmed
- changes numbers: **True**
- location: runner/oodbench/backends/slurm.py:80-95, 121-159
- evidence: `SlurmBackend.preflight()` checks only `sbatch`/`squeue` availability and partition configuration. `submit()` renders and submits the job without calling `probe_pairs`, `probe`, or any node-side port check.
- why wrong: The local backend explicitly probes its reserved CARLA and traffic-manager ports because the evaluator scans upward when a requested port is occupied. SLURM does neither on the execution node, so an occupied reserved port can move CARLA into another worker's window and silently make concurrent routes share simulator resources.
- suggested fix: Add a node-local preflight/reservation inside the submitted wrapper, or allocate ports through node-aware scheduler resources; abort the job before the evaluator can scan to another worker's ports.

### The records generator accepts non-42 seeds while labeling the output as the v0.9 seed-42 release

- source: codex | category: config_drift | severity: medium | confidence: confirmed
- changes numbers: **True**
- location: records/build_records.py:610-631, 741-752
- evidence: The CLI accepts arbitrary `--seed` values, `partial_run` does not include seed deviation, and metadata writes `"seed": args.seed` alongside the fixed note `"Seed 42 only. Every number in the paper is seed 42"`.
- why wrong: A full run with `--seed 43` can produce a v0.9 artifact with `partial_run: false`, while `VERSION` and the release note still identify the seed-42 protocol. Downstream users can treat a different-split result as the released paper records.
- suggested fix: Reject seeds other than 42 in the release generator, or mark such output explicitly non-release/non-comparable and make verification fail unless the deviation is intentional.

### decide() ignores infra budget whenever a final retryable record exists

- source: cursor | category: state_determinism | severity: medium | confidence: confirmed
- changes numbers: **True**
- location: runner/oodbench/plan.py:287-313
- evidence: if record.final:
    ...  # only record/tickruntime budgets gate RUN vs SKIP_EXHAUSTED
    return TaskDecision(Decision.RUN, ...)
# No final record. Only the infra budget can be exhausted here.
if ledger_attempts.get("infra", 0) >= infra_budget:
    return TaskDecision(Decision.SKIP_EXHAUSTED, ...)
- why wrong: Coupled with launch-failure restoration: infra can be fully spent while a retryable final remains, yet every resume still plans RUN, attempts one failed launch, warns, and exits 0 again. The route never becomes skip_exhausted, so a permanently broken port/GPU yields a looping soft-success with a stale crash score rather than a hard incomplete.
- suggested fix: When disposition is retryable, also require infra_spent < infra_budget (or a dedicated launch budget) before Decision.RUN; otherwise SKIP_EXHAUSTED with an explicit 'retries never started' reason.

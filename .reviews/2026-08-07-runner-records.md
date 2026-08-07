# Cross-review — runner/ and records/ — 2026-08-07

**Reviewers:** `gpt-5.6-luna` @ xhigh (codex, OpenAI) · `cursor-grok-4.5-high` (cursor, xAI)
**Implementer:** Claude (Opus 5). Three labs across the loop.
**Both verdicts: BLOCKING.** Neither reviewer failed.

## Change set
41 code files, 437 KB diff — the full history of `runner/` and `records/`.
**Reconstructed from git**, not the snapshot hook: these components were authored by
sub-agents across several sessions, so the hook held only the latest repair pass (20 of 44
files). Data artifacts (CSV / parquet / rename_map.json) excluded — outputs, not code.

## Why this review happened late
`release/README.md` §3b made cross-review mandatory for W2, W5 and W8. It was never run: the
orchestrator wrote "Do NOT invoke the /cross-review skill yourself; this workflow runs
independent verification agents" into every agent brief, substituting same-family adversarial
agents for different-family review. W5 and W8 both flagged the omission in their own reports
("CROSS-REVIEW STILL OWED"). The tooling was installed the whole time. The same-family reviews
did find real bugs — but the repair pass that fixed them (R1, R3) was itself never reviewed,
and that is where several findings below land.

## Reviewer summaries
- **codex:** "The records path is careful about the shipped artifact, but the runner can reuse
  stale results or mis-handle SLURM jobs while still reporting success. The biggest numerical
  risk is cache reuse after model/checkpoint/route changes."
- **cursor:** "records/ faithfully regenerates the paper table under a frozen-CSV lock; runner/
  still has a settle-path hole that can freeze kill-manufactured `Simulation crashed` records as
  benchmark results and exit 0."

## Findings and adjudication

| # | Sev | Finding | Verdict | Routing |
|---|---|---|---|---|
| 1 | high | Resume cache not invalidated by model/checkpoint/route change — identity is only `rel_dir/stem_seedN` | ACCEPT | ESCALATE (design) |
| 2 | high | Timeout/FAULT kills charge the *record* budget when the crash handler writes `Simulation crashed` | ACCEPT | ESCALATE |
| 3 | high | SLURM query failures interpreted as completed jobs (`squeue` exception → `out=""`) | ACCEPT | ESCALATE |
| 4 | high | SLURM jobs have no node-local reserved-port probe | ACCEPT | ESCALATE (design) |
| 5 | med | `decide()` ignores infra budget when a final retryable record exists | ACCEPT | ESCALATE |
| 6 | med | Infra-exhausted failed launches still exit 0 via preserved retryable record | ACCEPT | ESCALATE |
| 7 | med | records generator accepts non-42 seeds while labelling output the seed-42 release | ACCEPT | ESCALATE (touches the published artifact) |
| 8 | med | Runner reports hard-coded release metadata instead of configured values | ACCEPT | ESCALATE |
| 9 | low | Documented report-time seed re-check not implemented (DESIGN.md claims it) | ACCEPT | ESCALATE (doc/code mismatch) |

**Nothing rejected.** All nine are specific, evidenced against real code paths, and none were
caught by the earlier same-family reviews.

**Nothing fixed in this pass.** Findings 2/5/6 are all in the retry-accounting and settle path
that R3 already modified once; 1 and 4 are design changes with real trade-offs (strict resume
invalidation forces expensive re-runs; node-local port reservation changes the SLURM contract).
Fixing six interacting bugs blind, in a component with zero hardware validation, is how a
seventh gets introduced. Escalated to the user as a set.

## Consequence for the release
The repo is pushed but **untagged**, so nothing is citable and no third party has run it. The
paper's published numbers are unaffected — they came from the previous cluster orchestrators,
not this runner. `records/` was found sound on its core path: "faithfully regenerates the paper
table under a frozen-CSV lock."

# Captured SLURM output

`captured_2026-10-04.json` holds what real `sbatch`, `squeue`, `sacct` and `scancel` printed on
a SLURM **24.11.5** cluster on **2026-10-04**. The SLURM backend tests replay these bytes through
fake scheduler commands on `PATH`.

Why this exists: a parser that has only ever seen hand-typed output can pass every test and still
never match what the real program prints. A fixture here is either captured or marked synthetic.

## How it was captured

- Tiny CPU-only batch jobs: 1 CPU, `--mem=100M`, a 1–15 minute `--time`, running `sleep`,
  `exit 3`, `kill -9 $$`, or an over-long sleep.
- Each case records the command line, return code, stdout and stderr exactly as returned.
- `timelines` lists, in order, every distinct answer that `squeue` and `sacct` gave while one job
  was polled about once a second until it ended.

## Scrubbing

Job IDs and timestamps are kept as captured. Only these were replaced:

| Captured | Stored as |
|---|---|
| the cluster name (`sbatch -M` suffix) | `cluster0` |
| the submitting user name | `user` |
| the numeric UID in `CANCELLED by <uid>` | `1000` |
| the job-script directory | `<jobdir>/` |

## What real output showed

- `squeue -h -j <id>` for a job that ended **recently** prints nothing and exits 0.
- Once the controller has purged the job (after `MinJobAge`), the same command exits 1 with
  `slurm_load_jobs error: Invalid job id specified`. That is identical to an id that was never
  issued. **A non-zero `squeue` exit is therefore normal for a finished job**, not just a fault.
- `squeue` can still show `COMPLETING` while `sacct` already reports the terminal state
  (`TIMEOUT`, `CANCELLED by ...`).
- A job cancelled while `PENDING` reports `Start` as **`None`** (not `Unknown`) and `Elapsed` as
  `00:00:00`.
- `sacct` reported `PENDING` within 20 ms of submission in all four tries. The empty
  accounting reply was seen only for an id the database never issued (exit 0, no output).

## What could not be captured

- **`OUT_OF_MEMORY`.** The capture cluster did not enforce `--mem`: a job that wrote 600 MB under
  a 100 MB limit ended `COMPLETED`.
- **An empty `sacct` reply for a real, just-finished job** (accounting lag). Not observed. Tests of
  that path use `sacct_never_issued`, which is the real empty reply.
- **A malformed `--parsable` reply.** `sbatch_without_parsable` is real output, from plain
  `sbatch` without `--parsable`, and stands in for a site wrapper that drops or decorates the
  flag. Any test using output not in this file must say in its docstring that it is synthetic.

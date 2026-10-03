"""OOD-PerceptionBench portable evaluation runner.

FIRST CUT — see ../STATUS.md for what is implemented, what is untested, and what remains.
"""

__version__ = "0.9.0.dev0"

# Which release of the benchmark this runner targets, and which arXiv version of the paper
# the resulting numbers bind to. Stamped into every report. See ../VERSION.
BENCHMARK_RELEASE = "v0.9"
ARXIV_VERSION = "v1"

# Exit codes. Documented in DESIGN.md §6 and mirrored in README.md.
EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CONFIG = 2
EXIT_INTERRUPTED = 3
EXIT_NO_WORKERS = 4
EXIT_AGENT_FATAL = 5

# NOT a runner exit code: the status the patched leaderboard evaluator exits with when the CARLA
# world never answered a readiness probe (patch 330, OODPB_WORLD_READY_S). Settlement charges it
# to the infrastructure budget. Must equal WORLD_NOT_READY_EXIT_CODE in that patch; a test checks.
EVALUATOR_WORLD_NOT_READY = 75

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

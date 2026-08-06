from .base import Attempt, AttemptOutcome, Backend  # noqa: F401


def make(cfg, log) -> Backend:
    backend = cfg.execution["backend"]
    if backend == "local":
        from .local import LocalBackend
        return LocalBackend(cfg, log)
    if backend == "slurm":
        from .slurm import SlurmBackend
        return SlurmBackend(cfg, log)
    raise ValueError(f"unknown backend: {backend!r}")

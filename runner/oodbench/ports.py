"""Deterministic, collision-proof port allocation. See DESIGN.md section 3.

One CARLA instance consumes a 3-port contiguous window starting at its RPC port:

    P     -carla-rpc-port
    P + 1 streaming port   (derived by CARLA, never passed explicitly)
    P + 2 secondary port   (multi-GPU / secondary-server channel)

plus one independent traffic-manager port.

Allocation is a pure function of the worker index:

    rpc(i) = rpc_base + i * stride
    tm(i)  = tm_base  + i * stride

Not of the route, the attempt, the GPU, or the scheduling order. A worker owns its ports for
the whole sweep, and at most one route runs in a worker slot at a time, so two routes can never
contend for a port under any ordering, restart or crash-recovery path.
"""

from __future__ import annotations

import errno
import fcntl
import os
import socket
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

#: Ports CARLA occupies starting at its RPC port. Do not lower this: the streaming and
#: secondary ports are derived by the server and are never visible on our command line.
CARLA_PORT_SPAN = 3

#: Minimum stride: the CARLA window plus one port of margin.
MIN_STRIDE = CARLA_PORT_SPAN + 1

MIN_PORT = 1024
MAX_PORT = 65535

#: Where the per-port lock files live. Machine-wide and shared by every user, because two runs
#: by two users on one machine contend for the same ports. Overridable, mainly so the test
#: suite never touches the real lock directory.
PORT_LOCK_DIR_ENV = "OODPB_PORT_LOCK_DIR"
DEFAULT_PORT_LOCK_DIR = "/tmp"


class PortAllocationError(Exception):
    """Raised when the requested port layout is unsatisfiable or already occupied."""


class PortLockHeld(Exception):
    """Another process holds the lock on one or more of the requested ports."""

    def __init__(self, ports: Sequence[int], paths: Sequence[str]) -> None:
        self.ports = list(ports)
        self.paths = list(paths)
        super().__init__(f"port lock(s) held by another process: {self.ports}")


def port_lock_dir() -> str:
    return os.environ.get(PORT_LOCK_DIR_ENV) or DEFAULT_PORT_LOCK_DIR


def port_lock_path(port: int, directory: Optional[str] = None) -> str:
    return os.path.join(directory or port_lock_dir(), f"oodbench-port-{port}.lock")


@dataclass(frozen=True)
class PortPair:
    worker: int
    rpc: int
    tm: int

    @property
    def rpc_window(self) -> range:
        return range(self.rpc, self.rpc + CARLA_PORT_SPAN)

    @property
    def all_ports(self) -> Tuple[int, ...]:
        return tuple(self.rpc_window) + (self.tm,)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"worker {self.worker}: rpc={self.rpc}(+{CARLA_PORT_SPAN - 1}) tm={self.tm}"


def allocate(workers: int, rpc_base: int, tm_base: int, stride: int) -> List[PortPair]:
    """Return one :class:`PortPair` per worker, or raise :class:`PortAllocationError`.

    Every invariant is checked here rather than at use sites, so a unit test can drive the
    allocator at ``workers=64`` on a single-GPU laptop and prove the scheme sound independently
    of hardware.
    """
    if workers < 1:
        raise PortAllocationError(f"workers must be >= 1, got {workers}")
    if stride < MIN_STRIDE:
        raise PortAllocationError(
            f"ports.stride must be >= {MIN_STRIDE} (a CARLA instance occupies "
            f"{CARLA_PORT_SPAN} consecutive ports from its RPC port, plus margin); got {stride}"
        )
    for name, base in (("ports.rpc_base", rpc_base), ("ports.tm_base", tm_base)):
        if base < MIN_PORT:
            raise PortAllocationError(f"{name}={base} is below {MIN_PORT} (privileged range)")

    span = (workers - 1) * stride
    rpc_lo, rpc_hi = rpc_base, rpc_base + span + CARLA_PORT_SPAN          # half-open
    tm_lo, tm_hi = tm_base, tm_base + span + 1                            # half-open

    for name, hi in (("ports.rpc_base", rpc_hi), ("ports.tm_base", tm_hi)):
        if hi - 1 > MAX_PORT:
            raise PortAllocationError(
                f"{name} block would reach port {hi - 1}, above {MAX_PORT}. "
                f"Lower the base, the stride, or the worker count."
            )

    if rpc_lo < tm_hi and tm_lo < rpc_hi:
        raise PortAllocationError(
            f"the RPC block [{rpc_lo},{rpc_hi}) and the traffic-manager block "
            f"[{tm_lo},{tm_hi}) overlap for workers={workers}, stride={stride}. "
            f"Move ports.tm_base further from ports.rpc_base."
        )

    pairs = [
        PortPair(worker=i, rpc=rpc_base + i * stride, tm=tm_base + i * stride)
        for i in range(workers)
    ]

    # Belt and braces: the invariants above imply this, but a duplicate here would be a silent
    # two-workers-one-simulator bug, so assert it rather than trust the arithmetic.
    flat: List[int] = []
    for p in pairs:
        flat.extend(p.all_ports)
    if len(set(flat)) != len(flat):
        dupes = sorted({x for x in flat if flat.count(x) > 1})
        raise PortAllocationError(
            f"internal error: duplicate port(s) {dupes} in allocation "
            f"(workers={workers}, rpc_base={rpc_base}, tm_base={tm_base}, stride={stride})"
        )
    return pairs


def port_is_free(port: int, host: str = "localhost") -> bool:
    """True if ``port`` can be bound right now on ``host``.

    Uses exactly the call the vendored ``leaderboard_evaluator.find_free_port`` uses -- a plain
    ``bind`` with no ``SO_REUSEADDR`` -- so a port this returns True for is a port the evaluator
    will accept verbatim instead of scanning upward into the next worker's window.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((host, port))
        return True
    except OSError:
        return False


def probe(ports: Iterable[int]) -> List[int]:
    """Return the subset of ``ports`` that is currently occupied.

    Probes both ``localhost`` (what the evaluator binds when searching) and ``0.0.0.0`` (what
    the CARLA server itself binds); either failure counts as occupied.
    """
    busy: List[int] = []
    for port in ports:
        if not port_is_free(port, "localhost") or not port_is_free(port, "0.0.0.0"):
            busy.append(port)
    return busy


def probe_pairs(pairs: Sequence[PortPair]) -> List[Tuple[int, int]]:
    """Probe every reserved port. Returns ``[(worker_index, busy_port), ...]``."""
    out: List[Tuple[int, int]] = []
    for pair in pairs:
        for port in probe(pair.all_ports):
            out.append((pair.worker, port))
    return out


def _open_lock_file(path: str) -> int:
    """Open (creating if needed) one lock file read-only, whoever created it.

    ``flock`` needs only an open descriptor, so read-only is enough and lets a second user lock
    a file the first user created. The plain open comes first because most Linux distributions
    set ``fs.protected_regular``, which refuses an ``O_CREAT`` open of another user's existing
    file in a sticky, world-writable directory such as ``/tmp`` -- even though the file exists
    and a plain open would succeed. ``O_NOFOLLOW`` keeps a planted symlink from redirecting the
    open (and the ``fchmod`` below) to a file of ours elsewhere. Descriptors from ``os.open``
    are non-inheritable (PEP 446), so no evaluator or CARLA child ever holds the lock.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except FileNotFoundError:
        pass
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:  # another run created it between our two opens
        return os.open(path, flags)
    try:
        # The umask may have stripped the read bits other users need to open the file.
        os.fchmod(fd, 0o644)
    except OSError:
        os.close(fd)
        raise
    return fd


class PortLocks:
    """Exclusive advisory locks, one file per port, held for the life of a run.

    A free probe says only that nobody holds a port *now*; it does not stop a second run from
    probing the same free block a moment later. The lock does: an oodbench run takes it before
    probing and keeps it until shutdown, so a second run on the same or an overlapping block is
    refused at once instead of starting and then reaping the first run's simulators as orphans.
    One file per port, not per block, so blocks that only partly overlap collide too. The kernel
    drops a ``flock`` when its holder dies, so a crashed run never leaves a stale lock behind;
    the lock *files* stay, empty and harmless.

    It coordinates only processes that share the lock directory: oodbench runs on one machine
    with the same ``/tmp``. A CARLA started by hand takes no lock; the probe still catches that.
    """

    def __init__(self, ports: Iterable[int], directory: Optional[str] = None) -> None:
        self.ports: List[int] = sorted(set(ports))
        self.directory = directory or port_lock_dir()
        self._fds: Dict[int, int] = {}

    @property
    def held(self) -> bool:
        return bool(self._fds)

    def path(self, port: int) -> str:
        return port_lock_path(port, self.directory)

    def acquire(self) -> None:
        """Lock every port, or none of them.

        Raises :class:`PortLockHeld` naming every port another process holds, and lets any other
        ``OSError`` (an unusable directory, say) propagate. Either way nothing stays locked.
        Never waits: a held lock means another run's whole sweep, not a moment's contention.
        """
        held: List[int] = []
        try:
            for port in self.ports:
                fd = _open_lock_file(self.path(port))
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    os.close(fd)
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        held.append(port)
                        continue
                    raise
                self._fds[port] = fd
        except BaseException:
            self.release()
            raise
        if held:
            self.release()
            raise PortLockHeld(held, [self.path(p) for p in held])

    def release(self) -> None:
        """Drop every lock this object holds. Idempotent. Closing the descriptor unlocks it."""
        fds, self._fds = self._fds, {}
        for fd in fds.values():
            try:
                os.close(fd)
            except OSError:
                pass


def describe(pairs: Sequence[PortPair]) -> str:
    lines = ["worker  carla-rpc      traffic-manager"]
    for p in pairs:
        lines.append(
            f"{p.worker:>6}  {p.rpc}-{p.rpc + CARLA_PORT_SPAN - 1:<8}  {p.tm}"
        )
    return "\n".join(lines)

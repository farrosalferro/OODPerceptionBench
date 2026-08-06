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

import socket
from dataclasses import dataclass
from typing import Iterable, List, Sequence, Tuple

#: Ports CARLA occupies starting at its RPC port. Do not lower this: the streaming and
#: secondary ports are derived by the server and are never visible on our command line.
CARLA_PORT_SPAN = 3

#: Minimum stride: the CARLA window plus one port of margin.
MIN_STRIDE = CARLA_PORT_SPAN + 1

MIN_PORT = 1024
MAX_PORT = 65535


class PortAllocationError(Exception):
    """Raised when the requested port layout is unsatisfiable or already occupied."""


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


def describe(pairs: Sequence[PortPair]) -> str:
    lines = ["worker  carla-rpc      traffic-manager"]
    for p in pairs:
        lines.append(
            f"{p.worker:>6}  {p.rpc}-{p.rpc + CARLA_PORT_SPAN - 1:<8}  {p.tm}"
        )
    return "\n".join(lines)

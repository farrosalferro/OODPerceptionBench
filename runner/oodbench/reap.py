"""Process supervision helpers: fault detection and port-scoped CARLA reaping.

The reaper is deliberately narrower than the internal one, which kills any CARLA process whose
parent is PID 1. That heuristic is fine on a dedicated node and destructive on a shared
workstation, where another user's simulator can be reparented for entirely benign reasons.

Here we kill only CARLA processes whose command line names a port from **this worker's own
reserved window** -- which is only possible because ports are deterministic (DESIGN.md sect. 3).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

#: Patterns that mean the child died hard even if the exit status is ambiguous.
FAULT_PATTERNS = (
    "Segmentation fault",
    "Aborted (core dumped)",
    "Fatal Python error: Segmentation fault",
    "Illegal instruction",
    "Bus error",
)

_RPC_PORT_RE = re.compile(rb"-carla-rpc-port[= ](\d+)")


def detect_fault(path: Optional[Path], max_bytes: int = 256 * 1024) -> Optional[str]:
    """Return the first fault pattern found in the tail of ``path``, or None."""
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for pat in FAULT_PATTERNS:
        if pat in tail:
            return pat
    return None


def _iter_own_pids() -> Iterable[int]:
    uid = os.getuid()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        yield pid


def find_carla_on_ports(ports: Sequence[int]) -> List[int]:
    """PIDs of our own processes whose command line binds one of ``ports`` as CARLA's RPC port."""
    wanted = {int(p) for p in ports}
    hits: List[int] = []
    for pid in _iter_own_pids():
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except OSError:
            continue
        if b"Carla" not in cmdline and b"carla" not in cmdline:
            continue
        for match in _RPC_PORT_RE.finditer(cmdline.replace(b"\x00", b" ")):
            if int(match.group(1)) in wanted:
                hits.append(pid)
                break
    return hits


def reap_ports(ports: Sequence[int], grace_s: float = 10.0) -> List[int]:
    """SIGTERM then SIGKILL any CARLA bound to ``ports``. Returns the PIDs acted on."""
    pids = find_carla_on_ports(ports)
    if not pids:
        return []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.time() + grace_s
    while time.time() < deadline:
        if not find_carla_on_ports(ports):
            return pids
        time.sleep(0.5)
    for pid in find_carla_on_ports(ports):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return pids


def terminate_process_tree(proc: "subprocess.Popen", grace_s: float = 30.0) -> None:
    """SIGTERM the child's process group, then SIGKILL if it does not exit.

    The child is launched with ``start_new_session=True`` so it leads its own group; the
    evaluator's CARLA subprocess is a member of that group because the evaluator does not
    detach it. One ``killpg`` therefore takes the whole route down.
    """
    if proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, OSError):
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        pass

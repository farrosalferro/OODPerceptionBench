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
#:
#: **These are regexes, and the whitespace class is the whole point.** They were plain
#: substrings, and ``"Aborted (core dumped)"`` -- written with one space -- was dead code that
#: had never matched anything: the shell pads the signal name into a fixed column, so the line
#: it actually writes is ``Aborted`` + seventeen spaces + ``(core dumped)``. ``Segmentation
#: fault`` matched only by accident, because it is also present as a bare prefix before the
#: padding starts. Any pattern here that carries a space MUST use ``\s+``.
#:
#: This list is now a *secondary* signal. The primary one is the exit status
#: (:func:`describe_exit_signal`), which needs no text at all and cannot be defeated by a
#: shell's formatting or a locale. The patterns still earn their place because
#: :meth:`LocalBackend.poll` also consults them while the attempt is **still running**, where
#: there is no exit status yet: a simulator that crashes under a hung evaluator is visible only
#: in the stream. Deliberately absent: a bare ``Killed``. It is what the OOM killer's shell
#: message says, but it is also an ordinary English word an agent may log, and a false positive
#: here costs a real retry. SIGKILL is caught by exit status instead, where it is unambiguous.
FAULT_PATTERNS = (
    r"Segmentation fault",
    r"Aborted\s+\(core dumped\)",
    r"Fatal Python error: Segmentation fault",
    r"Fatal Python error: Aborted",
    r"Illegal instruction",
    r"Bus error",
)

_FAULT_RES = tuple(re.compile(p) for p in FAULT_PATTERNS)

_RPC_PORT_RE = re.compile(rb"-carla-rpc-port[= ](\d+)")

#: Highest real signal number, used to bound the shell's ``128 + N`` convention. Linux defines
#: ``NSIG`` as 65, so signals run 1..64.
_MAX_SIGNAL = getattr(signal, "NSIG", 65) - 1


def describe_exit_signal(rc: int) -> Optional[str]:
    """Name the signal that killed a process, read from its exit status alone, or ``None``.

    Death by signal is the one thing a stderr pattern cannot be trusted to report, and it is
    exactly the case that must not be mistaken for a self-terminated verdict. Two shapes reach
    us:

    * ``rc < 0`` -- :meth:`subprocess.Popen.poll` returns ``-N`` when the process we launched
      *directly* (the job script's ``bash``) was itself killed by signal *N*. A cgroup OOM
      reaper, a SLURM preemption or an operator's ``kill`` takes the whole group, shell
      included.
    * ``128 < rc <= 128 + NSIG-1`` -- the shell's convention for relaying a child's death by
      signal. :func:`jobscript.render` ends with ``exit ${rc}``, so the evaluator's ``128+N``
      arrives here verbatim.

    **The upper bound is load-bearing, not tidiness.** The vendored evaluator ends its own
    crash path with ``sys.exit(-1)``, which is exit status **255** -- numerically ``128 + 127``,
    and there is no signal 127. Without the bound, that self-terminated verdict would be
    reclassified as a hard death, which is the opposite error and would spend the ambiguity
    budget on a record the model really did write. A non-zero exit on its own is still a clean
    exit (DESIGN.md 6A.2); only death by signal is not.
    """
    if rc < 0:
        return f"killed by {_signal_name(-rc)} (the job script itself was signalled)"
    if 128 < rc <= 128 + _MAX_SIGNAL:
        return f"killed by {_signal_name(rc - 128)} (relayed by the shell as {rc})"
    return None


def _signal_name(num: int) -> str:
    try:
        return signal.Signals(num).name
    except ValueError:
        return f"signal {num}"


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
    for rx in _FAULT_RES:
        match = rx.search(tail)
        if match:
            # The matched TEXT, not the pattern: a raw regex is not something to put in a log
            # line an operator has to read, and the text is what they will grep the stream for.
            # Whitespace is collapsed so a seventeen-space column pad does not reach the report.
            return " ".join(match.group(0).split())
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

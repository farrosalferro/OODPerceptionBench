"""Fake SLURM commands that replay output captured from a real scheduler.

``fixtures/slurm/captured_2026-10-04.json`` holds what real ``sbatch``/``squeue``/``sacct``/
``scancel`` printed (see the README beside it). :func:`install` writes an executable of the
given name into a directory that the test puts first on ``PATH``. Each call of that executable
writes the next answer's stdout and stderr byte for byte and exits with its return code.

An answer is either the name of a captured case or an explicit ``{"rc", "stdout", "stderr"}``
dict. A dict is synthetic: the test using it must say so in its docstring.

Answers are consumed in order, and the last one repeats once the sequence is exhausted, so
``install(bin, "sacct", "sacct_never_issued", "sacct_completed")`` answers empty once and then
``COMPLETED`` for every later poll. Several sequences can share one executable: each
:func:`install` call with ``when=`` adds a sequence that serves only command lines containing
that substring (for example ``when="Start,End,Elapsed"``). A call that matches no ``when``
falls back to the sequence installed without one. Every call's argv is recorded for
:func:`calls`.

Captured paths were scrubbed to ``<jobdir>/...``. ``replace={"<jobdir>/a/x.sbatch": real}``
puts a test's own path back where a scrubbed one stood; nothing else in the answer changes.
"""

from __future__ import annotations

import json
import stat
import sys
import textwrap
from pathlib import Path
from typing import Dict, List, Optional, Union

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "slurm" / "captured_2026-10-04.json"

Answer = Union[str, Dict[str, object]]

_FAKE = textwrap.dedent('''\
    import json, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    name = os.path.basename(__file__)
    with open(os.path.join(here, name + ".replay.json"), encoding="utf-8") as f:
        plan = json.load(f)
    argv = sys.argv[1:]
    with open(os.path.join(here, name + ".calls.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(argv) + "\\n")
    line = " ".join(argv)
    chosen = None
    for index, seq in enumerate(plan):
        if seq["when"] is not None and seq["when"] in line:
            chosen = index
            break
    if chosen is None:
        for index, seq in enumerate(plan):
            if seq["when"] is None:
                chosen = index
                break
    if chosen is None:
        sys.stderr.write("replay fake %s: no sequence matches %r\\n" % (name, argv))
        sys.exit(97)
    counter = os.path.join(here, "%s.%d.count" % (name, chosen))
    try:
        with open(counter, encoding="utf-8") as f:
            used = int(f.read() or 0)
    except FileNotFoundError:
        used = 0
    with open(counter, "w", encoding="utf-8") as f:
        f.write(str(used + 1))
    answers = plan[chosen]["answers"]
    answer = answers[min(used, len(answers) - 1)]
    sys.stdout.write(answer["stdout"])
    sys.stdout.flush()
    sys.stderr.write(answer["stderr"])
    sys.stderr.flush()
    sys.exit(answer["rc"])
''')


def load() -> dict:
    with FIXTURE.open(encoding="utf-8") as f:
        return json.load(f)


def case(name: str) -> Dict[str, object]:
    """One captured answer: ``{"argv", "rc", "stdout", "stderr"}``."""
    return load()["cases"][name]


def timeline(name: str, tool: str) -> List[Dict[str, object]]:
    """The distinct answers ``tool`` gave, in order, while one real job was polled."""
    return load()["timelines"][name][tool]


def _resolve(answer: Answer, replace: Dict[str, str]) -> Dict[str, object]:
    if isinstance(answer, str):
        found = case(answer)
    else:
        found = answer
    missing = {"rc", "stdout", "stderr"} - set(found)
    if missing:
        raise ValueError(f"replay answer {answer!r} lacks {sorted(missing)}")
    stdout, stderr = str(found["stdout"]), str(found["stderr"])
    for old, new in replace.items():
        if old not in stdout + stderr:
            raise ValueError(f"replay answer {answer!r} does not contain {old!r}")
        stdout, stderr = stdout.replace(old, new), stderr.replace(old, new)
    return {"rc": int(found["rc"]), "stdout": stdout, "stderr": stderr}


def install(bin_dir: Path, name: str, *answers: Answer, when: Optional[str] = None,
            replace: Optional[Dict[str, str]] = None) -> Path:
    """Install (or extend) the fake ``name`` in ``bin_dir``; see the module docstring."""
    if not answers:
        raise ValueError("install() needs at least one answer")
    bin_dir = Path(bin_dir)
    plan_path = bin_dir / f"{name}.replay.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else []
    plan = [seq for seq in plan if seq["when"] != when]
    plan.append({"when": when, "answers": [_resolve(a, replace or {}) for a in answers]})
    # Specific matches first, so a later catch-all never shadows them.
    plan.sort(key=lambda seq: seq["when"] is None)
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    for stale in bin_dir.glob(f"{name}.*.count"):
        stale.unlink()
    tool = bin_dir / name
    tool.write_text("#!" + sys.executable + "\n" + _FAKE, encoding="utf-8")
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def calls(bin_dir: Path, name: str) -> List[List[str]]:
    """Every argv the fake ``name`` was called with, oldest first."""
    path = Path(bin_dir) / f"{name}.calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

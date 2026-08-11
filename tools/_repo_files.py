"""Which files does this repository actually ship?

Every check under `tools/` answers a question about **our** content — does it leak a private
path, does it name a non-redistributable asset's source, does every markdown link resolve. All
three had their own idea of which files to look at, and all three were wrong in the same way.

**The bug this module exists to end.** `setup.sh` clones the pinned upstream into `third_party/`,
which `.gitignore` excludes because it is not ours. The checkers walked the filesystem, so on a
tree where `setup.sh` had been run they scanned upstream's code and failed on it — two hits in
`third_party/carla_garage/`, one an `export CARLA_ROOT=` line pointing at the upstream authors'
own HPC scratch filesystem, the other a `wget` of their public S3 dataset bucket whose URL path
happens to contain the same word as one of our private conda environments.

(The offending strings are deliberately *described* rather than quoted here. Quoting them would
put them back in a tracked file and trip `check_no_cluster_paths.py` — which is precisely what
happened on the first attempt to commit this module, and the check was right to refuse. The
allowlist exists for files whose job is to document the ban; a utility module is not one, and
every allowlist entry is a hole.)

Neither is a leak of ours: one is the upstream authors' own infrastructure, the other is a
public bucket path that merely collides with a word on our denylist. Alongside them, 45
"broken" markdown links in vendored docs whose relative targets were never ours to satisfy.

The consequence was worse than noise. In our own working tree `setup.sh` has never been run, so
`third_party/` does not exist and every check passed — while **any user who followed the
quickstart** got `check_release_ready.py` exiting non-zero with two alarming failures. A gate
that cries wolf on a correct installation is worse than no gate: it teaches the operator to
ignore it. Found by the first fresh-clone acceptance run, 2026-08-11.

**The rule, stated once here instead of three times badly.** A file is ours if **git tracks it**.
That is the same definition as "what a user receives when they clone", which is exactly what
these checks are about. Tracked includes *staged* files, so the pre-push hook still catches a
leak on its way in.

Outside a git checkout — an extracted tarball, say — there is no index to consult, so we fall
back to walking and skip the directories `.gitignore` names. That path is strictly weaker and
says so when used.
"""

from __future__ import annotations

import os
import subprocess
from typing import Iterator, Optional, Set

#: Directories that are never ours, used only by the no-git fallback. Kept in step with
#: `.gitignore`; `third_party` is the one that mattered.
FALLBACK_SKIP_DIRS: Set[str] = {
    ".git", ".github/cache", "__pycache__", "node_modules", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ipynb_checkpoints", ".reviews",
    "third_party", "results", "Import", "Saved", "Intermediate",
}

#: Binary and archive extensions: nothing textual to leak, and reading them is a waste.
SKIP_SUFFIX: Set[str] = {
    ".png", ".jpg", ".jpeg", ".pdf", ".fbx", ".uasset", ".uexp",
    ".tar", ".gz", ".zip", ".parquet", ".pyc", ".so",
}


def _tracked(root: str) -> Optional[list[str]]:
    """Paths git tracks under `root`, relative and sorted. None if this is not a checkout."""
    try:
        out = subprocess.run(
            ["git", "-C", root, "ls-files", "-z"],
            capture_output=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted(p for p in out.decode("utf-8", "replace").split("\0") if p)


def repo_files(root: str, skip_suffix: Optional[Set[str]] = None) -> Iterator[str]:
    """Yield repo-relative paths of every text-ish file this repository ships.

    Prefer this over `os.walk` in anything under `tools/`. The order is deterministic so two
    runs of a check report their hits in the same sequence.
    """
    suffixes = SKIP_SUFFIX if skip_suffix is None else skip_suffix
    tracked = _tracked(root)
    if tracked is not None:
        for rel in tracked:
            if os.path.splitext(rel)[1].lower() in suffixes:
                continue
            if os.path.isfile(os.path.join(root, rel)):
                yield rel
        return

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in FALLBACK_SKIP_DIRS]
        for name in sorted(filenames):
            if os.path.splitext(name)[1].lower() in suffixes:
                continue
            yield os.path.relpath(os.path.join(dirpath, name), root)


def source_description(root: str) -> str:
    """One line for a check to print, so the reader knows which rule produced the file set."""
    return ("git-tracked files" if _tracked(root) is not None
            else "filesystem walk (NOT a git checkout — gitignored dirs skipped by name)")

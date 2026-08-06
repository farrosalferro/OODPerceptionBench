#!/usr/bin/env python3
"""Fail if any cluster-specific path, hostname, private conda env, or credential
pattern leaks into the public repository.

The benchmark was developed on a private SLURM cluster. Absolute `/po1` and
`/po5` paths, `yagi*` node names, jump hosts, and internal conda env names are
useless to a public user and, worse, make a shipped script look configurable
when it is actually hardwired to a machine nobody else has.

Run standalone:
    python3 tools/check_no_cluster_paths.py [--root .]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

# (regex, human-readable reason)
FORBIDDEN = [
    # `\b` not `/` — an earlier version required a trailing slash and therefore
    # missed a bare "/po5" in prose, which is the form documentation tends to use.
    (r"/po[0-9]+\b",             "absolute private-cluster storage path"),
    (r"\byagi[0-9]{1,3}",        "private cluster node name"),
    (r"\bakiu[0-9]?\b",          "private jump host"),
    (r"\bwumbo\b",               "private workstation name"),
    (r"/mnt/lustre/",            "another project's hardcoded cluster path"),
    (r"\bb2d_zoo_clean\b",       "private conda environment name"),
    (r"\bgarage_2\b",            "private conda environment name"),
    (r"max_num_jobs\.txt",       "private cluster cap-gate file"),
    (r"/media/[A-Za-z0-9_]+/College/", "private workstation asset path"),
    (r"hooks\.slack\.com/services/", "Slack webhook URL"),
    # Internal planning documents. They are not part of the public repository, so
    # any reference to them is a dangling pointer for every reader outside the lab,
    # and they carry decision history that was deliberately not published.
    (r"\bRELEASE_PLAN\.md\b",    "internal planning document"),
    (r"\brelease/staging/",      "internal staging path"),
    (r"(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*['\"][^'\"]{12,}",
     "hardcoded credential"),
]

SKIP_DIRS = {".git", ".github/cache", "__pycache__", "node_modules", ".venv"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".pdf", ".fbx", ".uasset", ".uexp",
               ".tar", ".gz", ".zip", ".parquet", ".pyc", ".so"}

# Paths whose job is to DOCUMENT the ban, by naming the very things that are
# banned. Keep this set tiny and justified — every entry is a hole in the check.
ALLOWLIST = {
    # Defines the patterns.
    "tools/check_no_cluster_paths.py",
    # Its entire purpose is to enumerate the internal files that were excluded
    # from the release, several of which are named after cluster nodes and
    # scheduler files. Naming them is the audit trail; omitting them would hide
    # what was dropped.
    "patches/EXCLUDED.md",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()

    patterns = [(re.compile(p), why) for p, why in FORBIDDEN]
    hits: list[tuple[str, int, str, str]] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(args.root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, args.root)
            if rel in ALLOWLIST:
                continue
            if os.path.splitext(name)[1].lower() in SKIP_SUFFIX:
                continue
            try:
                with open(path, encoding="utf-8", errors="strict") as fh:
                    lines = fh.readlines()
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable — nothing to leak in text form
            scanned += 1
            for i, line in enumerate(lines, 1):
                for rx, why in patterns:
                    if rx.search(line):
                        hits.append((rel, i, why, line.rstrip()[:160]))

    print(f"scanned {scanned} text files under {args.root}")
    if hits:
        print(f"\nFAIL — {len(hits)} forbidden reference(s):\n")
        for rel, ln, why, text in hits:
            print(f"  {rel}:{ln}  [{why}]")
            print(f"      {text}")
        return 1
    print("PASS — no cluster-specific paths, hostnames, env names, or credentials")
    return 0


if __name__ == "__main__":
    sys.exit(main())

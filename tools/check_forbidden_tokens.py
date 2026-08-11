#!/usr/bin/env python3
"""Fail if the repository names the upstream source of a non-redistributable prop.

Twelve of the eighteen OOD props are not in this release. Two of them are absent
because their upstream meshes were third-party game IP, uploaded to a free model
site under a licence the uploader had no right to grant. Removing the assets was
the point; naming their source slugs anywhere in the repository would put the
association back — *this* shipped prop was a copy of *that* — which is the one
thing the removal was for.

The obvious implementation of this check spells the slugs out in order to grep
for them, and therefore publishes them itself, in a file that every visitor can
read. This one does not: `tools/forbidden_tokens.txt` holds salted SHA-256
digests, this script hashes the repository's own text the same way, and a hit is
reported as a file, a line number and an opaque digest prefix. Nothing in the
denylist, in this script, or in a CI log names anything.

That makes the check *self-scanning*: unlike a cleartext pattern list, neither
this file nor the denylist needs to be excluded from its own scan, so there is no
allowlist hole to walk through.

**This is obfuscation, not secrecy.** The salt sits next to the digests and the
token space is tiny, so anyone who already knows the answer can confirm it. The
guarantee being bought is the narrow one that matters: the repository never
*states* the association, and a regression still turns CI red.

Usage
-----
    python3 tools/check_forbidden_tokens.py [--root .] [--quiet]
    printf '%s' 'a slug' | python3 tools/check_forbidden_tokens.py --hash-token

Exit status
-----------
    0  no forbidden token found
    1  at least one forbidden token found (file and line are printed; the token is not)
    2  the check could not run — missing, corrupt or truncated denylist, or the
       self-test failed. Never treat 2 as a pass: it means nothing was verified.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DENYLIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forbidden_tokens.txt")

# Built by concatenation so that the literal never appears as a token in this
# file. The scanner would otherwise flag its own source, which would force an
# allowlist entry — and an allowlisted file is a hole.
CANARY_TOKEN = "oodpb" + "forbidden" + "canary0"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".fbx", ".uasset", ".uexp",
               ".tar", ".gz", ".zip", ".7z", ".parquet", ".pyc", ".so", ".bin",
               ".woff", ".woff2", ".ico"}

_WORD_SEP = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> list[str]:
    """Lowercase, collapse every non-alphanumeric run to a separator, split."""
    return [w for w in _WORD_SEP.split(text.lower()) if w]


def digest(salt: bytes, token: str) -> str:
    return hashlib.sha256(salt + b"\x00" + token.encode("utf-8")).hexdigest()


class Denylist:
    def __init__(self, salt: bytes, canary: str, by_len: dict[int, set[str]]):
        self.salt = salt
        self.canary = canary
        self.by_len = by_len
        self.lengths = sorted(by_len)
        self.total = sum(len(v) for v in by_len.values())

    def hits_in(self, words: list[str], cache: dict[str, str | None]) -> list[tuple[int, str]]:
        """Return [(n, digest)] for every denied n-gram present in `words`."""
        found: list[tuple[int, str]] = []
        for n in self.lengths:
            bucket = self.by_len[n]
            for i in range(len(words) - n + 1):
                gram = "-".join(words[i:i + n])
                if gram in cache:
                    hit = cache[gram]
                else:
                    d = digest(self.salt, gram)
                    hit = d if d in bucket else None
                    cache[gram] = hit
                if hit is not None:
                    found.append((n, hit))
        return found


def load_denylist(path: str) -> Denylist:
    salt_hex = canary = None
    declared = None
    by_len: dict[int, set[str]] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key == "salt":
                    salt_hex = val
                elif key == "canary":
                    canary = val
                elif key == "count":
                    declared = int(val)
                continue
            n_str, _, dig = line.partition(" ")
            by_len.setdefault(int(n_str), set()).add(dig.strip())

    if not salt_hex or not canary or declared is None:
        raise ValueError("denylist is missing salt, canary or count")
    total = sum(len(v) for v in by_len.values())
    if total != declared:
        # A truncated or half-merged denylist must fail loudly. Silently checking
        # fewer tokens than the file claims is the failure mode that lets a
        # regression through while CI stays green.
        raise ValueError(f"denylist declares count={declared} but carries {total} tokens")
    return Denylist(bytes.fromhex(salt_hex), canary, by_len)


def self_test(dl: Denylist) -> None:
    """Prove the pipeline is live before trusting a clean scan.

    An empty denylist, a mangled salt or a broken normaliser would all produce a
    green run that verified nothing. So the checker first plants a token it knows
    must be caught, in a shape a real leak would take, and requires a hit.
    """
    if digest(dl.salt, CANARY_TOKEN) != dl.canary:
        raise ValueError("canary digest does not match the denylist: wrong salt or hash scheme")
    probe = f"harmless prose {CANARY_TOKEN.upper()} more prose"
    canary_only = Denylist(dl.salt, dl.canary, {1: {dl.canary}})
    if not canary_only.hits_in(normalise(probe), {}):
        raise ValueError("self-test failed: the scanner did not flag its own canary")
    if canary_only.hits_in(normalise("ordinary text with nothing to find"), {}):
        raise ValueError("self-test failed: the scanner flagged clean text")


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _repo_files import repo_files  # noqa: E402


def scan(root: str, dl: Denylist, quiet: bool) -> int:
    cache: dict[str, str | None] = {}
    hits: list[tuple[str, int, int, str]] = []
    scanned = 0

    # Only files this repository SHIPS -- see tools/_repo_files.py.
    for rel in repo_files(root, skip_suffix=SKIP_SUFFIX):
            path = os.path.join(root, rel)
            try:
                with open(path, encoding="utf-8", errors="strict") as fh:
                    lines = fh.readlines()
            except (OSError, UnicodeDecodeError):
                continue  # binary or unreadable: nothing textual to leak
            scanned += 1
            for lineno, line in enumerate(lines, 1):
                for n, dig in dl.hits_in(normalise(line), cache):
                    hits.append((rel, lineno, n, dig[:8]))

    if hits:
        print(f"FAILED: {len(hits)} forbidden source token(s) found in {scanned} files\n")
        print("The matched text is deliberately NOT printed — printing it here would")
        print("reproduce the leak in the CI log. Open each line and remove the")
        print("upstream source slug; describe the prop dimensionally instead.\n")
        for rel, lineno, n, short in hits:
            print(f"  {rel}:{lineno}: matches denied {n}-word token {short}…")
        return 1

    if not quiet:
        print(f"OK: no forbidden source token in {scanned} text files "
              f"({dl.total} tokens checked, lengths {dl.lengths}, canary verified)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=REPO, help="repository root to scan")
    ap.add_argument("--denylist", default=DENYLIST)
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--hash-token", action="store_true",
                    help="read one token from STDIN and print the denylist line for it")
    args = ap.parse_args()

    try:
        dl = load_denylist(args.denylist)
        self_test(dl)
    except (OSError, ValueError) as exc:
        print(f"CANNOT RUN: {exc}", file=sys.stderr)
        print("This is not a pass. Nothing was verified.", file=sys.stderr)
        return 2

    if args.hash_token:
        words = normalise(sys.stdin.read())
        if not words:
            print("CANNOT RUN: stdin held no token", file=sys.stderr)
            return 2
        token = "-".join(words)
        print(f"{len(words)} {digest(dl.salt, token)}")
        print(f"# paste the line above into {os.path.relpath(args.denylist, REPO)} "
              f"and bump count to {dl.total + 1}", file=sys.stderr)
        return 0

    return scan(args.root, dl, args.quiet)


if __name__ == "__main__":
    sys.exit(main())

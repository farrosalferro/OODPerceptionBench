"""Tests for the goldens-claim check in tools/check_release_ready.py.

The check compares three things that must agree: the bundles under tests/goldens/, the
`goldens_present` line in tests/VERSION, and the v0.9 cell of the README's "Acceptance
goldens" row. Each test builds a small temporary copy of just those files and points the
module's REPO at it, so the real repository is never touched.

    python3 -m pytest -q tools/tests
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil

import pytest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_REPO = os.path.dirname(TOOLS)
SHIPPED_GOLDEN = os.path.join(REAL_REPO, "tests", "goldens", "pdmlite_seed42_v0.9.golden.json")


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "check_release_ready_under_test", os.path.join(TOOLS, "check_release_ready.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def checker(monkeypatch):
    monkeypatch.syspath_prepend(TOOLS)
    return _load_module()


def _write_bundle(root, name: str, n_routes) -> None:
    """A copy of the shipped bundle covering `n_routes` routes; None drops the count."""
    with open(SHIPPED_GOLDEN, encoding="utf-8") as fh:
        doc = json.load(fh)
    if n_routes is None:
        del doc["split"]["n_routes"]
    else:
        doc["split"]["n_routes"] = n_routes
    with open(os.path.join(root, "tests", "goldens", name), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def _make_repo(root, *, bundles: int, cell: str, declared: bool, n_routes: int = 9) -> str:
    goldens = os.path.join(root, "tests", "goldens")
    os.makedirs(goldens)
    # EXAMPLE never counts as a real bundle, so every copy carries it.
    shutil.copy(SHIPPED_GOLDEN, os.path.join(goldens, "EXAMPLE.golden.json"))
    for i in range(bundles):
        _write_bundle(root, f"bundle{i}.golden.json", n_routes)
    with open(os.path.join(root, "tests", "VERSION"), "w", encoding="utf-8") as fh:
        fh.write(f"component: tests\ngoldens_present: {'true' if declared else 'false'}\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("| Feature | v0.9 | v1.0 |\n|---|---|---|\n"
                 f"| Acceptance goldens | {cell} | regenerate for v1.0 |\n")
    return root


def _verdict(checker, monkeypatch, root):
    monkeypatch.setattr(checker, "REPO", root)
    return checker._goldens_claim_is_honest()


MEASURED_9 = "measured PDM-Lite bundle for the 9-route smoke split"


def test_no_bundle_and_readme_says_none_passes(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=0, cell="none", declared=False)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.PASS, why


def test_no_bundle_but_readme_claims_one_fails(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=0, cell=MEASURED_9, declared=False)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "no golden bundle exists" in why


def test_bundle_present_but_readme_says_none_fails(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=1, cell="none", declared=True)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "README" in why


def test_bundle_present_with_wrong_route_count_fails(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=True, n_routes=12)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "12" in why and "9" in why


def test_bundles_disagreeing_on_route_count_fail(checker, monkeypatch, tmp_path):
    # The README matches one of the two bundles, which used to be enough to pass.
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=True)
    _write_bundle(root, "other.golden.json", 12)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "different route counts" in why and "other.golden.json=12" in why


def test_unreadable_bundle_fails(checker, monkeypatch, tmp_path):
    # A broken bundle used to be skipped while a good one next to it carried the check.
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=True)
    with open(os.path.join(root, "tests", "goldens", "broken.golden.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{ not json")
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "broken.golden.json" in why


def test_bundle_without_a_route_count_fails(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=True)
    _write_bundle(root, "nocount.golden.json", None)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "nocount.golden.json" in why


def test_measured_cell_without_a_route_count_fails(checker, monkeypatch, tmp_path):
    # "measured" alone used to pass: the count is the part of the promise that can be wrong.
    root = _make_repo(str(tmp_path), bundles=1, cell="measured PDM-Lite bundle", declared=True)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "N-route" in why


def test_bundle_present_and_readme_agrees_passes(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=True)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.PASS, why


def test_version_stamp_disagreeing_with_filesystem_fails(checker, monkeypatch, tmp_path):
    root = _make_repo(str(tmp_path), bundles=1, cell=MEASURED_9, declared=False)
    state, why = _verdict(checker, monkeypatch, root)
    assert state == checker.TODO
    assert "goldens_present" in why


def test_the_real_repository_passes(checker):
    state, why = checker._goldens_claim_is_honest()
    assert state == checker.PASS, why

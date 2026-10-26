"""The fixed-asset checks: they find the seeded errors and raise nothing else.

The fixture is the contract. Every number in it is computed, every error in it is
deliberate, and the checks are measured against the ground truth the generator wrote
down before it corrupted anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tiepoint_bench import checks as fixed_assets  # noqa: E402
from tiepoint_bench.checks import Config, Doc, run_checks  # noqa: E402
from tiepoint_bench.fixtures import build, canonical_plan, random_plan  # noqa: E402


def _docs(root: Path) -> list[Doc]:
    """Every document in a fixture, routed the way intake routes it."""
    docs = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        request = rel.parts[0] if len(rel.parts) > 1 else ""
        docs.append(Doc(name=path.name, path=path, request=request))
    return docs


def _config(root: Path) -> Config:
    """The firm's config: the policy, read from the policy document.

    This is what the benchmark does, and what the demo engagement carries in its
    config: the rates come from the policy, so both arms read the same one.
    """
    policy = [d for d in _docs(root) if "policy" in d.name]
    rates = fixed_assets.read_policy(policy[0]) if policy else {}
    return Config(rates=rates)


def _checks(root: Path) -> list:
    return run_checks(_docs(root), _config(root))


@pytest.fixture()
def canonical(tmp_path):
    plan = canonical_plan()
    out = tmp_path / "dry-run"
    build(plan, out)
    return plan, out


# ------------------------------------------------------------------ the canonical fixture

def test_the_canonical_fixture_yields_exactly_the_seeded_errors(canonical):
    """Four seeded errors, five reports: the ledger error is in the GL and the TB."""
    plan, out = canonical
    findings = _checks(out)
    kinds = sorted(f.kind for f in findings)
    assert kinds == ["cost_variance", "cost_variance", "dep_variance",
                     "missing_approval", "missing_support"]
    by_kind = {f.kind: f for f in findings}
    assert by_kind["dep_variance"].subject == "Office Equipment"
    assert by_kind["dep_variance"].amount == 3_000
    assert by_kind["cost_variance"].subject == "Office Equipment"
    assert by_kind["cost_variance"].amount == -2_000
    assert by_kind["missing_support"].subject == "FA-0102"
    assert by_kind["missing_approval"].subject == "DISP-2026-003"


def test_a_finding_carries_the_locator_of_the_figure_that_proves_it(canonical):
    """No locator, no fact (DECISIONS §5)."""
    _plan, out = canonical
    for finding in _checks(out):
        assert finding.locator, f"{finding.kind} carries no locator"
        assert finding.locator.get("file")


def test_a_summed_figure_names_its_cells_and_never_claims_one(canonical):
    """A variance computed from four cells cannot honestly claim one of them."""
    _plan, out = canonical
    findings = _checks(out)
    rollforward = [f for f in findings if f.kind == "rollforward_variance"]
    for finding in findings:
        if finding.locator.get("kind") == "cells":
            assert "cell" not in finding.locator
            assert finding.locator.get("cells")
    assert rollforward == []      # the canonical fixture's rollforward balances


def test_the_near_miss_names_are_not_confused(canonical):
    """"Forklift" and "Forklift replacement" are different assets.

    The replacement is an addition with its own invoice, and it is supported. The
    forklift is a disposal with no approval, and it is flagged. A matcher that did
    substring matching would raise the approval on the wrong asset.
    """
    _plan, out = canonical
    findings = _checks(out)
    missing = [f for f in findings if f.kind == "missing_approval"]
    assert len(missing) == 1
    assert missing[0].subject == "DISP-2026-003"
    assert "Forklift" in missing[0].detail
    # and nothing was raised against the addition that does have an invoice
    assert not [f for f in findings if f.subject == "FA-0150"]


def test_the_policy_is_read_from_the_policy_document(canonical):
    """The rates come from the document, so both arms of the benchmark read the same one."""
    _plan, out = canonical
    policy = [d for d in _docs(out) if "policy" in d.name][0]
    rates = fixed_assets.read_policy(policy)
    assert rates == {"Buildings": 0.02, "Plant & Machinery": 0.15,
                     "Office Equipment": 0.20, "Motor Vehicles": 0.25}


def test_a_class_with_no_configured_rate_is_a_finding_not_a_guess(canonical):
    """The engine never guesses a rate. It says the check could not run."""
    _plan, out = canonical
    findings = run_checks(_docs(out), Config(rates={}))
    kinds = [f.kind for f in findings]
    assert "no_policy" in kinds
    # and nothing was guessed: the policy check did not run
    assert "dep_variance" not in kinds


# ------------------------------------------------------------------ a population

def _build(tmp_path, count, seed=11):
    """A small population, and its ground truth."""
    truth = {}
    for i in range(count):
        plan = random_plan(seed, i)
        out = tmp_path / f"fixture-{i:03d}"
        truth[plan.name] = build(plan, out)
    return truth


def test_a_clean_fixture_raises_nothing(tmp_path):
    """A population with no errors is what makes precision falsifiable."""
    for i in range(40):
        plan = random_plan(3, i)
        if plan.ground_truth():
            continue
        out = tmp_path / "clean"
        build(plan, out)
        findings = _checks(out)
        assert findings == [], f"a clean fixture raised {findings}"
        return
    pytest.skip("no clean fixture in this draw")


def test_the_checks_find_every_seeded_error_and_raise_nothing_else(tmp_path):
    """The property the whole benchmark rests on."""
    for i in range(10):
        plan = random_plan(11, i)
        out = tmp_path / f"fixture-{i:03d}"
        truth = build(plan, out)
        findings = _checks(out)
        found = set()
        for finding in findings:
            for error in truth:
                if error["type"] != finding.kind:
                    continue
                if finding.subject in error["aliases"] or \
                        any(a in finding.subject for a in error["aliases"]):
                    found.add(error["id"])
        assert len(found) == len(truth), (
            f"{plan.name}: missed {[e['id'] for e in truth if e['id'] not in found]}")
        # and nothing that is not there
        for finding in findings:
            claimed = [e for e in truth
                       if e["type"] == finding.kind
                       and (finding.subject in e["aliases"]
                            or any(a in finding.subject for a in e["aliases"]))]
            assert claimed, f"{plan.name}: raised {finding.kind} on {finding.subject}, " \
                            f"which is not an error in this fixture"


def test_a_missing_invoice_is_reported_against_the_asset_it_is_missing(tmp_path):
    """The finding names the asset, not the document that is absent."""
    for i in range(30):
        plan = random_plan(5, i)
        if not any(e["type"] == "missing_support" for e in plan.ground_truth()):
            continue
        out = tmp_path / "fixture"
        build(plan, out)
        findings = _checks(out)
        missing = [f for f in findings if f.kind == "missing_support"]
        assert missing
        assert all(f.subject.startswith("FA-") for f in missing)
        return
    pytest.skip("no missing support in this draw")

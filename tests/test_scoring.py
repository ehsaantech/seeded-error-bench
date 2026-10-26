"""The scorer.

The scorer is the part that must not be able to flatter anyone. Its rules are pinned
here: a duplicate is neither a hit nor a false positive, an unmatched finding is a
false positive, and two different assets are never the same subject — however similar
their names.
"""

from __future__ import annotations

import pytest

from tiepoint_bench.checks import Finding
from tiepoint_bench.scoring import norm_type, score, same_subject


def _error(kind="cost_variance", subject="Office Equipment", aliases=None, amount=2000):
    return {"id": "E01", "type": kind, "subject": subject, "amount": amount,
            "detail": "", "aliases": aliases or [subject]}


def test_a_matching_finding_is_a_hit():
    s = score([Finding(kind="cost_variance", subject="Office Equipment", detail="")],
              [_error()])
    assert (s["found"], s["missed"], s["false_positives"], s["duplicates"]) == (1, 0, 0, 0)
    assert s["precision"] == 1.0 and s["recall"] == 1.0


def test_an_unmatched_finding_is_a_false_positive():
    s = score([Finding(kind="cost_variance", subject="Motor Vehicles", detail="")],
              [_error()])
    assert (s["found"], s["missed"], s["false_positives"]) == (0, 1, 1)
    assert s["precision"] == 0.0


def test_a_second_finding_on_the_same_error_is_a_duplicate_not_a_false_positive():
    """The ledger error is in the GL and in the trial balance: one error, two reports."""
    findings = [
        Finding(kind="cost_variance", subject="Office Equipment", detail="from the GL"),
        Finding(kind="cost_variance", subject="Office Equipment", detail="from the TB"),
    ]
    s = score(findings, [_error()])
    assert s["found"] == 1
    assert s["duplicates"] == 1
    assert s["false_positives"] == 0
    assert s["precision"] == 1.0


def test_a_claim_about_a_different_asset_is_not_a_hit():
    """"Forklift" and "Forklift replacement" are different assets."""
    s = score([Finding(kind="missing_approval", subject="Forklift replacement", detail="")],
              [_error(kind="missing_approval", subject="Forklift",
                      aliases=["Forklift", "Forklift"])])
    assert s["found"] == 0
    assert s["false_positives"] == 1


def test_a_reference_matches_the_asset_it_names():
    s = score([Finding(kind="missing_support", subject="FA-0102 (CNC machine 2)", detail="")],
              [_error(kind="missing_support", subject="FA-0102",
                      aliases=["FA-0102", "CNC machine 2"])])
    assert s["found"] == 1


def test_a_description_matches_the_asset_it_names():
    s = score([Finding(kind="missing_support", subject="CNC machine 2", detail="")],
              [_error(kind="missing_support", subject="FA-0102",
                      aliases=["FA-0102", "CNC machine 2"])])
    assert s["found"] == 1


def test_the_type_synonyms_fold_onto_the_taxonomy():
    assert norm_type("depreciation_error") == "dep_variance"
    assert norm_type("missing_invoice") == "missing_support"
    assert norm_type("no_approval") == "missing_approval"
    assert norm_type("DEP_VARIANCE") == "dep_variance"


def test_a_finding_of_the_wrong_type_is_not_a_hit():
    s = score([Finding(kind="dep_variance", subject="Office Equipment", detail="")],
              [_error(kind="cost_variance")])
    assert s["found"] == 0
    assert s["false_positives"] == 1


def test_a_clean_fixture_makes_precision_falsifiable():
    """No errors seeded, and the arm raises one: that is a false positive."""
    s = score([Finding(kind="cost_variance", subject="Office Equipment", detail="")], [])
    assert s["seeded"] == 0
    assert s["false_positives"] == 1
    assert s["precision"] == 0.0
    assert s["recall"] is None

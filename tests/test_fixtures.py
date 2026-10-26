"""The fixture generator: deterministic, self-checking, and honest about its errors.

A fixture is evidence, and a benchmark rests on the fixture. So the generator has to
be reproducible (the same seed is the same bytes), consistent (nothing is wrong by
accident), and explicit (the ground truth says what was corrupted, and says it before
the fixture is written).
"""

from __future__ import annotations

import sys
from pathlib import Path

from tiepoint_bench.fixtures import build, canonical_plan, random_plan  # noqa: E402


def test_the_generator_is_deterministic():
    """The same seed and index are the same fixture, every time."""
    a = random_plan(7, 3)
    b = random_plan(7, 3)
    assert a.ground_truth() == b.ground_truth()
    assert [x.description for x in a.assets] == [x.description for x in b.assets]


def test_a_fixture_is_byte_stable(tmp_path):
    """Regenerating a fixture must not change its hash: the demo byte-checks it."""
    import hashlib

    def digests(root: Path) -> dict[str, str]:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(root.rglob("*")) if p.is_file()}

    plan = random_plan(7, 3)
    first = tmp_path / "a"
    second = tmp_path / "b"
    build(plan, first)
    build(plan, second)
    assert digests(first) == digests(second)


def test_the_canonical_fixture_keeps_its_four_seeded_errors():
    truth = canonical_plan().ground_truth()
    assert sorted(e["type"] for e in truth) == [
        "cost_variance", "dep_variance", "missing_approval", "missing_support"]
    amounts = {e["type"]: e["amount"] for e in truth}
    assert amounts["dep_variance"] == 3_000
    assert amounts["cost_variance"] == 2_000


def test_the_canonical_fixture_keeps_its_numbers():
    """The demo fixture is the one the demo talks about. Its numbers do not move."""
    plan = canonical_plan()
    assert sum(c.opening_cost for c in plan.classes) == 2_000_000
    assert sum(c.additions for c in plan.classes) == 405_000
    assert sum(c.disposals for c in plan.classes) == 80_000
    assert sum(plan.true_closing(c) for c in plan.classes) == 2_325_000
    assert plan.total_charge() == 215_500
    assert sum(plan.accum_closing(c) for c in plan.classes) == 784_500
    assert plan.loss_on_disposal() == 37_000


def test_the_ground_truth_never_contradicts_itself():
    """An addition cannot both have no invoice and have one for the wrong amount."""
    for i in range(12):
        plan = random_plan(4, i)
        truth = plan.ground_truth()
        missing = {e["subject"] for e in truth if e["type"] == "missing_support"}
        variances = {e["subject"] for e in truth if e["type"] == "support_variance"}
        assert not (missing & variances)
        for error in truth:
            assert error["aliases"]


def test_a_fixture_is_consistent_except_where_it_was_corrupted():
    """Nothing is wrong by accident: the rollforwards balance, or the error is seeded."""
    for i in range(10):
        plan = random_plan(21, i)
        for c in plan.classes:
            stated = (c.opening_cost + plan.stated_additions(c)
                      - plan.stated_disposals(c))
            if stated != plan.true_closing(c):
                assert any(e["type"] == "rollforward_variance" and e["subject"] == c.name
                           for e in plan.ground_truth())
            # the accumulated-depreciation rollforward always balances
            assert plan.accum_closing(c) == (
                c.opening_accdep + plan.charge(c) - c.on_disposals)


def test_asset_names_are_unique_within_a_fixture():
    """A register names its assets; two assets with one name cannot be vouched apart."""
    for i in range(10):
        plan = random_plan(9, i)
        names = [a.description for a in plan.assets]
        assert len(names) == len(set(names))


def test_a_population_contains_clean_fixtures():
    """A population with no errors is what makes precision falsifiable."""
    clean = 0
    for i in range(40):
        if not random_plan(7, i).ground_truth():
            clean += 1
    assert clean > 0

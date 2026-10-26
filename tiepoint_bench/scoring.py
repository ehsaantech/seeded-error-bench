"""The scorer.

**The scoring rules are published, because a scorer that can flatter is not a scorer.**

- A finding **matches** a ground-truth error when its type is the same (after the
  published synonym map) and it is about the same thing — a reference (FA-0102,
  DISP-2026-003) if the finding names one, otherwise the normalised name. Names are
  never matched by containment: "Forklift" and "Forklift replacement" are different
  assets, and a matcher that treats one as the other is the false positive this
  benchmark exists to expose.
- A finding that matches an error nobody has claimed yet is a **true positive**, and
  marks that error **found**.
- A second finding on an error already found is a **duplicate**: not wrong, not enough.
  It is reported, and it is neither a true positive nor a false positive. (The ledger
  error is in the GL *and* in the trial balance, so a correct check reports it twice —
  that is one error and two reports, not two errors.)
- A finding that matches nothing is a **false positive**. So is every finding on a
  clean fixture — a population with no errors is what makes precision falsifiable.
- **precision** = found / (found + false positives); **recall** = found / seeded.
"""

from __future__ import annotations

import re

from .checks import Finding, _norm

# the published synonym map: a model's words for a type, folded onto the taxonomy
TYPE_SYNONYMS: dict[str, set[str]] = {
    "dep_variance": {"dep_variance", "depreciation_variance", "depreciation_error",
                     "depreciation", "over_depreciation", "under_depreciation",
                     "depreciation_charge"},
    "cost_variance": {"cost_variance", "ledger_variance", "gl_variance", "tb_variance",
                      "trial_balance_variance", "cost_difference", "variance"},
    "rollforward_variance": {"rollforward_variance", "rollforward_error", "rollforward",
                             "roll_forward_variance"},
    "prior_year_variance": {"prior_year_variance", "opening_balance_variance",
                            "prior_year", "opening_variance"},
    "missing_support": {"missing_support", "missing_invoice", "no_invoice", "unsupported",
                        "missing_supporting_invoice", "vouching"},
    "support_variance": {"support_variance", "invoice_variance", "amount_variance",
                         "supporting_amount_variance"},
    "missing_approval": {"missing_approval", "no_approval", "unapproved_disposal",
                         "disposal_approval", "missing_disposal_approval"},
}

_REF = re.compile(r"\b(FA|DISP|INV)[- ]?\d+(?:[- ]\d+)?\b", re.I)


def norm_type(kind: str) -> str:
    """A type, folded onto the taxonomy."""
    k = (kind or "").strip().casefold().replace(" ", "_").replace("-", "_")
    for canonical, names in TYPE_SYNONYMS.items():
        if k in names:
            return canonical
    return k


def keys(text: str) -> set[str]:
    """The identity of a subject: its reference if it names one, else its name."""
    refs = {m.group(0).upper().replace(" ", "-") for m in _REF.finditer(text or "")}
    if refs:
        return refs
    name = _norm(text or "")
    return {name} if name else set()


def same_subject(finding_subject: str, error: dict) -> bool:
    """Is the finding about the same thing as the error?"""
    aliases = error.get("aliases") or [error.get("subject", "")]
    fk = keys(finding_subject)
    if not fk:
        return False
    for alias in aliases:
        if fk & keys(alias):
            return True
    return False


def score(findings: list[Finding], errors: list[dict]) -> dict:
    """Score one arm's findings against one fixture's ground truth."""
    found: dict[int, Finding] = {}
    duplicates: list[Finding] = []
    false_positives: list[Finding] = []

    for finding in findings:
        kind = norm_type(finding.kind)
        hit = None
        for i, error in enumerate(errors):
            if error["type"] != kind:
                continue
            if same_subject(finding.subject, error):
                hit = i
                break
        if hit is None:
            false_positives.append(finding)
            continue
        if hit in found:
            duplicates.append(finding)
            continue
        found[hit] = finding

    missed = [error for i, error in enumerate(errors) if i not in found]
    tp = len(found)
    fp = len(false_positives)
    precision = (tp / (tp + fp)) if (tp + fp) else None
    recall = (tp / len(errors)) if errors else None
    f1 = None
    if precision is not None and recall is not None and (precision + recall):
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "found": tp, "missed": len(missed), "false_positives": fp,
        "duplicates": len(duplicates), "seeded": len(errors),
        "precision": precision, "recall": recall, "f1": f1,
        "missed_errors": missed, "false_positive_findings": false_positives,
    }

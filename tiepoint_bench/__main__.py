"""The public benchmark: generate the population, run the two arms, score them.

    python -m tiepoint_bench build --count 40 --seed 7
    python -m tiepoint_bench deterministic
    python -m tiepoint_bench llm --provider cloud --model glm-5.3-flash
    python -m tiepoint_bench table

Everything is synthetic. The population is drawn from a seed, so the same seed is the
same fixtures, byte for byte.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import scoring
from .checks import Config, Doc, Finding, read_policy, run_checks
from .fixtures import build, random_plan

DEFAULT_POPULATION = Path("population")


# ------------------------------------------------------------------ the population

@dataclass
class Fixture:
    id: str
    path: Path
    errors: list[dict] = field(default_factory=list)


@dataclass
class Population:
    root: Path
    seed: int
    count: int
    fixtures: list[Fixture] = field(default_factory=list)

    @property
    def seeded_errors(self) -> int:
        return sum(len(f.errors) for f in self.fixtures)

    @property
    def clean_fixtures(self) -> int:
        return sum(1 for f in self.fixtures if not f.errors)


def build_population(root: Path, count: int, seed: int) -> Population:
    """Write the population and its ground truth."""
    fixtures = []
    seeded = 0
    clean = 0
    for i in range(count):
        plan = random_plan(seed, i)
        truth = build(plan, root / f"fixture-{i:03d}")
        fixtures.append({"id": f"fixture-{i:03d}", "path": f"fixture-{i:03d}",
                         "errors": truth})
        seeded += len(truth)
        clean += 1 if not truth else 0
    meta = {"seed": seed, "count": count, "seeded_errors": seeded,
            "clean_fixtures": clean, "fixtures": fixtures}
    root.mkdir(parents=True, exist_ok=True)
    (root / "ground_truth.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return Population(root=root, seed=seed, count=count,
                      fixtures=[Fixture(id=f["id"], path=root / f["path"],
                                        errors=f["errors"]) for f in fixtures])


def load_population(root: Path) -> Population:
    meta = json.loads((root / "ground_truth.json").read_text(encoding="utf-8"))
    return Population(root=root, seed=meta.get("seed", 0), count=meta.get("count", 0),
                      fixtures=[Fixture(id=e["id"], path=root / e["path"],
                                        errors=e.get("errors", []))
                                for e in meta["fixtures"]])


def docs_for(fixture: Fixture) -> list[Doc]:
    """Every document in a fixture, routed the way the engine routes them."""
    docs = []
    for path in sorted(fixture.path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(fixture.path)
        request = rel.parts[0] if len(rel.parts) > 1 else ""
        docs.append(Doc(name=path.name, path=path, request=request))
    return docs


# ------------------------------------------------------------------ the arms

@dataclass
class ArmResult:
    arm: str
    model: str = ""
    found: int = 0
    missed: int = 0
    false_positives: int = 0
    duplicates: int = 0
    seeded: int = 0
    clean_fixtures: int = 0
    findings_on_clean: int = 0
    failed_fixtures: int = 0
    failed_seeded: int = 0
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    located_findings: int = 0
    unlocated_findings: int = 0
    times: list[float] = field(default_factory=list)
    missed_examples: list[dict] = field(default_factory=list)
    false_positive_examples: list[dict] = field(default_factory=list)
    per_fixture: list[dict] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        denom = self.found + self.false_positives
        return (self.found / denom) if denom else None

    @property
    def recall(self) -> float | None:
        """Recall over the fixtures this arm answered.

        A fixture the harness could not get an answer for is not a miss — the model
        was never asked. It is reported separately, and the count is here so the
        number can be read either way.
        """
        answered = self.seeded - self.failed_seeded
        return (self.found / answered) if answered else None

    @property
    def recall_counting_failures_as_misses(self) -> float | None:
        """The pessimistic reading: a fixture the harness could not answer counts as
        a miss. Both numbers are published."""
        return (self.found / self.seeded) if self.seeded else None

    @property
    def median_seconds(self) -> float | None:
        return statistics.median(self.times) if self.times else None

    def as_dict(self) -> dict:
        return {
            "arm": self.arm, "model": self.model, "seeded": self.seeded, "found": self.found,
            "missed": self.missed, "false_positives": self.false_positives,
            "duplicates": self.duplicates, "clean_fixtures": self.clean_fixtures,
            "findings_on_clean": self.findings_on_clean,
            "failed_fixtures": self.failed_fixtures,
            "failed_seeded": self.failed_seeded,
            "recall_counting_failures_as_misses": self.recall_counting_failures_as_misses,
            "model_calls": self.model_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "precision": self.precision, "recall": self.recall,
            "median_seconds": self.median_seconds,
            "located_findings": self.located_findings,
            "unlocated_findings": self.unlocated_findings,
            "per_fixture": self.per_fixture,
            "missed_examples": self.missed_examples,
            "false_positive_examples": self.false_positive_examples,
        }


def run_deterministic(population: Population) -> ArmResult:
    """The rules. No model call, ever."""
    result = ArmResult(arm="deterministic")
    for fixture in population.fixtures:
        docs = docs_for(fixture)
        started = time.time()
        policy = next((d for d in docs if d.request == "FA-04"), None)
        rates = read_policy(policy) if policy else {}
        findings = run_checks(docs, Config(rates=rates))
        elapsed = time.time() - started
        result.times.append(elapsed)
        _tally(result, fixture, findings, elapsed)
    return result


def run_llm(population: Population, model: str | None = None, provider: str | None = None,
            endpoint: str | None = None, api_key: str | None = None) -> ArmResult:
    """The model, on the same documents."""
    from .llm import run_llm_arm

    return run_llm_arm(population, model=model, provider=provider, endpoint=endpoint,
                       api_key=api_key)


def _tally(result: ArmResult, fixture: Fixture, findings: list[Finding],
           elapsed: float) -> None:
    s = scoring.score(findings, fixture.errors)
    result.seeded += s["seeded"]
    result.found += s["found"]
    result.missed += s["missed"]
    result.false_positives += s["false_positives"]
    result.duplicates += s["duplicates"]
    if not fixture.errors:
        result.clean_fixtures += 1
        result.findings_on_clean += len(findings)
    for finding in findings:
        if _is_located(finding):
            result.located_findings += 1
        else:
            result.unlocated_findings += 1
    for error in s["missed_errors"]:
        if len(result.missed_examples) < 10:
            result.missed_examples.append({"fixture": fixture.id, **error})
    for finding in s["false_positive_findings"]:
        if len(result.false_positive_examples) < 10:
            result.false_positive_examples.append(
                {"fixture": fixture.id, "kind": finding.kind, "subject": finding.subject,
                 "detail": finding.detail})
    result.per_fixture.append({
        "fixture": fixture.id, "seeded": s["seeded"], "found": s["found"],
        "missed": s["missed"], "false_positives": s["false_positives"],
        "duplicates": s["duplicates"], "seconds": round(elapsed, 3),
    })


_CELL = re.compile(r"\b[A-Z]{1,2}\d{1,4}\b")


def _is_located(finding: Finding) -> bool:
    """Does the finding point at a place?

    A deterministic finding always does — it carries the cell it came from, or the
    document and the field within it. A model finding does only when it names a cell:
    "the register disagrees with the GL" names two files, which is a place to start
    looking, not a place to sign.
    """
    loc = finding.locator or {}
    kind = loc.get("kind")
    if kind in ("cell", "cells", "row", "page", "bbox"):
        return True
    if kind == "text":
        label = loc.get("label") or ""
        if _CELL.search(label):
            return True
        return bool(loc.get("file")) and loc.get("file") != "model finding" and bool(label)
    return False


# ------------------------------------------------------------------ the table

def auditor_minutes(result: ArmResult, located_minutes: int = 2,
                     unlocated_minutes: int = 8) -> int:
    """The minutes an auditor would spend clearing this arm's queue.

    An assumption, stated so it can be re-computed: a located, typed finding takes
    `located_minutes`; a claim without a locator takes `unlocated_minutes`. The false
    positives are in the queue too — an auditor still has to dismiss them.
    """
    return int(round(result.located_findings * located_minutes
                     + result.unlocated_findings * unlocated_minutes))


def render_table(deterministic: ArmResult | None, llm: ArmResult | None,
                 population: Population, located_minutes: int = 2,
                 unlocated_minutes: int = 8) -> str:
    """The table. Every number in it is measured."""

    def pct(v) -> str:
        return "—" if v is None else f"{v * 100:.1f}%"

    def num(v) -> str:
        return "—" if v is None else (f"{v:.2f}s" if isinstance(v, float) else str(v))

    d, l = deterministic, llm
    model = (l.model if l is not None and l.model else "the model")
    lines = [
        "SEEDED-ERROR BENCHMARK — fixed assets, one template, synthetic population",
        f"population: {len(population.fixtures)} fixtures · {population.seeded_errors} "
        f"seeded errors · {population.clean_fixtures} clean fixtures · seed {population.seed}",
        "",
    ]
    header = f"{'':38} {'Deterministic (rules)':>24} {f'LLM ({model})':>24}"
    lines.append(header)
    lines.append("-" * len(header))

    def row(label: str, dv, lv) -> None:
        lines.append(f"{label:38} {str(dv):>24} {str(lv):>24}")

    if d and l:
        row("Seeded errors found", f"{d.found} / {d.seeded}", f"{l.found} / {l.seeded}")
        row("Missed", d.missed, l.missed)
        row("False positives", d.false_positives, l.false_positives)
        row("  of which on clean fixtures", d.findings_on_clean, l.findings_on_clean)
        row("Duplicate reports (same error twice)", d.duplicates, l.duplicates)
        row("Fixtures the arm could not answer", d.failed_fixtures, l.failed_fixtures)
        row("Precision", pct(d.precision), pct(l.precision))
        row("Recall", pct(d.recall), pct(l.recall))
        if l.failed_fixtures:
            row("  if the unanswered count as misses", "—", pct(l.recall_counting_failures_as_misses))
        row("Wall time per fixture (median)", num(d.median_seconds), num(l.median_seconds))
        row("Model calls", 0, l.model_calls)
        row("Tokens (prompt / completion)", "0 / 0", f"{l.prompt_tokens} / {l.completion_tokens}")
        row("Queue items (located / unlocated)",
            f"{d.located_findings} / {d.unlocated_findings}",
            f"{l.located_findings} / {l.unlocated_findings}")
        row("Auditor-minutes to clear the queue",
            auditor_minutes(d, located_minutes, unlocated_minutes),
            auditor_minutes(l, located_minutes, unlocated_minutes))
    elif d:
        row("Seeded errors found", f"{d.found} / {d.seeded}", "—")
        row("Missed", d.missed, "—")
        row("False positives", d.false_positives, "—")
        row("Precision", pct(d.precision), "—")
        row("Recall", pct(d.recall), "—")
        row("Wall time per fixture (median)", num(d.median_seconds), "—")
    elif l:
        row("Seeded errors found", "—", f"{l.found} / {l.seeded}")
        row("Missed", "—", l.missed)
        row("False positives", "—", l.false_positives)
        row("Precision", "—", pct(l.precision))
        row("Recall", "—", pct(l.recall))
        row("Wall time per fixture (median)", "—", num(l.median_seconds))
    lines.append("")
    lines.append(f"auditor-minutes assumes {located_minutes} min per located finding and "
                 f"{unlocated_minutes} min per unlocated claim — an assumption, stated so "
                 f"the row can be re-computed.")
    if llm is not None and llm.failed_fixtures:
        lines.append(f"{llm.failed_fixtures} fixture(s) the harness could not get an answer "
                     f"for ({llm.failed_seeded} seeded errors) are not counted as misses: "
                     f"the model was never asked. The row above counts them as misses, "
                     f"so the number can be read either way.")
    return "\n".join(lines)


# ------------------------------------------------------------------ the command

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tiepoint_bench", description=__doc__.splitlines()[0])
    ap.add_argument("--population", default=str(DEFAULT_POPULATION))
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="write the population and its ground truth")
    b.add_argument("--count", type=int, default=40)
    b.add_argument("--seed", type=int, default=7)

    sub.add_parser("deterministic", help="run the rules over the population")

    lm = sub.add_parser("llm", help="run the model over the population")
    lm.add_argument("--model", default="glm-5.3-flash")
    lm.add_argument("--provider", choices=["local", "cloud"], default="cloud")
    lm.add_argument("--endpoint", default=None, help="an OpenAI-compatible endpoint")
    lm.add_argument("--api-key", default=None)

    tb = sub.add_parser("table", help="print the table from a run record")
    tb.add_argument("--record", default=None)

    args = ap.parse_args(argv)
    root = Path(args.population)

    if args.cmd == "build":
        population = build_population(root, args.count, args.seed)
        print(f"population written to {root}: {population.count} fixtures · "
              f"{population.seeded_errors} seeded errors · "
              f"{population.clean_fixtures} clean fixtures")
        return 0

    if args.cmd == "deterministic":
        population = load_population(root)
        result = run_deterministic(population)
        print(render_table(result, None, population))
        return 0

    if args.cmd == "llm":
        population = load_population(root)
        result = run_llm(population, model=args.model, provider=args.provider,
                          endpoint=args.endpoint, api_key=args.api_key)
        print(render_table(None, result, population))
        return 0

    if args.cmd == "table":
        record = json.loads(Path(args.record).read_text(encoding="utf-8"))
        print(record.get("table", ""))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())

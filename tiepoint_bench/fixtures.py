"""The fixture generator — parameterised, with ground truth.

A fixture is a synthetic fixed-asset engagement: a register, a prior-year rollforward,
a GL extract, a trial balance, a depreciation policy, and the invoices and disposal
approvals that support the register's movements. Everything is fake; the shapes are
real.

**Errors are seeded deliberately and are the only errors present.** The generator
computes the truth first, then corrupts it in named ways, then writes down what the
truth was — that list is the ground truth a benchmark is scored against. A fixture
that is inconsistent by accident proves nothing; a fixture with no errors proves
even less, which is why the generator can also build a *clean* population.

**Why parameterised.** One fixture with four errors is a unit test. A population
drawn from a seed, each fixture with its own ground truth, is a benchmark: the checks
can be measured on recall (what did they miss?) and on precision (what did they
raise that was not there?).

**The corruption model.** Each class carries the truth (opening, movements, rate).
A corruption changes one stated figure and nothing else:

- a wrong depreciation charge (the policy check fires),
- a ledger that is off the register (the tie fires — in the GL *and* in the trial
  balance, which is one error and two findings),
- movements that do not reconcile to the closing cost (the rollforward check fires),
- a prior-year file that is off the opening balance (the prior-year check fires),
- an addition with no invoice, or an invoice for the wrong amount (vouching fires),
- a disposal with no approved document (vouching fires).

Nothing cascades: the register's charge stays policy-correct against its own closing
cost, so one seeded error is one true finding, and the ground truth never has to
guess what else a corruption would have caused.

The canonical fixture — the one the demo runs on — is this generator with one
hand-written plan: `samples/make_dry_run_data.py` writes it.

Run:  python samples/make_bench_fixtures.py --count 40 --seed 7
"""

from __future__ import annotations

import random
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font

# ------------------------------------------------------------------ the firm's checklist

# The error taxonomy: the checklist a junior auditor is given, and the ground truth
# both arms of the benchmark are scored against.
KINDS: dict[str, str] = {
    "dep_variance": "the depreciation charge for a class is not the policy charge "
                    "(rate x closing cost)",
    "cost_variance": "the ledger (GL or trial balance) does not agree with the "
                     "register's closing cost for a class",
    "rollforward_variance": "the register's own rollforward does not balance "
                            "(opening + additions - disposals != closing)",
    "prior_year_variance": "the opening balance does not agree with the prior-year "
                           "closing balance",
    "missing_support": "an addition in the register has no supporting invoice",
    "support_variance": "the invoice for an addition states a different amount from "
                        "the register",
    "missing_approval": "a disposal in the register has no approved disposal document",
}

CLASS_POOL = ["Buildings", "Plant & Machinery", "Office Equipment", "Motor Vehicles"]
RATES = {"Buildings": 0.02, "Plant & Machinery": 0.15,
         "Office Equipment": 0.20, "Motor Vehicles": 0.25}

HEAD = Font(bold=True)

# A fixed timestamp, so the same fixture is the same bytes. Regenerating the fixture
# must not change its hash: the demo byte-checks it, and a published benchmark is only
# reproducible if the fixtures are.
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
FIXED_DT = datetime(2026, 1, 1)


def _stabilise(path: Path) -> None:
    """Rewrite the archive with fixed timestamps.

    Both openpyxl and python-docx stamp the current time into the file, so a
    regenerated fixture would have a different hash every run. The content is
    identical; only the clock is noise.

    The timestamps inside the document properties are pinned as well: openpyxl
    writes `dcterms:modified` at save time, whatever the workbook was told.
    """
    with zipfile.ZipFile(path) as zf:
        items = [(i.filename, zf.read(i.filename)) for i in zf.infolist()]
    pinned = [(name, _pinned_core(data) if name == "docProps/core.xml" else data)
              for name, data in items]
    tmp = path.with_name(path.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        for name, data in pinned:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            out.writestr(info, data)
    tmp.replace(path)


def _pinned_core(data: bytes) -> bytes:
    """The document properties carry the clock. Pin them to the fixed timestamp."""
    import re

    text = data.decode("utf-8", "ignore")
    text = re.sub(r"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)",
                  r"\g<1>2026-01-01T00:00:00Z\g<2>", text)
    return text.encode("utf-8")


def _stable_workbook(wb: Workbook, path: Path) -> None:
    wb.properties.created = FIXED_DT
    wb.properties.modified = FIXED_DT
    wb.properties.creator = "tiepoint fixture generator"
    wb.save(path)
    _stabilise(path)


def _stable_document(doc: Document, path: Path) -> None:
    doc.core_properties.created = FIXED_DT
    doc.core_properties.modified = FIXED_DT
    doc.core_properties.author = "tiepoint fixture generator"
    doc.save(path)
    _stabilise(path)


# ------------------------------------------------------------------ the plan

@dataclass
class Asset:
    id: str
    description: str
    asset_class: str
    cost: int
    acquired: str
    status: str                 # opening | addition | disposed
    accum_dep: int = 0          # accumulated depreciation relieved on disposal


@dataclass
class Class:
    """One asset class: the truth."""
    name: str
    rate: float
    opening_cost: int
    additions: int
    disposals: int
    opening_accdep: int
    on_disposals: int           # accumulated depreciation relieved on disposals


@dataclass
class Plan:
    """One fixture: the truth, plus the named ways it is corrupted.

    The overrides are the corruptions. Everything not overridden is consistent by
    construction, so every difference between the fixture and the truth is a seeded
    error — and the ground truth below says what it is.
    """
    name: str
    classes: list[Class]
    assets: list[Asset]
    # class -> the movements the register states (a rollforward error states wrong ones)
    additions_override: dict[str, int] = field(default_factory=dict)
    disposals_override: dict[str, int] = field(default_factory=dict)
    # class -> the wrong depreciation charge (a policy error)
    charged_override: dict[str, int] = field(default_factory=dict)
    # class -> how far the ledger is off the register's closing cost
    gl_delta: dict[str, int] = field(default_factory=dict)
    tb_delta: dict[str, int] = field(default_factory=dict)
    # class -> how far the prior-year closing balance is off the opening balance
    prior_delta: dict[str, int] = field(default_factory=dict)
    # asset id -> the addition has no invoice at all
    missing_support: list[str] = field(default_factory=list)
    # asset id -> the invoice states this amount instead of the register's cost
    support_amount: dict[str, int] = field(default_factory=dict)
    # asset id -> the disposal document says this (the canonical says "Ute — sold to
    # employee", which is how a disposal record reads)
    disposal_text: dict[str, str] = field(default_factory=dict)
    # asset id -> proceeds on the disposal
    proceeds_by_asset: dict[str, int] = field(default_factory=dict)
    # asset id -> the document reference it carries ("INV-2026-0038", "DISP-2026-001")
    refs: dict[str, str] = field(default_factory=dict)
    # disposal ref -> the document exists but carries no approval
    unapproved: list[str] = field(default_factory=list)
    # asset id -> the disposal has no document at all
    no_disposal_doc: list[str] = field(default_factory=list)

    # ---- the truth, as stated --------------------------------------------

    def true_closing(self, c: Class) -> int:
        return c.opening_cost + c.additions - c.disposals

    def stated_additions(self, c: Class) -> int:
        return self.additions_override.get(c.name, c.additions)

    def stated_disposals(self, c: Class) -> int:
        return self.disposals_override.get(c.name, c.disposals)

    def charge(self, c: Class) -> int:
        return self.charged_override.get(c.name, round(self.true_closing(c) * c.rate))

    def accum_closing(self, c: Class) -> int:
        return c.opening_accdep + self.charge(c) - c.on_disposals

    def prior_closing(self, c: Class) -> int:
        return c.opening_cost + self.prior_delta.get(c.name, 0)

    def gl_closing(self, c: Class) -> int:
        return self.true_closing(c) + self.gl_delta.get(c.name, 0)

    def tb_closing(self, c: Class) -> int:
        return self.true_closing(c) + self.tb_delta.get(c.name, 0)

    def class_by_name(self, name: str) -> Class | None:
        return next((c for c in self.classes if c.name == name), None)

    def total_charge(self) -> int:
        return sum(self.charge(c) for c in self.classes)

    def loss_on_disposal(self) -> int:
        """Net book value of the disposals less the proceeds (the canonical: 37,000)."""
        loss = 0
        for a in self.assets:
            if a.status != "disposed":
                continue
            loss += (a.cost - a.accum_dep) - self.proceeds_for(a)
        return loss

    def proceeds_for(self, asset: Asset) -> int:
        return self.proceeds_by_asset.get(asset.id, 0)

    # ---- the documents ---------------------------------------------------

    def invoice_ref(self, asset: Asset) -> str:
        if asset.id in self.refs:
            return self.refs[asset.id]
        return f"INV-2026-{[a.id for a in self.assets].index(asset.id) + 1:04d}"

    def disposal_ref(self, asset: Asset) -> str:
        if asset.id in self.refs:
            return self.refs[asset.id]
        return f"DISP-2026-{[a.id for a in self.assets].index(asset.id) + 1:03d}"

    def invoice_documents(self) -> list[dict]:
        """The invoices, in the order they are written."""
        docs = []
        for a in self.assets:
            if a.status != "addition" or a.id in self.missing_support:
                continue
            docs.append({
                "ref": self.invoice_ref(a),
                "asset_id": a.id,
                "description": a.description,
                "amount": self.support_amount.get(a.id, a.cost),
            })
        return docs

    def disposal_documents(self) -> list[dict]:
        """The disposal documents, in the order they are written."""
        docs = []
        for a in self.assets:
            if a.status != "disposed" or a.id in self.no_disposal_doc:
                continue
            ref = self.disposal_ref(a)
            docs.append({
                "ref": ref,
                "asset_id": a.id,
                "description": self.disposal_text.get(a.id, a.description),
                "proceeds": self.proceeds_for(a),
                "approved": ref not in self.unapproved,
            })
        docs.sort(key=lambda d: d["ref"])
        return docs

    # ---- ground truth ----------------------------------------------------

    def ground_truth(self) -> list[dict]:
        """Every true statement about this fixture's errors, by construction.

        One seeded error is one true finding. The cost error is the exception: the
        ledger is wrong in the GL *and* in the trial balance, so the deterministic
        checks find it twice — one true finding, two reports. The scorer treats the
        second as a duplicate, not as a false positive.
        """
        out: list[dict] = []
        n = 0

        def add(kind: str, subject: str, amount: int | None, detail: str,
                aliases: list[str] | None = None) -> None:
            nonlocal n
            n += 1
            out.append({"id": f"E{n:02d}", "type": kind, "subject": subject,
                        "amount": amount, "detail": detail,
                        "aliases": aliases or [subject]})

        for c in self.classes:
            stated = c.opening_cost + self.stated_additions(c) - self.stated_disposals(c)
            if stated != self.true_closing(c):
                add("rollforward_variance", c.name, stated - self.true_closing(c),
                    f"{c.name}: opening {c.opening_cost:,} + additions "
                    f"{self.stated_additions(c):,} - disposals "
                    f"{self.stated_disposals(c):,} = {stated:,}, but the register "
                    f"states {self.true_closing(c):,}")
            if c.name in self.charged_override:
                correct = round(self.true_closing(c) * c.rate)
                add("dep_variance", c.name, correct - self.charge(c),
                    f"{c.name}: charged {self.charge(c):,}, policy requires {correct:,} "
                    f"({c.rate:.0%} x {self.true_closing(c):,})")
            if self.gl_delta.get(c.name):
                add("cost_variance", c.name, abs(self.gl_delta[c.name]),
                    f"{c.name}: ledger closing {self.gl_closing(c):,} vs register "
                    f"{self.true_closing(c):,}")
            if self.prior_delta.get(c.name):
                add("prior_year_variance", c.name, abs(self.prior_delta[c.name]),
                    f"{c.name}: prior-year closing {self.prior_closing(c):,} vs opening "
                    f"{c.opening_cost:,}")
        for a in self.assets:
            if a.id in self.missing_support:
                add("missing_support", a.id, a.cost,
                    f"{a.id} {a.description} ({a.cost:,}) has no supporting invoice",
                    aliases=[a.id, a.description])
            if a.id in self.support_amount:
                add("support_variance", a.id,
                    abs(self.support_amount[a.id] - a.cost),
                    f"{a.id} {a.description}: invoice states "
                    f"{self.support_amount[a.id]:,}, register states {a.cost:,}",
                    aliases=[a.id, a.description])
        for d in self.disposal_documents():
            if not d["approved"]:
                add("missing_approval", d["ref"], None,
                    f"disposal {d['ref']} ({d['description']}) carries no approval",
                    aliases=[d["ref"], d["description"]])
        for a in self.assets:
            if a.status == "disposed" and a.id in self.no_disposal_doc:
                add("missing_approval", a.id, None,
                    f"the disposal of {a.id} {a.description} has no disposal document",
                    aliases=[a.id, a.description])
        return out


# ------------------------------------------------------------------ writing

def _sheet(ws, title, headers, rows):
    ws.title = title
    for c, h in enumerate(headers, 1):
        ws.cell(1, c, h).font = HEAD
    for r, row in enumerate(rows, 2):
        for c, v in enumerate(row, 1):
            ws.cell(r, c, v)
    for c in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + c)].width = 22


def write_register(plan: Plan, out: Path) -> None:
    wb = Workbook()
    ws = wb.active
    rows = [[c.name, c.opening_cost, plan.stated_additions(c), plan.stated_disposals(c),
             plan.true_closing(c)] for c in plan.classes]
    rows.append(["TOTAL",
                 sum(c.opening_cost for c in plan.classes),
                 sum(plan.stated_additions(c) for c in plan.classes),
                 sum(plan.stated_disposals(c) for c in plan.classes),
                 sum(plan.true_closing(c) for c in plan.classes)])
    _sheet(ws, "Cost Rollforward",
           ["Asset class", "Opening cost", "Additions", "Disposals", "Closing cost"], rows)

    ws2 = wb.create_sheet("Depreciation Rollforward")
    rows2 = [[c.name, c.opening_accdep, plan.charge(c), c.on_disposals, plan.accum_closing(c)]
             for c in plan.classes]
    rows2.append(["TOTAL",
                  sum(c.opening_accdep for c in plan.classes),
                  plan.total_charge(),
                  sum(c.on_disposals for c in plan.classes),
                  sum(plan.accum_closing(c) for c in plan.classes)])
    _sheet(ws2, "Depreciation Rollforward",
           ["Asset class", "Opening acc dep", "Charge for year", "On disposals",
            "Closing acc dep"], rows2)

    ws3 = wb.create_sheet("Asset List")
    _sheet(ws3, "Asset List",
           ["Asset ID", "Description", "Class", "Cost", "Acquisition date", "Status"],
           [[a.id, a.description, a.asset_class, a.cost, a.acquired, a.status]
            for a in plan.assets])
    _stable_workbook(wb, out / "FA-01 fixed asset register FY26.xlsx")


def write_gl(plan: Plan, out: Path) -> None:
    """The GL extract.

    The ledger carries the truth: its own opening + movements = its closing. A cost
    error is the ledger's closing being off the register — so 'Closing (calc)' (the
    ledger's arithmetic) and 'Closing (GL)' (what it states) differ.
    """
    wb = Workbook()
    ws = wb.active
    rows = []
    code = 4100
    for c in plan.classes:
        rows.append((str(code), f"PPE — cost — {c.name}", c.opening_cost, c.additions,
                     c.disposals, plan.true_closing(c), plan.gl_closing(c)))
        code += 10
    code = 4200
    for c in plan.classes:
        rows.append((str(code), f"Acc dep — {c.name}", c.opening_accdep, plan.charge(c),
                     c.on_disposals, plan.accum_closing(c), plan.accum_closing(c)))
        code += 10
    rows.append(("5000", "Depreciation expense", 0, plan.total_charge(), 0,
                 plan.total_charge(), plan.total_charge()))
    rows.append(("5100", "Loss on disposal", 0, plan.loss_on_disposal(), 0,
                 plan.loss_on_disposal(), plan.loss_on_disposal()))
    _sheet(ws, "GL Detail",
           ["Account", "Description", "Opening", "Movement Dr", "Movement Cr",
            "Closing (calc)", "Closing (GL)"], rows)
    _stable_workbook(wb, out / "FA-06 GL extract PPE.xlsx")


def write_tb(plan: Plan, out: Path) -> None:
    wb = Workbook()
    ws = wb.active
    debits = []
    code = 4100
    for c in plan.classes:
        debits.append((str(code), f"PPE — cost — {c.name}", plan.tb_closing(c), 0))
        code += 10
    debits.append(("5000", "Depreciation expense", plan.total_charge(), 0))
    debits.append(("5100", "Loss on disposal", plan.loss_on_disposal(), 0))
    credits = []
    code = 4200
    for c in plan.classes:
        credits.append((str(code), f"Acc dep — {c.name}", 0, plan.accum_closing(c)))
        code += 10
    dr = sum(r[2] for r in debits)
    cr = sum(r[3] for r in credits)
    cash_debit = 900_000
    retained_credit = cash_debit + dr - cr
    rows = (debits + [("1000", "Cash", cash_debit, 0)]
            + credits + [("3000", "Share capital", 0, 1_000_000),
                         ("3900", "Retained earnings", 0, retained_credit)])
    _sheet(ws, "TB", ["Account", "Description", "Debit", "Credit"], rows)
    _stable_workbook(wb, out / "FA-07 trial balance FY26.xlsx")


def write_prior(plan: Plan, out: Path) -> None:
    wb = Workbook()
    ws = wb.active
    rows = [[c.name, plan.prior_closing(c), c.opening_accdep,
             plan.prior_closing(c) - c.opening_accdep] for c in plan.classes]
    _sheet(ws, "FY25 Rollforward",
           ["Asset class", "Closing cost", "Closing acc dep", "Closing NBV"], rows)
    _stable_workbook(wb, out / "FA-05 prior year rollforward FY25.xlsx")


def write_policy(plan: Plan, out: Path) -> None:
    doc = Document()
    doc.add_heading("Depreciation Policy — FY2026", level=1)
    doc.add_paragraph("This policy applies to all property, plant and equipment.")
    doc.add_heading("Methods and rates", level=2)
    t = doc.add_table(rows=1, cols=3)
    h = t.rows[0].cells
    h[0].text, h[1].text, h[2].text = "Asset class", "Method", "Annual rate"
    for c in plan.classes:
        row = t.add_row().cells
        row[0].text, row[1].text, row[2].text = c.name, "Straight line", f"{c.rate * 100:.0f}%"
    doc.add_heading("Convention", level=2)
    doc.add_paragraph(
        "A full year's charge is applied in the year of acquisition. No charge is applied "
        "in the year of disposal. Charge is calculated on closing cost for the class.")
    (out / "FA-04").mkdir(parents=True, exist_ok=True)
    _stable_document(doc, out / "FA-04" / "FA-04 depreciation policy FY26.docx")


def write_support(plan: Plan, out: Path) -> None:
    add = out / "FA-02"
    disp = out / "FA-03"
    for d in plan.invoice_documents():
        doc = Document()
        doc.add_heading(f"Tax invoice {d['ref']}", level=1)
        doc.add_paragraph(f"Description: {d['description']}")
        doc.add_paragraph(f"Amount (ex GST): AUD {d['amount']:,.2f}")
        doc.add_paragraph("Capitalised: yes")
        _stable_document(doc, add / f"{d['ref']} {d['description']}.docx")
    for d in plan.disposal_documents():
        doc = Document()
        doc.add_heading(f"Disposal {d['ref']}", level=1)
        doc.add_paragraph(f"Asset: {d['description']}")
        doc.add_paragraph(f"Proceeds: AUD {d['proceeds']:,.2f}")
        if d["approved"]:
            doc.add_paragraph("Approved by: Board resolution BR-2026-04")
        _stable_document(doc, disp / f"{d['ref']} {d['description'].split('—')[0].strip()}.docx")


def _check_plan(plan: Plan) -> None:
    """The plan must be internally consistent before it is written.

    A fixture that is wrong by accident proves nothing, and the errors that are
    seeded must be the *only* errors — so the generator checks its own arithmetic
    every time, and refuses to write a plan that does not add up.
    """
    for c in plan.classes:
        assert c.on_disposals == sum(
            a.accum_dep for a in plan.assets
            if a.asset_class == c.name and a.status == "disposed"), \
            f"{c.name}: the class's on-disposals does not match the assets"
        assert plan.accum_closing(c) == (
            c.opening_accdep + plan.charge(c) - c.on_disposals), \
            f"{c.name}: the accumulated-depreciation rollforward does not balance"
        # the register's own movements, and the ground truth, agree
        stated = (c.opening_cost + plan.stated_additions(c) - plan.stated_disposals(c))
        truth = {e["type"] for e in plan.ground_truth() if e["subject"] == c.name}
        if stated != plan.true_closing(c):
            assert "rollforward_variance" in truth, \
                f"{c.name}: the rollforward does not balance and no error says so"
        else:
            assert "rollforward_variance" not in truth


def build(plan: Plan, out: Path) -> list[dict]:
    """Write one fixture and return its ground truth.

    The truth is returned, not written into the folder: a fixture folder is evidence,
    and evidence is what gets ingested. A ground-truth file sitting in the middle of it
    would be ingested with it.
    """
    _check_plan(plan)
    _clean(out)
    for d in (out, out / "FA-02", out / "FA-03"):
        d.mkdir(parents=True, exist_ok=True)
    write_register(plan, out)
    write_gl(plan, out)
    write_tb(plan, out)
    write_prior(plan, out)
    write_policy(plan, out)
    write_support(plan, out)
    return plan.ground_truth()


# the artefacts a fixture is made of — the only things `build` will remove
_ARTEFACTS = ("FA-01 fixed asset register FY26.xlsx",
              "FA-05 prior year rollforward FY25.xlsx",
              "FA-06 GL extract PPE.xlsx",
              "FA-07 trial balance FY26.xlsx",
              "FA-04 depreciation policy FY26.docx",
              "FA-02", "FA-03", "FA-04", "ground_truth.json")


def _clean(out: Path) -> None:
    """Remove the previous build of this fixture, and nothing else."""
    import shutil

    for name in _ARTEFACTS:
        p = out / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()


# ------------------------------------------------------------------ the canonical plan

def canonical_plan() -> Plan:
    """The demo fixture: four seeded errors, exactly as `SEEDED.md` describes them."""
    classes = [
        Class("Buildings", 0.02, 1_200_000, 0, 0, 240_000, 0),
        Class("Plant & Machinery", 0.15, 520_000, 265_000, 55_000, 210_000, 22_000),
        Class("Office Equipment", 0.20, 180_000, 95_000, 0, 120_000, 0),
        Class("Motor Vehicles", 0.25, 100_000, 45_000, 25_000, 30_000, 9_000),
    ]
    assets = [
        Asset("FA-0001", "Head office", "Buildings", 900_000, "2001-07-01", "opening"),
        Asset("FA-0002", "Warehouse", "Buildings", 300_000, "2012-03-15", "opening"),
        Asset("FA-0101", "CNC machine 1", "Plant & Machinery", 210_000, "2019-04-01", "opening"),
        Asset("FA-0102", "CNC machine 2", "Plant & Machinery", 210_000, "2026-02-10", "addition"),
        Asset("FA-0103", "Forklift", "Plant & Machinery", 55_000, "2018-09-01", "disposed",
              accum_dep=22_000),
        Asset("FA-0150", "Forklift replacement", "Plant & Machinery", 55_000, "2026-05-20", "addition"),
        Asset("FA-0104", "Conveyor system", "Plant & Machinery", 145_000, "2016-08-01", "opening"),
        Asset("FA-0105", "Press machine", "Plant & Machinery", 110_000, "2014-02-01", "opening"),
        Asset("FA-0201", "Laptops (25 units)", "Office Equipment", 95_000, "2026-01-15", "addition"),
        Asset("FA-0202", "Office furniture", "Office Equipment", 85_000, "2017-06-01", "opening"),
        Asset("FA-0301", "Delivery van", "Motor Vehicles", 45_000, "2026-03-01", "addition"),
        Asset("FA-0302", "Ute", "Motor Vehicles", 25_000, "2016-11-01", "disposed",
              accum_dep=9_000),
    ]
    plan = Plan(name="dry-run", classes=classes, assets=assets)
    # SEEDED ERROR 1 — Office Equipment is charged 3,000 less than policy requires
    plan.charged_override["Office Equipment"] = 52_000
    # SEEDED ERROR 2 — the ledger is 2,000 short of the register on Office Equipment
    plan.gl_delta["Office Equipment"] = -2_000
    plan.tb_delta["Office Equipment"] = -2_000
    # SEEDED ERROR 3 — no invoice references FA-0102 (CNC machine 2, 210,000)
    plan.missing_support = ["FA-0102"]
    # SEEDED ERROR 4 — DISP-2026-003 (Forklift) carries no approval
    plan.unapproved = ["DISP-2026-003"]
    plan.proceeds_by_asset = {"FA-0302": 12_000, "FA-0103": 0}
    plan.disposal_text = {"FA-0302": "Ute — sold to employee",
                          "FA-0103": "Forklift — traded in"}
    plan.refs = {"FA-0302": "DISP-2026-001", "FA-0103": "DISP-2026-003",
                 "FA-0150": "INV-2026-0204", "FA-0201": "INV-2026-0038",
                 "FA-0301": "INV-2026-0147"}
    return plan


# ------------------------------------------------------------------ a population

# Names for generated assets. The "Forklift" / "Forklift replacement" pair is the trap:
# a matcher doing substring matching raises a false positive on the wrong asset, or
# misses the real one.
_ASSET_NAMES = ["CNC machine", "Press machine", "Conveyor system", "Forklift",
                "Delivery van", "Ute", "Office furniture", "Laptops",
                "Server rack", "Air conditioner", "Lathe", "Excavator"]
_OPENING_DATES = ["2003-04-01", "2007-09-15", "2011-02-28", "2014-06-30",
                   "2016-11-01", "2018-05-12", "2019-08-20", "2021-03-07"]
_ADDITION_DATES = ["2026-01-15", "2026-02-10", "2026-03-01", "2026-04-22", "2026-05-20"]


def _asset_id(cls_index: int, n: int) -> str:
    return f"FA-{cls_index:02d}{n:02d}"


def random_plan(seed: int, index: int, name: str | None = None) -> Plan:
    """One fixture from a seed. Deterministic: the same (seed, index) is the same fixture."""
    rng = random.Random(f"{seed}:{index}")
    n_classes = rng.choice([3, 4])
    pool = CLASS_POOL[:]
    rng.shuffle(pool)
    chosen = sorted(pool[:n_classes], key=CLASS_POOL.index)

    classes: list[Class] = []
    assets: list[Asset] = []
    used: set[str] = set()

    def take(base: str) -> str:
        """A description that is not already used in this fixture.

        A register names its assets; two assets in one register rarely share a name,
        and a vouching check that cannot tell them apart cannot vouch for either.
        The near-miss pair ("Forklift" / "Forklift replacement") is deliberate — they
        are different names, and a matcher doing substring matching is the one that
        gets confused.
        """
        if base not in used:
            used.add(base)
            return base
        for i in range(2, 99):
            candidate = f"{base} {i}"
            if candidate not in used:
                used.add(candidate)
                return candidate
        used.add(base)
        return base

    for ci, cname in enumerate(chosen):
        rate = RATES[cname]
        opening = rng.randrange(200, 1200, 10) * 1_000
        additions = 0 if rng.random() < 0.25 else rng.randrange(20, 300, 5) * 1_000
        disposals = 0 if rng.random() < 0.4 else rng.randrange(20, 60, 5) * 1_000
        opening_accdep = round(opening * rate / 1_000) * 1_000
        classes.append(Class(cname, rate, opening, additions, disposals, opening_accdep, 0))

        # one or two opening assets — a sample. The register's asset list is not the
        # whole population, and no check assumes it sums to the rollforward.
        n_open = rng.choice([1, 2])
        for k in range(n_open):
            assets.append(Asset(_asset_id(ci, k + 1), take(rng.choice(_ASSET_NAMES)),
                                cname, opening // n_open, rng.choice(_OPENING_DATES), "opening"))
        # the additions — every one of them is in the list, because each is vouched
        if additions:
            n_add = rng.choice([1, 2])
            for k in range(n_add):
                desc = rng.choice(_ASSET_NAMES)
                if k == 0 and n_add == 2 and rng.random() < 0.5:
                    desc = "Forklift replacement"     # the near-miss name
                assets.append(Asset(_asset_id(ci, 10 + k), take(desc), cname,
                                    additions // n_add, rng.choice(_ADDITION_DATES), "addition"))
        # the disposals
        if disposals:
            desc = "Forklift" if rng.random() < 0.3 else rng.choice(_ASSET_NAMES)
            # the accumulated depreciation the disposal relieves — a real register
            # carries it, and the loss on disposal is the net book value less proceeds
            accum = round(disposals * rng.uniform(0.3, 0.6) / 1_000) * 1_000
            classes[-1].on_disposals += accum
            assets.append(Asset(_asset_id(ci, 20), take(desc), cname, disposals,
                                rng.choice(_OPENING_DATES), "disposed", accum_dep=accum))

    plan = Plan(name=name or f"fixture-{index:03d}", classes=classes, assets=assets)
    plan.proceeds_by_asset = {a.id: (0 if rng.random() < 0.5 else a.cost // 2)
                              for a in assets if a.status == "disposed"}
    _seed_errors(plan, rng)
    return plan


def _seed_errors(plan: Plan, rng: random.Random) -> None:
    """Seed 0-3 errors. A third of the population is left clean, deliberately."""
    if rng.random() < 0.35:
        return
    used_classes: set[str] = set()
    kinds = rng.sample(list(KINDS), 4)
    for kind in kinds:
        if len(plan.ground_truth()) >= 3:
            break
        if kind in ("dep_variance", "cost_variance", "rollforward_variance",
                    "prior_year_variance"):
            free = [c for c in plan.classes if c.name not in used_classes]
            if not free:
                continue
            c = rng.choice(free)
            used_classes.add(c.name)
            if kind == "dep_variance":
                correct = round(c.rate * (c.opening_cost + c.additions - c.disposals))
                plan.charged_override[c.name] = max(0, correct - rng.choice([1_000, 2_000, 3_000, 5_000]))
            elif kind == "cost_variance":
                delta = rng.choice([1_000, 2_000, 4_000]) * rng.choice([1, -1])
                plan.gl_delta[c.name] = delta
                plan.tb_delta[c.name] = delta
            elif kind == "rollforward_variance":
                delta = rng.choice([2_000, 5_000, 10_000]) * rng.choice([1, -1])
                if c.additions:
                    plan.additions_override[c.name] = c.additions + delta
                else:
                    plan.disposals_override[c.name] = c.disposals + delta
            elif kind == "prior_year_variance":
                plan.prior_delta[c.name] = rng.choice([1_000, 3_000]) * rng.choice([1, -1])
        elif kind == "missing_support":
            adds = [a for a in plan.assets
                    if a.status == "addition" and a.id not in plan.missing_support
                    and a.id not in plan.support_amount]
            if adds:
                plan.missing_support.append(rng.choice(adds).id)
        elif kind == "support_variance":
            adds = [a for a in plan.assets
                    if a.status == "addition" and a.id not in plan.support_amount
                    and a.id not in plan.missing_support]
            if adds:
                a = rng.choice(adds)
                plan.support_amount[a.id] = a.cost + rng.choice([1_000, 2_500, -1_000])
        elif kind == "missing_approval":
            disposed = [a for a in plan.assets if a.status == "disposed"]
            if not disposed:
                continue
            a = rng.choice(disposed)
            if rng.random() < 0.5:
                plan.no_disposal_doc.append(a.id)
            else:
                plan.unapproved.append(plan.disposal_ref(a))

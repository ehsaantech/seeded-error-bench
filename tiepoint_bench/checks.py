"""The fixed-asset substantive checks.

DECISIONS §3 — reconciliation is pandas, never the LLM. These checks read the
register, the ledger and the support documents, and raise a typed finding for every
difference, with a locator on every figure they touch (DECISIONS §5).

**The checks are the account-area layer.** The engine core knows nothing about PPE:
which sheet carries the closing cost, which column is the rate, which document is
the policy — all of that is config (DECISIONS §9), and the defaults here are one
firm's convention. An engagement replaces them; the engine does not change.

What each check does, and what it refuses to do:

1. **Rollforward** — the register's own arithmetic: opening + additions − disposals
   must be the closing cost. A register that does not balance is not a register.
2. **Accumulated depreciation rollforward** — opening + charge − on disposals must be
   the closing accumulated depreciation.
3. **Policy** — the charge must be the rate x closing cost the policy states. The
   rate comes from the engagement's config, where the auditor records it; a class
   with no configured rate is a finding, never a guess.
4. **Register ↔ ledger** — the closing cost per class must agree with the GL and
   with the trial balance. The GL and the TB usually carry the same error, so this
   is one error and two reports.
5. **Prior year** — the opening balance must be the prior year's closing balance.
6. **Additions vouching** — every addition must have an invoice, and the invoice
   must state the register's amount.
7. **Disposals vouching** — every disposal must have a disposal document, and that
   document must carry an approval.

**Matching is exact, never a substring.** "Forklift" and "Forklift replacement" are
different assets; a matcher that treats one as the other misses a real error or
raises a false positive on the wrong asset. Names are compared after normalisation
(case, whitespace, punctuation) — nothing else.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from . import workbooks as tables

# ------------------------------------------------------------------ the firm's mapping

DEFAULT_SOURCES = {
    "register": "FA-01", "prior": "FA-05", "gl": "FA-06", "tb": "FA-07",
    "policy": "FA-04", "invoices": "FA-02", "disposals": "FA-03",
}

DEFAULT_SHEETS = {
    "cost": "Cost Rollforward", "dep": "Depreciation Rollforward",
    "assets": "Asset List", "prior": "FY25 Rollforward", "gl": "GL Detail", "tb": "TB",
}

DEFAULT_COLUMNS = {
    "cost": {"class": "Asset class", "opening": "Opening cost", "additions": "Additions",
             "disposals": "Disposals", "closing": "Closing cost"},
    "dep": {"class": "Asset class", "opening": "Opening acc dep", "charge": "Charge for year",
            "on_disposals": "On disposals", "closing": "Closing acc dep"},
    "assets": {"id": "Asset ID", "description": "Description", "class": "Class",
               "cost": "Cost", "acquired": "Acquisition date", "status": "Status"},
    "prior": {"class": "Asset class", "closing": "Closing cost"},
    "gl": {"account": "Account", "description": "Description", "closing": "Closing (GL)"},
    "tb": {"account": "Account", "description": "Description", "debit": "Debit"},
}

# the prefix a ledger account carries for a cost balance ("PPE — cost — Office Equipment")
COST_ACCOUNT_PREFIX = "ppe"


@dataclass
class Config:
    """How this firm lays the engagement out. The engine core holds none of it."""
    sources: dict = field(default_factory=lambda: dict(DEFAULT_SOURCES))
    sheets: dict = field(default_factory=lambda: dict(DEFAULT_SHEETS))
    columns: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_COLUMNS.items()})
    rates: dict = field(default_factory=dict)      # asset class -> annual rate


@dataclass
class Doc:
    """One document a check may read."""
    name: str
    path: Path
    request: str = ""                              # the PBC request it was routed to


@dataclass
class Finding:
    """A difference, typed, with the locator of the figure that proves it."""
    kind: str
    subject: str
    detail: str
    amount: Decimal | None = None
    locator: dict | None = None
    source: str = ""

    def as_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject, "detail": self.detail,
                "amount": str(self.amount) if self.amount is not None else None,
                "locator": self.locator, "source": self.source}


# ------------------------------------------------------------------ names

def _norm(text: str) -> str:
    """Normalise a name for comparison: case, whitespace, punctuation, em-dashes.

    Normalisation is not matching. "Forklift" and "Forklift replacement" stay
    different names, on purpose.
    """
    t = (text or "").replace("—", "-").replace("–", "-")
    t = re.sub(r"[^\w\s-]", " ", t)
    t = re.sub(r"[\s-]+", " ", t)
    return t.strip().casefold()


def _class_from_description(description: str) -> str:
    """The asset class a ledger account describes ("PPE — cost — Office Equipment")."""
    parts = [p.strip() for p in re.split(r"[—–]", description or "") if p.strip()]
    return parts[-1] if parts else ""


def _is_cost_account(description: str) -> bool:
    return _norm(description).startswith(COST_ACCOUNT_PREFIX)


def _asset_name(text: str) -> str:
    """The asset a disposal document names ("Ute — sold to employee" → "Ute")."""
    return (text or "").split("—")[0].split("–")[0].strip()


# ------------------------------------------------------------------ sheets

class _Sheet:
    """A sheet, read through its headers: logical field -> (value, locator)."""

    def __init__(self, table: tables.Table, sheet_name: str, columns: dict[str, str]):
        self.file = table.filename
        sheet = table.sheet(sheet_name)
        self.sheet = sheet
        self.index: dict[str, int] = {}
        if sheet is None:
            return
        heads = [c.text.strip().casefold() for c in sheet.row(1)]
        for logical, header in columns.items():
            want = header.strip().casefold()
            for i, h in enumerate(heads):
                if h == want:
                    self.index[logical] = i
                    break

    def present(self) -> bool:
        return self.sheet is not None and bool(self.index)

    def _cell(self, row: list[tables.Cell], logical: str) -> tables.Cell | None:
        i = self.index.get(logical)
        if i is None or i >= len(row):
            return None
        return row[i]

    def rows(self) -> list[dict[str, tables.Cell | None]]:
        """The data rows, without the header and without the TOTAL row."""
        if self.sheet is None:
            return []
        out = []
        for row in self.sheet.rows[1:]:
            first = row[0].text.strip().casefold() if row else ""
            if first in ("total", "totals"):
                continue
            if not any(c.text for c in row):
                continue
            out.append({k: self._cell(row, k) for k in self.index})
        return out


def _number(cell: tables.Cell | None) -> Decimal | None:
    return cell.number() if cell is not None else None


def _text(cell: tables.Cell | None) -> str:
    return cell.text if cell is not None else ""


def _locator(cells: list[tables.Cell | None], file: str, sheet: str | None = None) -> dict:
    """The locator for a figure built from one or more cells.

    One cell -> that cell's own locator. Several -> the cells it was summed from, and
    no "cell" key: a sum cannot honestly claim one cell (DECISIONS §5).
    """
    live = [c for c in cells if c is not None]
    if not live:
        return {"kind": "cells", "file": file}
    if len(live) == 1:
        loc = dict(live[0].locator)
        loc.setdefault("file", file)
        return loc
    loc: dict = {"kind": "cells", "file": file}
    first = live[0].locator
    for key in ("sheet", "page"):
        if key in first:
            loc[key] = first[key]
    if sheet and "sheet" not in loc:
        loc["sheet"] = sheet
    rows: list[int] = []
    for c in live:
        if c.row not in rows:
            rows.append(c.row)
    loc["rows"] = rows
    refs = [c.locator.get("cell") for c in live]
    if all(refs):
        loc["cells"] = refs
    return loc


# ------------------------------------------------------------------ documents

def _read_table(doc: Doc) -> tables.Table:
    return tables.read_table(doc.path, doc.name)


def _labels(text: str) -> dict[str, str]:
    """The 'Label: value' lines of a document, lower-cased label -> value."""
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        out.setdefault(label.strip().casefold(), value.strip())
    return out


def _doc_text(doc: Doc) -> str:
    """The document's text, extracted deterministically. Never a model call."""
    from .text import text_of

    return text_of(doc.path, Path(doc.name).suffix.lower())


# ------------------------------------------------------------------ the checks

def run_checks(docs: list[Doc], config: Config | None = None) -> list[Finding]:
    """Run every fixed-asset check over these documents. Deterministic.

    A document the engagement has not provided is a *missing* check, not a silent
    skip: a check that cannot run raises a finding of its own, so the queue says
    what was not tested.
    """
    cfg = config or Config()
    roles = _by_role(docs, cfg)
    findings: list[Finding] = []

    findings += _check_rollforward(roles, cfg)
    findings += _check_accum_rollforward(roles, cfg)
    findings += _check_policy(roles, cfg)
    findings += _check_ledger(roles, cfg)
    findings += _check_prior_year(roles, cfg)
    findings += _check_additions(roles, cfg)
    findings += _check_disposals(roles, cfg)
    return findings


def _by_role(docs: list[Doc], cfg: Config) -> dict[str, list[Doc]]:
    roles: dict[str, list[Doc]] = {}
    for role, code in cfg.sources.items():
        matched = [d for d in docs if d.request == code]
        if not matched:
            # fall back to the code appearing in the name — the intake routes by the
            # name, and a file that arrived without being routed still must be found
            matched = [d for d in docs if code in d.name]
        roles[role] = matched
    return roles


def _sheet_for(roles: dict[str, list[Doc]], cfg: Config, role: str, sheet_key: str,
               doc: Doc | None = None) -> _Sheet:
    docs = roles.get(role) or []
    target = doc or (docs[0] if docs else None)
    if target is None:
        return _Sheet(tables.Table(path=Path("."), filename="", kind=""), "", {})
    return _Sheet(_read_table(target), cfg.sheets.get(sheet_key, ""),
                  cfg.columns.get(sheet_key, {}))


def _check_rollforward(roles, cfg) -> list[Finding]:
    """The register's own arithmetic: opening + additions - disposals = closing."""
    docs = roles.get("register") or []
    if not docs:
        return []
    sheet = _sheet_for(roles, cfg, "register", "cost")
    if not sheet.present():
        return []
    out = []
    for row in sheet.rows():
        cls = _text(row.get("class"))
        opening = _number(row.get("opening"))
        additions = _number(row.get("additions"))
        disposals = _number(row.get("disposals"))
        closing = _number(row.get("closing"))
        if None in (opening, additions, disposals, closing):
            continue
        computed = opening + additions - disposals
        if computed != closing:
            out.append(Finding(
                kind="rollforward_variance", subject=cls,
                detail=(f"{cls}: opening {opening:,} + additions {additions:,} "
                        f"- disposals {disposals:,} = {computed:,}, but the register "
                        f"states {closing:,}"),
                amount=closing - computed,
                locator=_locator([row.get("opening"), row.get("additions"),
                                  row.get("disposals"), row.get("closing")],
                                 sheet.file, cfg.sheets.get("cost")),
                source=sheet.file))
    return out


def _check_accum_rollforward(roles, cfg) -> list[Finding]:
    """opening + charge - on disposals = closing accumulated depreciation."""
    docs = roles.get("register") or []
    if not docs:
        return []
    sheet = _sheet_for(roles, cfg, "register", "dep")
    if not sheet.present():
        return []
    out = []
    for row in sheet.rows():
        cls = _text(row.get("class"))
        opening = _number(row.get("opening"))
        charge = _number(row.get("charge"))
        relieved = _number(row.get("on_disposals"))
        closing = _number(row.get("closing"))
        if None in (opening, charge, relieved, closing):
            continue
        computed = opening + charge - relieved
        if computed != closing:
            out.append(Finding(
                kind="acc_dep_variance", subject=cls,
                detail=(f"{cls}: opening {opening:,} + charge {charge:,} "
                        f"- on disposals {relieved:,} = {computed:,}, but the register "
                        f"states {closing:,}"),
                amount=closing - computed,
                locator=_locator([row.get("opening"), row.get("charge"),
                                  row.get("on_disposals"), row.get("closing")],
                                 sheet.file, cfg.sheets.get("dep")),
                source=sheet.file))
    return out


def _check_policy(roles, cfg) -> list[Finding]:
    """The charge must be the policy charge: rate x closing cost.

    The rate comes from the engagement's config — the auditor records the policy
    there. A class with no configured rate is a finding (the check could not run),
    never a silent pass.
    """
    docs = roles.get("register") or []
    if not docs:
        return []
    sheet = _sheet_for(roles, cfg, "register", "dep")
    cost = _sheet_for(roles, cfg, "register", "cost")
    if not sheet.present() or not cost.present():
        return []
    closing_by_class = {}
    for row in cost.rows():
        cls = _text(row.get("class"))
        closing = _number(row.get("closing"))
        if cls and closing is not None:
            closing_by_class[cls] = closing
    out = []
    for row in sheet.rows():
        cls = _text(row.get("class"))
        charge = _number(row.get("charge"))
        if charge is None:
            continue
        rate = cfg.rates.get(cls)
        if rate is None:
            out.append(Finding(
                kind="no_policy", subject=cls,
                detail=(f"{cls}: no depreciation rate is recorded in the engagement's "
                        f"policy, so the charge of {charge:,} cannot be recomputed"),
                amount=None,
                locator=_locator([row.get("charge")], sheet.file, cfg.sheets.get("dep")),
                source=sheet.file))
            continue
        closing = closing_by_class.get(cls)
        if closing is None:
            continue
        expected = (Decimal(str(rate)) * closing).quantize(Decimal("1"))
        if expected != charge:
            out.append(Finding(
                kind="dep_variance", subject=cls,
                detail=(f"{cls}: charged {charge:,}, policy requires {expected:,} "
                        f"({Decimal(str(rate)) * 100:.0f}% x {closing:,})"),
                amount=expected - charge,
                locator=_locator([row.get("charge")], sheet.file, cfg.sheets.get("dep")),
                source=sheet.file))
    return out


def _check_ledger(roles, cfg) -> list[Finding]:
    """The register's closing cost must agree with the GL and with the trial balance."""
    out: list[Finding] = []
    register = roles.get("register") or []
    if not register:
        return out
    cost = _sheet_for(roles, cfg, "register", "cost")
    if not cost.present():
        return out
    closing_by_class = {}
    for row in cost.rows():
        cls = _text(row.get("class"))
        closing = _number(row.get("closing"))
        if cls and closing is not None:
            closing_by_class[cls] = closing

    for role, sheet_key, logical, label in (("gl", "gl", "closing", "GL"),
                                            ("tb", "tb", "debit", "trial balance")):
        docs = roles.get(role) or []
        if not docs:
            continue
        sheet = _sheet_for(roles, cfg, role, sheet_key)
        if not sheet.present():
            continue
        for row in sheet.rows():
            description = _text(row.get("description"))
            if not _is_cost_account(description):
                continue
            cls = _class_from_description(description)
            register_closing = closing_by_class.get(cls)
            ledger = _number(row.get(logical))
            if register_closing is None or ledger is None:
                continue
            if ledger != register_closing:
                out.append(Finding(
                    kind="cost_variance", subject=cls,
                    detail=(f"{cls}: {label} closing {ledger:,} vs register "
                            f"{register_closing:,}"),
                    amount=ledger - register_closing,
                    locator=_locator([row.get(logical)], sheet.file,
                                     cfg.sheets.get(sheet_key)),
                    source=sheet.file))
    return out


def _check_prior_year(roles, cfg) -> list[Finding]:
    """The opening balance must be the prior year's closing balance."""
    register = roles.get("register") or []
    prior = roles.get("prior") or []
    if not register or not prior:
        return []
    cost = _sheet_for(roles, cfg, "register", "cost")
    sheet = _sheet_for(roles, cfg, "prior", "prior")
    if not cost.present() or not sheet.present():
        return []
    opening_by_class = {}
    for row in cost.rows():
        cls = _text(row.get("class"))
        opening = _number(row.get("opening"))
        if cls and opening is not None:
            opening_by_class[cls] = opening
    out = []
    for row in sheet.rows():
        cls = _text(row.get("class"))
        prior_closing = _number(row.get("closing"))
        opening = opening_by_class.get(cls)
        if prior_closing is None or opening is None:
            continue
        if prior_closing != opening:
            out.append(Finding(
                kind="prior_year_variance", subject=cls,
                detail=(f"{cls}: prior-year closing {prior_closing:,} vs opening "
                        f"{opening:,}"),
                amount=prior_closing - opening,
                locator=_locator([row.get("closing")], sheet.file, cfg.sheets.get("prior")),
                source=sheet.file))
    return out


def _check_additions(roles, cfg) -> list[Finding]:
    """Every addition must have an invoice, and the invoice must state the amount.

    An invoice is attributed to an addition by the name it states. When a name
    appears more than once in the register, the amount narrows it; when it cannot,
    the invoice is attributed to nothing — the engine does not guess which asset a
    document was about.
    """
    register = roles.get("register") or []
    invoices = roles.get("invoices") or []
    if not register:
        return []
    sheet = _sheet_for(roles, cfg, "register", "assets")
    if not sheet.present():
        return []
    additions = []
    for row in sheet.rows():
        if _text(row.get("status")).strip().casefold() != "addition":
            continue
        additions.append({
            "row": row,
            "id": _text(row.get("id")),
            "description": _text(row.get("description")),
            "cost": _number(row.get("cost")),
        })
    by_name: dict[str, list[dict]] = {}
    for a in additions:
        by_name.setdefault(_norm(a["description"]), []).append(a)
    parsed = [_parse_invoice(d) for d in invoices]
    inv_by_name: dict[str, list[dict]] = {}
    for invoice in parsed:
        inv_by_name.setdefault(_norm(invoice["description"]), []).append(invoice)

    # attribute an invoice to an addition: by the name it states, narrowed by the
    # amount when the name is not unique. An invoice that cannot be attributed to one
    # asset supports nothing.
    attributed: dict[int, dict] = {}
    for name, candidates in by_name.items():
        invoices_for_name = inv_by_name.get(name, [])
        if not invoices_for_name:
            continue
        if len(candidates) == 1:
            attributed[id(candidates[0])] = invoices_for_name[0]
            continue
        for invoice in invoices_for_name:
            exact = [c for c in candidates
                     if invoice["amount"] is not None and invoice["amount"] == c["cost"]]
            if len(exact) == 1:
                attributed[id(exact[0])] = invoice

    out = []
    for a in additions:
        invoice = attributed.get(id(a))
        if invoice is not None:
            # the invoice names the asset — now the amount must agree with it
            if (invoice["amount"] is not None and a["cost"] is not None
                    and invoice["amount"] != a["cost"]):
                out.append(Finding(
                    kind="support_variance", subject=a["id"],
                    detail=(f"{a['id']} {a['description']}: invoice {invoice['name']} states "
                            f"{invoice['amount']:,}, register states {a['cost']:,}"),
                    amount=invoice["amount"] - a["cost"],
                    locator={"kind": "text", "file": invoice["name"],
                             "label": "Amount (ex GST)"},
                    source=invoice["name"]))
            continue
        name = _norm(a["description"])
        candidates = by_name.get(name, [])
        invoices_for_name = inv_by_name.get(name, [])
        if invoices_for_name and len(candidates) == 1:
            amount = invoices_for_name[0]["amount"]
            out.append(Finding(
                kind="support_variance", subject=a["id"],
                detail=(f"{a['id']} {a['description']}: invoice "
                        f"{invoices_for_name[0]['name']} states {amount:,}, register "
                        f"states {a['cost']:,}"
                        if amount is not None and a["cost"] is not None else
                        f"{a['id']} {a['description']}: the invoice states a different "
                        f"amount from the register"),
                amount=(amount - a["cost"]) if (amount is not None and a["cost"] is not None)
                else None,
                locator={"kind": "text", "file": invoices_for_name[0]["name"],
                         "label": "Amount (ex GST)"},
                source=invoices_for_name[0]["name"]))
            continue
        out.append(Finding(
            kind="missing_support", subject=a["id"],
            detail=(f"{a['id']} {a['description']} ({a['cost']:,}) has no supporting "
                    f"invoice"),
            amount=a["cost"],
            locator=_locator([a["row"].get("id")], sheet.file, cfg.sheets.get("assets")),
            source=sheet.file))
    return out


def _check_disposals(roles, cfg) -> list[Finding]:
    """Every disposal must have a disposal document, and it must carry an approval."""
    register = roles.get("register") or []
    disposals = roles.get("disposals") or []
    if not register:
        return []
    sheet = _sheet_for(roles, cfg, "register", "assets")
    if not sheet.present():
        return []
    disposed = []
    for row in sheet.rows():
        if _text(row.get("status")).strip().casefold() != "disposed":
            continue
        disposed.append({"row": row, "id": _text(row.get("id")),
                         "description": _text(row.get("description"))})
    by_name: dict[str, list[dict]] = {}
    for a in disposed:
        by_name.setdefault(_norm(a["description"]), []).append(a)

    parsed = [_parse_disposal(d) for d in disposals]
    out = []
    for a in disposed:
        candidates = [p for p in parsed
                      if p["asset"] and _norm(p["asset"]) == _norm(a["description"])]
        if not candidates:
            out.append(Finding(
                kind="missing_approval", subject=a["id"],
                detail=f"the disposal of {a['id']} {a['description']} has no disposal document",
                amount=None,
                locator=_locator([a["row"].get("id")], sheet.file, cfg.sheets.get("assets")),
                source=sheet.file))
            continue
        if len(candidates) > 1:
            out.append(Finding(
                kind="missing_approval", subject=a["id"],
                detail=(f"the disposal of {a['id']} {a['description']} cannot be "
                        f"attributed: {len(candidates)} disposal records name it"),
                amount=None,
                locator=_locator([a["row"].get("id")], sheet.file, cfg.sheets.get("assets")),
                source=sheet.file))
            continue
        doc = candidates[0]
        if not doc["approved"]:
            out.append(Finding(
                kind="missing_approval", subject=doc["ref"] or a["id"],
                detail=(f"disposal {doc['ref'] or a['id']} ({a['description']}) carries no "
                        f"approval"),
                amount=None,
                locator={"kind": "text", "file": doc["name"], "label": "Approved by"},
                source=doc["name"]))
    return out


def read_policy(doc: Doc) -> dict[str, float]:
    """The depreciation policy, read from the policy document's table.

    Read deterministically, so the rules and the model read the *same* policy from the
    *same* document — the benchmark's comparison is the decision path, not the input.
    """
    try:
        import docx

        document = docx.Document(doc.path)
    except Exception:
        return {}
    rates: dict[str, float] = {}
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if len(cells) < 3:
                continue
            try:
                rate = float(cells[2].replace("%", "").strip()) / 100
            except ValueError:
                continue
            if cells[0]:
                rates[cells[0]] = rate
    return rates


def _parse_invoice(doc: Doc) -> dict:
    """An invoice, read by its labels. Deterministic — never a model call."""
    try:
        text = _doc_text(doc)
    except Exception:
        text = ""
    labels = _labels(text)
    return {
        "name": doc.name,
        "description": labels.get("description", ""),
        "amount": tables.as_number(labels.get("amount (ex gst)", "")),
        "ref": labels.get("tax invoice", "") or _ref_from_name(doc.name, "INV"),
    }


def _parse_disposal(doc: Doc) -> dict:
    """A disposal document, read by its labels. Deterministic."""
    try:
        text = _doc_text(doc)
    except Exception:
        text = ""
    labels = _labels(text)
    asset = _asset_name(labels.get("asset", ""))
    return {
        "name": doc.name,
        "asset": asset,
        "ref": labels.get("disposal", "") or _ref_from_name(doc.name, "DISP"),
        "approved": "approved by" in labels,
    }


def _ref_from_name(name: str, prefix: str) -> str:
    """The reference a document carries, from its name ("DISP-2026-003 Forklift.docx")."""
    stem = Path(name).stem
    return stem.split(" ")[0] if stem.upper().startswith(prefix) else ""

"""Generic table reading, with a locator for every cell (PB-02).

This is the first of the workbook readers the engine needs. A financial statement, a
trial balance, an AR ageing and a fixed-asset register are all the same shape — a
grid of cells — and every figure in them must be traceable to the cell it came
from. So the reading is generic here, and each document kind is understood on top
of it (see `fsparse.py`).

Nothing is inferred: a cell is a value plus the address it lives at. Amounts are
converted, never guessed — "1,234.00" and "(1,234.00)" are numbers, "-" and "" are
not, and a cell that is not a number stays text.

DECISIONS §5: no locator, no fact. The locator is built here, once, so that every
downstream consumer can rely on it.
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

XLSX_EXT = {".xlsx", ".xlsm"}
XLS_EXT = {".xls"}
CSV_EXT = {".csv", ".tsv"}
PDF_EXT = {".pdf"}


class TableError(Exception):
    pass


@dataclass
class Cell:
    value: object
    locator: dict
    row: int          # 1-based
    col: int          # 1-based

    @property
    def text(self) -> str:
        if self.value is None:
            return ""
        return str(self.value).strip()

    @property
    def is_empty(self) -> bool:
        return self.value is None or str(self.value).strip() == ""

    def number(self) -> Decimal | None:
        """The cell as a number, or None. A cell that is not a number is never one."""
        return as_number(self.value)


@dataclass
class Sheet:
    name: str
    rows: list[list[Cell]] = field(default_factory=list)

    def row(self, index: int) -> list[Cell]:
        """1-based row access."""
        if 1 <= index <= len(self.rows):
            return self.rows[index - 1]
        return []

    def non_empty(self) -> bool:
        return any(any(not c.is_empty for c in row) for row in self.rows)


@dataclass
class Table:
    path: Path
    filename: str
    kind: str
    sheets: list[Sheet] = field(default_factory=list)

    def sheet(self, name: str) -> Sheet | None:
        for s in self.sheets:
            if s.name == name:
                return s
        return None

    @property
    def rows(self) -> list[list[Cell]]:
        """Every row of every sheet, flattened in sheet order (PDFs are one sheet)."""
        out: list[list[Cell]] = []
        for s in self.sheets:
            out.extend(s.rows)
        return out


# ------------------------------------------------------------------ numbers

_NUM_CLEAN = re.compile(r"[,\s ]")
_TRAILING_UNIT = re.compile(r"[\s]*(?:AUD|USD|EUR|GBP|NZD|CAD|AED|INR|\$|kr)\s*$", re.I)
_LEADING_UNIT = re.compile(r"^\s*(?:AUD|USD|EUR|GBP|NZD|CAD|AED|INR|kr)\s*", re.I)


def as_number(value) -> Decimal | None:
    """Convert a cell to a number, or None. Never guesses, never returns a guess.

    Handles thousands separators, currency prefixes/suffixes, and accounting
    negatives in parentheses. A percentage or a date-looking string is not a number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    if negative:
        s = s[1:-1].strip()
    # a currency may lead ("AUD 1,200.00") or trail ("1,200.00 AUD")
    s = _LEADING_UNIT.sub("", s)
    s = _TRAILING_UNIT.sub("", s)
    s = s.replace("$", "").strip()
    s = _NUM_CLEAN.sub("", s)
    if not s or not re.fullmatch(r"[-+]?\d*\.?\d+", s):
        return None
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return -d if negative else d


def looks_numeric(value) -> bool:
    return as_number(value) is not None


# ------------------------------------------------------------------ readers

def _cell_letter(col: int) -> str:
    """1 -> A, 27 -> AA."""
    letters = ""
    while col > 0:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _read_xlsx(path: Path, display: str) -> list[Sheet]:
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise TableError(f"openpyxl is required to read {path.suffix}") from exc
    target, tmpdir = _named_copy(path, ".xlsx")
    try:
        wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
        sheets: list[Sheet] = []
        for ws in wb.worksheets:
            rows: list[list[Cell]] = []
            for r, raw in enumerate(ws.iter_rows(), start=1):
                cells: list[Cell] = []
                for c, cell in enumerate(raw, start=1):
                    cells.append(Cell(
                        value=cell.value,
                        locator={"kind": "cell", "file": display, "sheet": ws.title,
                                 "cell": f"{_cell_letter(c)}{r}"},
                        row=r, col=c))
                rows.append(cells)
            sheets.append(Sheet(name=ws.title, rows=rows))
        wb.close()
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return sheets


def named_copy(path: Path, suffix: str) -> tuple[Path, str | None]:
    """A copy of the file under a name the reader accepts.

    The content-addressed store keeps the bytes under their hash, with no
    extension, and some readers decide what a file is from its name. The copy
    lives in a temp directory and is removed straight after.
    """
    if path.suffix.lower() == suffix:
        return path, None
    tmpdir = tempfile.mkdtemp(prefix="tiepoint-read-")
    target = Path(tmpdir) / f"object{suffix}"
    try:
        os.symlink(path.resolve(), target)
    except OSError:
        shutil.copyfile(path, target)
    return target, tmpdir


# kept for the callers that grew up with the private name
_named_copy = named_copy


def _read_csv(path: Path, display: str) -> list[Sheet]:
    delim = "\t" if path.suffix.lower() == ".tsv" else ","
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh, delimiter=delim)
        rows: list[list[Cell]] = []
        for r, raw in enumerate(reader, start=1):
            cells = [
                Cell(value=v,
                     locator={"kind": "row", "file": display, "row": r,
                              "column": _cell_letter(c)},
                     row=r, col=c)
                for c, v in enumerate(raw, start=1)
            ]
            rows.append(cells)
    return [Sheet(name=path.stem, rows=rows)]


_PDF_SPLIT = re.compile(r"\s{2,}")


def _read_pdf(path: Path, display: str) -> list[Sheet]:
    """PDF text lines as a one-column-ish table.

    Splitting on runs of whitespace is crude, but a statement line is "label  1,234"
    and that is exactly the shape this preserves. Cells carry a page/line locator.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover
        raise TableError("pdfplumber is required to read PDFs") from exc
    rows: list[list[Cell]] = []
    with pdfplumber.open(path) as pdf:
        for p, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            for ln, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                parts = [p_ for p_ in _PDF_SPLIT.split(line.strip()) if p_]
                cells = [
                    Cell(value=v,
                         locator={"kind": "text", "file": display, "page": p, "line": ln},
                         row=len(rows) + 1, col=c)
                    for c, v in enumerate(parts, start=1)
                ]
                rows.append(cells)
    return [Sheet(name="pdf", rows=rows)]


def read_table(path: Path | str, filename: str | None = None) -> Table:
    """Read a spreadsheet, CSV or PDF into cells that each know their address.

    `filename` is the evidence object's original name. The content-addressed store
    (PB-04) keeps the bytes under their hash, with no extension — so the type is
    taken from the original name, and only then from the bytes themselves.
    """
    p = Path(path)
    if not p.is_file():
        raise TableError(f"not a file: {p}")
    display = filename or p.name
    ext = p.suffix.lower()
    if not ext and filename:
        ext = Path(filename).suffix.lower()
    if not ext:
        ext = _sniff(p)
    if ext in XLSX_EXT:
        return Table(path=p, filename=display, kind="xlsx", sheets=_read_xlsx(p, display))
    if ext in XLS_EXT:
        raise TableError(f"{ext} workbooks are not supported yet — convert to .xlsx")
    if ext in CSV_EXT:
        return Table(path=p, filename=display, kind="csv", sheets=_read_csv(p, display))
    if ext in PDF_EXT:
        return Table(path=p, filename=display, kind="pdf", sheets=_read_pdf(p, display))
    raise TableError(f"unsupported document type: {ext or 'unknown'}")


def _sniff(path: Path) -> str:
    """What kind of file is this, when the name says nothing?"""
    try:
        head = path.open("rb").read(8)
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        import zipfile

        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        except (OSError, zipfile.BadZipFile):
            return ""
        if any(n.startswith("xl/") for n in names):
            return ".xlsx"
        if any(n.startswith("word/") for n in names):
            return ".docx"
        return ""
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    return ".csv"

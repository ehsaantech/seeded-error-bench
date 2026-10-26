"""Deterministic text extraction.

The same rule the engine follows: the type of a file comes from the name it arrived
under, and the bytes are read through a temporary name, because some libraries decide
what a file is from its name (openpyxl does).

No model call lives here. Reading a document's text is not a judgement, and a
judgement is not a parse.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from . import workbooks as tables

TEXT_EXT = {".txt", ".csv", ".md"}


def text_of(path: Path, ext: str | None = None) -> str:
    """The document's text, or '' when nothing is extractable (likely a scan)."""
    ext = (ext or path.suffix).lower()
    target, tmpdir = tables.named_copy(path, ext) if ext else (path, None)
    try:
        return _extract(target, ext)
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _extract(path: Path, ext: str) -> str:
    try:
        if ext == ".pdf":
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                return "\n".join((pg.extract_text() or "") for pg in pdf.pages).strip()
        if ext in TEXT_EXT:
            return path.read_text(errors="ignore").strip()
        if ext == ".docx":
            import docx

            return "\n".join(p.text for p in docx.Document(path).paragraphs).strip()
        if ext in {".xlsx", ".xls"}:
            import openpyxl

            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            out = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    cells = [str(c) for c in row if c is not None]
                    if cells:
                        out.append(" | ".join(cells))
            wb.close()
            return "\n".join(out).strip()
    except Exception:
        return ""
    return ""

"""Dependency-free table export to CSV and XLSX.

XLSX is just a zip of a few XML parts, so a minimal single-sheet workbook
can be written with the stdlib (zipfile + string templates) — no openpyxl
/ xlsxwriter needed. Strings are written inline (no shared-strings table)
to keep it simple; numbers are written as numbers so they stay sortable in
a spreadsheet.
"""

from __future__ import annotations

import csv
import io
import zipfile
from xml.sax.saxutils import escape

Row = list


def to_csv(headers: Row, rows: list[Row]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue()


def _col_ref(idx0: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA (spreadsheet column letters)."""
    s = ""
    n = idx0 + 1
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell(col0: int, row1: int, value) -> str:
    ref = f"{_col_ref(col0)}{row1}"
    if isinstance(value, bool):
        value = str(value)
    if isinstance(value, (int, float)):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = "" if value is None else str(value)
    return (f'<c r="{ref}" t="inlineStr"><is>'
            f'<t xml:space="preserve">{escape(text)}</t></is></c>')


_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    '</Types>')

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
    '</Relationships>')

_WB_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
    '</Relationships>')


def to_xlsx(headers: Row, rows: list[Row], sheet_name: str = "Sheet1") -> bytes:
    # Excel sheet-name rules: <= 31 chars, none of []:*?/\
    safe_name = "".join(c for c in sheet_name if c not in '[]:*?/\\')[:31] or "Sheet1"
    xml_rows = []
    for ri, row in enumerate([headers] + rows, start=1):
        cells = "".join(_cell(ci, ri, v) for ci, v in enumerate(row))
        xml_rows.append(f'<row r="{ri}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(safe_name)}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", _WB_RELS)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()

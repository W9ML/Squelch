"""Dependency-free PDF case report (the printable documentation packet).

Design notes:
- In the spirit of ``tabexport.py`` (XLSX from the stdlib), the PDF is
  hand-rolled rather than importing reportlab/weasyprint: a case report is
  linear text plus one table, well within reach of raw PDF text operators
  and the built-in Helvetica faces, whose AFM widths are compiled in below
  so line wrapping is measured rather than guessed.
- Content streams are deliberately left uncompressed: reports are small,
  and greppable output lets the tests assert on visible text.
"""

from __future__ import annotations

import time

# ---- page geometry (US Letter, 0.75" margins) ----
PAGE_W, PAGE_H = 612.0, 792.0
MARGIN = 54.0
USABLE = PAGE_W - 2 * MARGIN
FLOOR = 64.0                      # bottom band reserved for the footer

# ---- Helvetica / Helvetica-Bold AFM widths for chars 32..126 (1/1000 em).
# Helvetica-Oblique shares the regular widths. Anything outside the table
# (rare cp1252 punctuation in transcripts) falls back to 556.
_WN = (
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
)
_WB = (
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
)


def _n(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s or "0"


def _esc(s: str) -> bytes:
    """cp1252 (~WinAnsi) with the three PDF string metacharacters escaped."""
    b = s.encode("cp1252", "replace")
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _cw(ch: str, bold: bool) -> int:
    b = ch.encode("cp1252", "replace")
    c = b[0] if b else 32
    return (_WB if bold else _WN)[c - 32] if 32 <= c <= 126 else 556


def _sw(s: str, bold: bool, size: float) -> float:
    return sum(_cw(c, bold) for c in s) * size / 1000.0


def _wrap(text, bold: bool, size: float, maxw: float) -> list[str]:
    """Greedy word wrap; words wider than the column are hard-split."""
    out: list[str] = []
    for para in (str(text).splitlines() or [""]):
        words = para.split()
        if not words:
            out.append("")
            continue
        cur = ""
        for wd in words:
            if _sw(wd, bold, size) > maxw:
                if cur:
                    out.append(cur)
                    cur = ""
                seg, segw = [], 0.0
                for ch in wd:
                    w = _cw(ch, bold) * size / 1000.0
                    if seg and segw + w > maxw:
                        out.append("".join(seg))
                        seg, segw = [ch], w
                    else:
                        seg.append(ch)
                        segw += w
                wd = "".join(seg)
            cand = f"{cur} {wd}" if cur else wd
            if _sw(cand, bold, size) <= maxw:
                cur = cand
            else:
                out.append(cur)
                cur = wd
        out.append(cur)
    return out or [""]


# ---- the document builder ----

class _Doc:
    def __init__(self):
        self.pages: list[list[bytes]] = []
        self.y = 0.0
        self.new_page()

    @property
    def ops(self) -> list[bytes]:
        return self.pages[-1]

    def new_page(self) -> None:
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def need(self, h: float) -> None:
        if self.y - h < FLOOR:
            self.new_page()

    def text(self, x: float, y: float, s: str, size: float,
             bold: bool = False, oblique: bool = False,
             gray: float = 0.0) -> None:
        f = "F2" if bold else ("F3" if oblique else "F1")
        self.ops.append(
            f"{_n(gray)} g BT /{f} {_n(size)} Tf 1 0 0 1 {_n(x)} {_n(y)} Tm ".encode()
            + b"(" + _esc(s) + b") Tj ET")

    def rule(self, x1: float, y: float, x2: float,
             gray: float = 0.75, width: float = 0.8) -> None:
        self.ops.append(
            f"{_n(gray)} G {_n(width)} w {_n(x1)} {_n(y)} m {_n(x2)} {_n(y)} l S"
            .encode())

    def vseg(self, x: float, y1: float, y2: float,
             gray: float = 0.75, width: float = 2.0) -> None:
        self.ops.append(
            f"{_n(gray)} G {_n(width)} w {_n(x)} {_n(y1)} m {_n(x)} {_n(y2)} l S"
            .encode())

    def cell_box(self, x: float, y: float, w: float, h: float,
                 fill: float | None = None) -> None:
        if fill is not None:
            self.ops.append(f"{_n(fill)} g {_n(x)} {_n(y)} {_n(w)} {_n(h)} re f"
                            .encode())
        self.ops.append(f"0.72 G 0.5 w {_n(x)} {_n(y)} {_n(w)} {_n(h)} re S"
                        .encode())

    def heading(self, s: str) -> None:
        self.need(34)
        self.y -= 8
        self.text(MARGIN, self.y - 11, s, 11.5, bold=True)
        self.y -= 17

    def paragraph(self, text, size: float = 9.5, gap: float = 6.0) -> None:
        for ln in _wrap(text, False, size, USABLE):
            self.need(12)
            self.text(MARGIN, self.y - 9, ln, size)
            self.y -= 12
        self.y -= gap

    def table(self, cols: tuple, rows: list[list[str]], empty: str) -> None:
        size, lh, pad = 8.0, 9.6, 3.0
        head_h = lh + 2 * pad

        def header() -> None:
            self.need(head_h + lh + 2 * pad)
            x = MARGIN
            for label, cw in cols:
                self.cell_box(x, self.y - head_h, cw, head_h, fill=0.94)
                self.text(x + pad, self.y - pad - 7.0, label, size, bold=True)
                x += cw
            self.y -= head_h

        header()
        if not rows:
            rh = lh + 2 * pad
            self.cell_box(MARGIN, self.y - rh, USABLE, rh)
            self.text(MARGIN + pad, self.y - pad - 7.0, empty, size)
            self.y -= rh + 10
            return
        for r in rows:
            wrapped = [_wrap(c, False, size, cw - 2 * pad)
                       for c, (_, cw) in zip(r, cols)]
            nlines = max(len(wl) for wl in wrapped)
            rh = nlines * lh + 2 * pad
            if self.y - rh < FLOOR:
                self.new_page()
                header()
            x = MARGIN
            for wl, (_, cw) in zip(wrapped, cols):
                self.cell_box(x, self.y - rh, cw, rh)
                yy = self.y - pad - 7.0
                for ln in wl:
                    self.text(x + pad, yy, ln, size)
                    yy -= lh
                x += cw
            self.y -= rh
        self.y -= 10

    # ---- assembly ----
    def build(self, footer_of) -> bytes:
        npages = len(self.pages)
        for i, ops in enumerate(self.pages, 1):
            txt = footer_of(i, npages)
            x = (PAGE_W - _sw(txt, False, 8.0)) / 2
            ops.append(f"0.45 g BT /F1 8 Tf 1 0 0 1 {_n(x)} 40 Tm ".encode()
                       + b"(" + _esc(txt) + b") Tj ET")

        objs: list[bytes] = []
        objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")        # 1
        objs.append(b"")                                         # 2 (patched)
        for name in (b"Helvetica", b"Helvetica-Bold", b"Helvetica-Oblique"):
            objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /" + name
                        + b" /Encoding /WinAnsiEncoding >>")     # 3..5
        res = b"/Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >>"
        first_page = 6
        kids = b" ".join(b"%d 0 R" % (first_page + 2 * i) for i in range(npages))
        objs[1] = b"<< /Type /Pages /Count %d /Kids [ %s ] >>" % (npages, kids)
        for i, ops in enumerate(self.pages):
            content = b"\n".join(ops)
            objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]"
                        b" /Resources << %s >> /Contents %d 0 R >>"
                        % (res, first_page + 2 * i + 1))
            objs.append(b"<< /Length %d >>\nstream\n%s\nendstream"
                        % (len(content), content))

        buf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(objs) + 1)
        for i, body in enumerate(objs, 1):
            offsets[i] = len(buf)
            buf += b"%d 0 obj\n" % i + body + b"\nendobj\n"
        xref_at = len(buf)
        buf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        for i in range(1, len(objs) + 1):
            buf += b"%010d 00000 n \n" % offsets[i]
        buf += (b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
                % (len(objs) + 1, xref_at))
        return bytes(buf)


# ---- the report itself ----

def render_case_pdf(case: dict, site_name: str) -> bytes:
    """The printable documentation packet for one case, as PDF bytes."""
    d = _Doc()

    def ts(t) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "—"

    num = str(case["number"])

    # header: title block
    tx = MARGIN
    ty = d.y - 14
    for ln in _wrap(f"Case {num} — {case['title']}", True, 16,
                    PAGE_W - MARGIN - tx):
        d.text(tx, ty, ln, 16, bold=True)
        ty -= 20
    d.text(tx, ty,
           f"{site_name} · interference documentation · generated {ts(time.time())}",
           9.5, gray=0.35)
    ty -= 6
    d.y = ty - 10
    d.rule(MARGIN, d.y, PAGE_W - MARGIN)
    d.y -= 16

    # the dl grid from the HTML version: label column + wrapped values
    items = case.get("items") or []
    for k, v in (("Status", case["status"]),
                 ("Suspected operator", case.get("subject") or "—"),
                 ("Opened", ts(case["opened_at"])),
                 ("Evidence", f"{len(items)} recording(s)")):
        vl = _wrap(v, False, 9.5, USABLE - 140)
        d.need(len(vl) * 12 + 2)
        d.text(MARGIN, d.y - 9, k, 9.5, bold=True, gray=0.30)
        yy = d.y - 9
        for ln in vl:
            d.text(MARGIN + 140, yy, ln, 9.5)
            yy -= 12
        d.y -= len(vl) * 12 + 2

    d.heading("Summary")
    d.paragraph(case.get("summary") or "—")

    d.heading("Evidence — recordings")
    rows = []
    for i, it in enumerate(items, 1):
        secs = round((it.get("duration_ms") or 0) / 1000.0, 1)
        purged = "" if it.get("has_audio") else " (audio purged)"
        rows.append([str(i), ts(it["started_at"]), f"{secs}s",
                     str(it.get("origin") or ""), str(it.get("origin_hub") or ""),
                     f"tx {it['tx_id']}{purged}",
                     str(it.get("label") or ""), str(it.get("note") or "")])
    d.table((("#", 18), ("Timestamp (local)", 100), ("Len", 30),
             ("Origin", 70), ("Hub", 46), ("Recording", 70),
             ("Label", 80), ("Note", 90)),
            rows, empty="No recordings attached.")

    d.heading("Activity log")
    notes = case.get("notes") or []
    if not notes:
        d.paragraph("—")
    for n in notes:
        who = str(n.get("author") or "system")
        sysflag = " · system" if n.get("kind") == "system" else ""
        stamp = ts(n["ts"])
        d.need(13)
        d.vseg(MARGIN + 1.5, d.y, d.y - 13)
        d.text(MARGIN + 10, d.y - 9, stamp, 8.5, gray=0.45)
        d.text(MARGIN + 10 + _sw(stamp, False, 8.5) + 8, d.y - 9,
               who + sysflag, 9.5, bold=True)
        d.y -= 13
        for ln in _wrap(n["text"], False, 9.5, USABLE - 12):
            d.need(12)
            d.vseg(MARGIN + 1.5, d.y, d.y - 12)
            d.text(MARGIN + 10, d.y - 9, ln, 9.5)
            d.y -= 12
        d.y -= 7

    return d.build(lambda i, np:
                   f"Case {num} · {site_name} · page {i} of {np}")

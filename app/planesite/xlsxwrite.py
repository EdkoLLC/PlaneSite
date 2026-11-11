"""A small .xlsx (Excel) writer: one sheet, a bold header row, text and number cells, and total
formulas. Written by hand so PlaneSite doesn't need a spreadsheet library in its runtime.

    write_xlsx(path, "Sheet", ["Name", "Acres"], [["Area 1", 26.5], ...],
               formats=[None, "0.00"], widths=[24, 12], totals={1: "SUM"}, total_label="Total")
"""
import datetime
import zipfile
from xml.sax.saxutils import escape

_FORMATS = {"0": 1, "0.00": 2, "#,##0": 3, "#,##0.00": 4}  # Excel's built-in number formats
_BAD_SHEET = set('[]:*?/\\')


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_text(v):
    # characters XML 1.0 can't hold (control codes) would make Excel refuse the file
    s = "".join(ch for ch in str(v) if ch in "\t\n\r" or ord(ch) >= 32)
    return escape(s)


def sheet_name(s):
    s = "".join("_" if ch in _BAD_SHEET else ch for ch in str(s)).strip("' ") or "Sheet1"
    return s[:31]


def write_xlsx(path, sheet, header, rows, formats=None, widths=None, totals=None, total_label="Total"):
    """rows: lists of str / int / float / None. formats: a number format per column ("0.00" ...).
    totals: {column index: "SUM"} adds a bold total row under the data."""
    ncol = len(header)
    formats = list(formats or [None] * ncol)
    styles = []          # cellXfs after the default: (numFmtId, bold, right-aligned)
    def style(fmt, bold=False, right=False):
        key = (_FORMATS.get(fmt, 0), bold, right)
        if key not in styles:
            styles.append(key)
        return styles.index(key) + 1
    head_s = [style(None, True, bool(f)) for f in formats]  # number columns: heading over the numbers
    col_s = [style(f) for f in formats]
    tot_s = [style(f, True) for f in formats]

    def cell(ref, v, s):
        if v is None or v == "":
            return '<c r="%s" s="%d"/>' % (ref, s)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return '<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, s, _xml_text(v))
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, s, repr(float(v)) if isinstance(v, float) else v)

    out = []
    out.append('<row r="1">' + "".join(cell("%s1" % _col(c), h, head_s[c]) for c, h in enumerate(header)) + "</row>")
    for r, row in enumerate(rows, start=2):
        out.append('<row r="%d">' % r + "".join(cell("%s%d" % (_col(c), r), (row[c] if c < len(row) else None),
                                                        col_s[c]) for c in range(ncol)) + "</row>")
    last = len(rows) + 1
    if totals and rows:
        t = last + 1
        cells = []
        for c in range(ncol):
            ref = "%s%d" % (_col(c), t)
            if c in totals:
                col = _col(c)
                vals = [row[c] for row in rows if c < len(row) and isinstance(row[c], (int, float))]
                cells.append('<c r="%s" s="%d"><f>SUM(%s2:%s%d)</f><v>%r</v></c>'
                             % (ref, tot_s[c], col, col, last, float(sum(vals))))
            elif c == 0:
                cells.append(cell(ref, total_label, tot_s[c]))
        out.append('<row r="%d">' % t + "".join(cells) + "</row>")

    cols = ""
    if widths:
        cols = "<cols>" + "".join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (i + 1, i + 1, w)
                                  for i, w in enumerate(widths)) + "</cols>"
    auto = '<autoFilter ref="A1:%s%d"/>' % (_col(ncol - 1), last) if rows else ""
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft"'
                 ' state="frozen"/></sheetView></sheetViews>'
                 '<sheetFormatPr defaultRowHeight="15"/>' + cols +
                 "<sheetData>" + "".join(out) + "</sheetData>" + auto + "</worksheet>")

    xfs = '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>' + "".join(
        '<xf numFmtId="%d" fontId="%d" fillId="0" borderId="%d" xfId="0"%s%s%s>%s</xf>'
        % (n, 1 if b else 0, 1 if b else 0, ' applyNumberFormat="1"' if n else "",
           ' applyFont="1" applyBorder="1"' if b else "", ' applyAlignment="1"' if r else "",
           '<alignment horizontal="right"/>' if r else "")
        for n, b, r in styles)
    styles_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                  '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  '<fonts count="2"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
                  '<font><b/><sz val="11"/><name val="Calibri"/><family val="2"/></font></fonts>'
                  '<fills count="2"><fill><patternFill patternType="none"/></fill>'
                  '<fill><patternFill patternType="gray125"/></fill></fills>'
                  '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
                  '<border><left/><right/><top/><bottom style="thin"><color auto="1"/></bottom><diagonal/></border></borders>'
                  '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                  '<cellXfs count="%d">%s</cellXfs>'
                  '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                  '</styleSheet>' % (len(styles) + 1, xfs))
    name = _xml_text(sheet_name(sheet))
    defined = ('<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
               "'%s'!$A$1:$%s$%d</definedName></definedNames>" % (name.replace("'", "''"), _col(ncol - 1), last)
               if rows else "")
    workbook = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="%s" sheetId="1" r:id="rId1"/></sheets>%s'
                '<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>' % (name, defined))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>',
        "docProps/core.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:creator>PlaneSite</dc:creator>'
            '<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            '</cp:coreProperties>' % (now, now),
        "docProps/app.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            '<Application>PlaneSite</Application></Properties>',
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/worksheets/sheet1.xml": sheet_xml,
        "xl/styles.xml": styles_xml,
    }
    tmp = path + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in files.items():
            z.writestr(n, data)
    import os
    try:
        os.replace(tmp, path)  # fails with PermissionError while Excel has the old file open (Windows)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path

"""Excel for the caller: leads the manager took "В работу", generated on demand with the standard library only.

One polished sheet: title banner, frozen header, zebra rows, real dates, deadline urgency, colour-coded grade,
clickable link. Passive workbook: inline strings and external hyperlinks only, no formulas, macros or embeds.
"""
from __future__ import annotations

import io
import math
import re
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from xml.sax.saxutils import escape, quoteattr

MSK = timezone(timedelta(hours=3))
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_BAD = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\D{1,8}(\d{1,2}):(\d{2}))?")
URGENT_HOURS = 48
# kind -> (sheet/banner title, header of the date column, tab colour, subtitle hint)
KINDS = {
    "work": ("Лиды в работе", "Взято в работу", "FF2A7F8F", "вы взяли их в работу"),
    "new": ("Новые лиды", "Получен", "FF3B6FB6", "на карточки ещё не нажимали «В работу» или «Мимо»"),
}

# (header, width, kind)
COLUMNS = [
    ("№", 5, "num"), ("Заказчик", 30, "customer"), ("Контакт", 24, "name"), ("Телефон", 26, "phone"),
    ("Срок приёма", 17, "deadline"), ("Комментарий", 38, "comment"), ("Что покупают", 48, "text"), ("Должность", 24, "text"), ("Email", 28, "text"),
    ("Другие контакты", 36, "text"), ("Зацепки для разговора", 60, "text"), ("Оценка", 9, "grade"),
    ("Дата", 17, "taken"), ("Закупка", 15, "link"),
]


def _clean(value, limit=4000) -> str:
    return _BAD.sub("", "" if value is None else str(value))[:limit]


def _col(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _safe_link(url) -> str | None:
    if not isinstance(url, str) or len(url) > 1500 or re.search(r"[\s\x00-\x1f\\<>]", url):
        return None
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or "." not in parts.hostname:
        return None
    return url


_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?\s*(Z)?)?")


def parse_deadline(text) -> tuple[datetime, bool] | None:
    """First date in a free-form deadline string -> (Moscow datetime, has_time). Date-only means end of that day.

    Understands "01.10.2026 09:00" and ISO "2026-10-02T11:00:00Z" (a trailing Z is UTC, anything else is Moscow time).
    """
    text = str(text or "")
    iso, dotted = _ISO.search(text), _DATE.search(text)
    try:
        if iso and (not dotted or iso.start() <= dotted.start()):
            year, month, day, hour, minute, zulu = iso.groups()
            if hour is None:
                return datetime(int(year), int(month), int(day), 23, 59, tzinfo=MSK), False
            moment = datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=timezone.utc if zulu else MSK)
            return moment.astimezone(MSK), True
        if dotted:
            day, month, year, hour, minute = dotted.groups()
            if hour is not None:
                return datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=MSK), True
            return datetime(int(year), int(month), int(day), 23, 59, tzinfo=MSK), False
    except ValueError:
        return None
    return None


def _serial(moment: datetime) -> float:
    return (moment.astimezone(MSK).replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400


def _comment_text(comments) -> str:
    """The manager's notes, oldest first, each with the Moscow time it was written."""
    lines = []
    for note in comments or []:
        try:
            at = datetime.fromisoformat(str(note["at"]).replace("Z", "+00:00")).astimezone(MSK)
        except (KeyError, ValueError):
            continue
        lines.append(f"{at:%d.%m %H:%M} — {note.get('text', '')}")
    return "\n".join(lines)


def lead_row(item: dict) -> dict:
    result = item["result"]
    contacts = [c for c in result.get("contacts") or [] if isinstance(c, dict) and (c.get("phone") or c.get("email"))]
    main = contacts[0] if contacts else {}
    others = "\n".join(" — ".join(filter(None, [c.get("name") or c.get("role"), c.get("phone") or c.get("email")]))
                       for c in contacts[1:4])
    match = re.search(r'href="(https://[^"]+)"', item.get("card_html") or "")
    stamp = item.get("stamp") or item["taken_at"]  # when taken in work, or when the card was delivered
    taken = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(MSK)
    signal, talk = result.get("signal") or {}, result.get("talk_track") or {}
    return {"customer": (result.get("customer") or {}).get("name"), "buy": signal.get("what_they_buy"),
            "deadline_text": signal.get("deadline"), "deadline": parse_deadline(signal.get("deadline")),
            "name": main.get("name"), "role": main.get("role"), "phone": main.get("phone"), "email": main.get("email"),
            "others": others, "hooks": "\n".join(f"• {h}" for h in (talk.get("hooks") or [])[:4]),
            "comment": _comment_text(item.get("comments")),
            "grade": item.get("grade"), "taken": taken, "link": _safe_link(match.group(1) if match else None)}


class _Styles:
    """Tiny style registry that renders styles.xml, so every look is declared next to where it is used."""

    FONTS = {"base": '<font><sz val="11"/><color rgb="FF1F2937"/><name val="Calibri"/></font>',
             "bold": '<font><b/><sz val="11"/><color rgb="FF1F2937"/><name val="Calibri"/></font>',
             "head": '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',
             "title": '<font><b/><sz val="20"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>',
             "sub": '<font><sz val="11"/><color rgb="FFD7E6EC"/><name val="Calibri"/></font>',
             "link": '<font><u/><sz val="11"/><color rgb="FF0B6AA8"/><name val="Calibri"/></font>',
             "grey": '<font><sz val="10"/><color rgb="FF6B7280"/><name val="Calibri"/></font>',
             "red": '<font><b/><sz val="11"/><color rgb="FFB42318"/><name val="Calibri"/></font>',
             "mute": '<font><i/><sz val="11"/><color rgb="FF9CA3AF"/><name val="Calibri"/></font>'}
    FILLS = {"none": None, "navy": "FF17394A", "teal": "FF2A7F8F", "zebra": "FFF2F7F9", "note": "FFFFF6D5", "a": "FFD3F0DD",
             "b": "FFFCEAB8", "urgent": "FFFDE4E1"}
    NUMFMTS = {"datetime": (164, "dd.mm.yyyy\\ hh:mm"), "date": (165, "dd.mm.yyyy")}

    def __init__(self) -> None:
        self.fonts, self.fills = list(self.FONTS), ["none", "gray125"] + [k for k in self.FILLS if k != "none"]
        self.xfs: list[tuple] = []
        self.index: dict[tuple, int] = {}
        self.get("base", "none", None, "left", "top", False)  # index 0 must be the default style

    def get(self, font, fill, numfmt, halign, valign, wrap, border=True) -> int:
        key = (font, fill, numfmt, halign, valign, wrap, border)
        if key not in self.index:
            self.index[key] = len(self.xfs)
            self.xfs.append(key)
        return self.index[key]

    def xml(self) -> str:
        fills = ['<fill><patternFill patternType="none"/></fill>', '<fill><patternFill patternType="gray125"/></fill>']
        fills += [f'<fill><patternFill patternType="solid"><fgColor rgb="{rgb}"/><bgColor indexed="64"/></patternFill></fill>'
                  for key, rgb in self.FILLS.items() if rgb]
        fill_ids = {"none": 0, "gray125": 1, **{key: i + 2 for i, key in enumerate(k for k, v in self.FILLS.items() if v)}}
        side = '<{0} style="thin"><color rgb="FFD5DEE3"/></{0}>'
        borders = ('<border><left/><right/><top/><bottom/><diagonal/></border><border>'
                   + "".join(side.format(name) for name in ("left", "right", "top", "bottom")) + "<diagonal/></border>")
        xfs = []
        for font, fill, numfmt, halign, valign, wrap, border in self.xfs:
            fmt = self.NUMFMTS[numfmt][0] if numfmt else 0
            xfs.append(f'<xf numFmtId="{fmt}" fontId="{list(self.FONTS).index(font)}" fillId="{fill_ids[fill]}" '
                       f'borderId="{1 if border else 0}" xfId="0" applyFont="1" applyFill="1" applyBorder="1" '
                       f'applyNumberFormat="1" applyAlignment="1"><alignment horizontal="{halign}" vertical="{valign}" '
                       f'wrapText="{1 if wrap else 0}"/></xf>')
        numfmts = "".join(f'<numFmt numFmtId="{i}" formatCode={quoteattr(code)}/>' for i, code in self.NUMFMTS.values())
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                f'<styleSheet xmlns="{_NS}"><numFmts count="{len(self.NUMFMTS)}">{numfmts}</numFmts>'
                f'<fonts count="{len(self.FONTS)}">{"".join(self.FONTS.values())}</fonts>'
                f'<fills count="{len(fills)}">{"".join(fills)}</fills><borders count="2">{borders}</borders>'
                '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
                f'<cellXfs count="{len(xfs)}">{"".join(xfs)}</cellXfs>'
                '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def _short(value, limit) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _lines(text, width) -> int:
    chars = max(6, int(width * 1.18))
    return sum(max(1, math.ceil(len(line) / chars)) for line in str(text or "").split("\n"))


def build_workbook(items: list[dict], *, kind: str = "work", now: datetime | None = None) -> bytes:
    title, date_header, tab_color, hint = KINDS[kind]
    columns = [(date_header if column[2] == "taken" else column[0], column[1], column[2]) for column in COLUMNS]
    now = (now or datetime.now(MSK)).astimezone(MSK)
    def order(row):
        deadline = row["deadline"]
        if deadline is None:
            return (1, now, row["taken"])
        if deadline[0] < now:
            return (2, now - deadline[0], row["taken"])  # long-expired sink to the very bottom
        return (0, deadline[0], row["taken"])
    rows = sorted((lead_row(item) for item in items), key=order)
    styles = _Styles()
    last_col = len(columns)
    cells_xml: list[str] = []
    links: list[tuple[str, str]] = []

    def cell(ref, style, value=None, number=False):
        if value is None or value == "":
            return f'<c r="{ref}" s="{style}"/>'
        if number:
            return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(_clean(value))}</t></is></c>'

    title_style = styles.get("title", "navy", None, "left", "center", False, border=False)
    sub_style = styles.get("sub", "navy", None, "left", "center", False, border=False)
    head_style = styles.get("head", "teal", None, "center", "center", True)
    urgent_count = sum(1 for r in rows if r["deadline"] and now <= r["deadline"][0] <= now + timedelta(hours=URGENT_HOURS))
    subtitle = (f"Выгрузка {now:%d.%m.%Y %H:%M} МСК  ·  лидов: {len(rows)}"
                + (f"  ·  срочно (срок в ближайшие {URGENT_HOURS} ч): {urgent_count}" if urgent_count else "")
                + f"  ·  {hint}  ·  сверху самые срочные, внизу просроченные")
    cells_xml.append(f'<row r="1" ht="40" customHeight="1">{cell("A1", title_style, title)}'
                     + "".join(cell(f"{_col(c)}1", title_style) for c in range(2, last_col + 1)) + "</row>")
    cells_xml.append(f'<row r="2" ht="24" customHeight="1">{cell("A2", sub_style, subtitle)}'
                     + "".join(cell(f"{_col(c)}2", sub_style) for c in range(2, last_col + 1)) + "</row>")
    cells_xml.append('<row r="3" ht="8" customHeight="1"/>')
    cells_xml.append('<row r="4" ht="36" customHeight="1">'
                     + "".join(cell(f"{_col(i)}4", head_style, name) for i, (name, _, _) in enumerate(columns, 1)) + "</row>")

    for n, row in enumerate(rows, 1):
        r = n + 4
        zebra = "zebra" if n % 2 == 0 else "none"
        deadline = row["deadline"]
        urgent = bool(deadline and now <= deadline[0] <= now + timedelta(hours=URGENT_HOURS))
        expired = bool(deadline and deadline[0] < now)
        text = lambda font="base", fill=zebra, h="left": styles.get(font, fill, None, h, "top", True)  # noqa: E731
        out = []
        for c, (header, width, kind) in enumerate(columns, 1):
            ref = f"{_col(c)}{r}"
            if kind == "num":
                out.append(cell(ref, styles.get("grey", zebra, None, "center", "top", False), n, True))
            elif kind == "deadline":
                if deadline:
                    fmt = "datetime" if deadline[1] else "date"
                    font, fill = ("red", "urgent") if urgent else ("mute", zebra) if expired else ("bold", zebra)
                    out.append(cell(ref, styles.get(font, fill, fmt, "center", "top", False), round(_serial(deadline[0]), 10), True))
                else:
                    out.append(cell(ref, text(h="center"), _short(row["deadline_text"], 48) or "—"))
            elif kind == "comment":
                out.append(cell(ref, styles.get("base", "note", None, "left", "top", True), row["comment"]))
            elif kind == "customer":
                out.append(cell(ref, text("bold"), row["customer"]))
            elif kind == "name":
                out.append(cell(ref, text("bold"), row["name"] or "—"))
            elif kind == "phone":
                out.append(cell(ref, text("bold"), row["phone"]))
            elif kind == "grade":
                fill = {"A": "a", "B": "b"}.get(row["grade"], zebra)
                out.append(cell(ref, styles.get("bold", fill, None, "center", "top", False), row["grade"]))
            elif kind == "taken":
                out.append(cell(ref, styles.get("grey", zebra, "datetime", "center", "top", False), round(_serial(row["taken"]), 10), True))
            elif kind == "link":
                if row["link"]:
                    links.append((ref, row["link"]))
                    out.append(cell(ref, styles.get("link", zebra, None, "center", "top", False), "Открыть ↗"))
                else:
                    out.append(cell(ref, text(h="center"), "—"))
            else:
                key = {"Что покупают": "buy", "Должность": "role", "Email": "email", "Другие контакты": "others",
                       "Зацепки для разговора": "hooks"}[header]
                out.append(cell(ref, text(), row[key]))
        height = min(260, max(48, max(_lines(row[k], w) for k, w in
                                      (("customer", 30), ("comment", 38), ("buy", 48), ("role", 24), ("others", 36), ("hooks", 60),
                                       ("phone", 26), ("email", 28))) * 14.5 + 8))
        cells_xml.append(f'<row r="{r}" ht="{height}" customHeight="1">{"".join(out)}</row>')

    last_row = max(5, len(rows) + 4)
    rels = "".join(f'<Relationship Id="rId{i}" Type="{_REL}/hyperlink" Target={quoteattr(url)} TargetMode="External"/>'
                   for i, (_, url) in enumerate(links, 1))
    hyper = ("<hyperlinks>" + "".join(f'<hyperlink ref="{ref}" r:id="rId{i}"/>' for i, (ref, _) in enumerate(links, 1))
             + "</hyperlinks>") if links else ""
    cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>' for i, (_, w, _) in enumerate(columns, 1))
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             f'<worksheet xmlns="{_NS}" xmlns:r="{_REL}"><sheetPr><tabColor rgb="{tab_color}"/><pageSetUpPr fitToPage="1"/></sheetPr>'
             f'<dimension ref="A1:{_col(last_col)}{last_row}"/>'
             '<sheetViews><sheetView workbookViewId="0" showGridLines="0" tabSelected="1" zoomScale="100">'
             '<pane xSplit="4" ySplit="4" topLeftCell="E5" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>'
             f'<sheetFormatPr defaultRowHeight="18"/><cols>{cols}</cols><sheetData>{"".join(cells_xml)}</sheetData>'
             f'<autoFilter ref="A4:{_col(last_col)}{last_row}"/>'
             f'<mergeCells count="2"><mergeCell ref="A1:{_col(last_col)}1"/><mergeCell ref="A2:{_col(last_col)}2"/></mergeCells>'
             f'{hyper}<printOptions horizontalCentered="1"/>'
             '<pageMargins left="0.3" right="0.3" top="0.4" bottom="0.4" header="0.2" footer="0.2"/>'
             '<pageSetup paperSize="9" orientation="landscape" fitToWidth="1" fitToHeight="0"/></worksheet>')
    workbook = (f'<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="{_NS}" xmlns:r="{_REL}">'
                '<bookViews><workbookView activeTab="0"/></bookViews>'
                f'<sheets><sheet name={quoteattr(title)} sheetId="1" r:id="rId1"/></sheets>'
                f'<definedNames><definedName name="_xlnm.Print_Titles" localSheetId="0">{escape(chr(39) + title + chr(39))}!$1:$4</definedName>'
                f'<definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">{escape(chr(39) + title + chr(39))}!$A$4:${_col(last_col)}${last_row}</definedName></definedNames></workbook>')
    parts = {
        "[Content_Types].xml": ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                                '<Default Extension="xml" ContentType="application/xml"/>'
                                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>'),
        "_rels/.rels": (f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                        f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>'),
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": (f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">'
                                       f'<Relationship Id="rId1" Type="{_REL}/worksheet" Target="worksheets/sheet1.xml"/>'
                                       f'<Relationship Id="rId2" Type="{_REL}/styles" Target="styles.xml"/></Relationships>'),
        "xl/styles.xml": styles.xml(),
        "xl/worksheets/sheet1.xml": sheet,
    }
    if links:
        parts["xl/worksheets/_rels/sheet1.xml.rels"] = f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{_PKG}">{rels}</Relationships>'
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body.encode("utf-8"))
    payload = stream.getvalue()
    if len(payload) > 2_000_000:
        raise ValueError("export exceeds safe size")
    return payload

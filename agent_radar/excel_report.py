"""Bounded manager-facing XLSX, generated with the standard library only.

Input is the parent's prepared/unseen snapshot. No fetching, model, store or
mutations: informational delivery fingerprints include work and history too.
"""
from __future__ import annotations

import io
import ipaddress
import re
import zipfile
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from agent_radar.hardware_profile import budget_decision, hardware_signal

MSK = timezone(timedelta(hours=3))
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
ET.register_namespace("", S)
ET.register_namespace("r", R)


def _text(value, limit=1000):
    value = str(value if value is not None else "")
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", value)
    value = re.sub(r"(?i)<\|[^>]*(?:>|$)|</?(?:think|analysis|reasoning|tool_call|function_call|scratchpad)[^>]*(?:>|$)|\[/?(?:inst|analysis|final)\]|<</?sys>>", "", value)
    return value[:limit]


def _url(value):
    if not isinstance(value, str) or len(value) > 1500 or re.search(r"[\s\x00-\x1f\\<>]", value):
        return None
    try:
        p = urlsplit(value)
        host = p.hostname or ""
        if (p.scheme != "https" or p.username or p.password or p.query or p.fragment or
                p.port not in (None, 443) or not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or "." not in host or
                host.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))):
            return None
        try:
            ipaddress.ip_address(host)
            return None
        except ValueError:
            return value
    except ValueError:
        return None


def _contacts(customer):
    result = []
    for c in customer.get("contacts", [])[:20]:
        if not isinstance(c, dict) or c.get("source_kind") not in ("procedure", "official_site", "curated_official") or not _url(c.get("source_url")):
            continue
        phone, email = str(c.get("phone") or ""), str(c.get("email") or "")
        if not re.fullmatch(r"\+?[0-9 (),-]{10,32}(?:\s*\(?(?:доб\.?|вн\.?|ext\.?)\s*[0-9]{1,3}(?:-?[0-9]{1,3})?\)?)?", phone, re.I) or not 10 <= len(re.sub(r"\D", "", re.split(r"доб|вн|ext", phone, flags=re.I)[0])) <= 15:
            phone = ""
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", email) or len(email) > 254:
            email = ""
        if phone and len(set(re.sub(r"\D", "", re.split(r"доб|вн|ext", phone, flags=re.I)[0]))) < 2:
            phone = ""
        if phone or email:
            result.append({**c, "phone": phone, "email": email})
    return result[:5]


def _date(value, *, deadline=False):
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if deadline and len(str(value)) == 10:
            d = datetime.combine(d.date(), time(23, 59, 59))
        return d.replace(tzinfo=MSK) if d.tzinfo is None else d.astimezone(MSK)
    except (ValueError, TypeError):
        return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = Decimal(str(value).replace(" ", "").replace(",", "."))
        return n if n.is_finite() and abs(n) <= 10**15 else None
    except InvalidOperation:
        return None


def _col(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def _xml(root):
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _node(root, name, attrs=None, **kw):
    return ET.SubElement(root, f"{{{S}}}{name}", attrs or {}, **kw)


def _workbook(specs):
    parts = {"xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="2"><numFmt numFmtId="164" formatCode="dd.mm.yy hh:mm"/><numFmt numFmtId="165" formatCode="# ##0.00;[Red]-# ##0.00;0"/></numFmts>
<fonts count="4"><font><sz val="11"/><color rgb="FF243746"/><name val="Calibri"/></font><font><b/><sz val="22"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font><font><u/><sz val="11"/><color rgb="FF087F8C"/><name val="Calibri"/></font></fonts>
<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF112D42"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FF087F8C"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFEAF4F5"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="7"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="2" fillId="3" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="0"/><tableStyles count="0" defaultTableStyle="TableStyleMedium2" defaultPivotStyle="PivotStyleLight16"/>
</styleSheet>'''.encode("utf-8")}
    styles = ET.fromstring(parts["xl/styles.xml"])
    xfs = styles.find(f"{{{S}}}cellXfs")
    for xf in list(xfs):
        striped = ET.fromstring(ET.tostring(xf))
        striped.set("fillId", "4")
        striped.set("applyFill", "1")
        xfs.append(striped)
    xfs.set("count", str(len(xfs)))
    parts["xl/styles.xml"] = _xml(styles)
    wb = ET.Element(f"{{{S}}}workbook")
    _node(_node(wb, "bookViews"), "workbookView", {"activeTab": "0"})
    sh = _node(wb, "sheets")
    relationships = ET.Element("Relationships", xmlns=P)
    content = ET.Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    for ext, mime in (("rels", "application/vnd.openxmlformats-package.relationships+xml"), ("xml", "application/xml")):
        ET.SubElement(content, "Default", Extension=ext, ContentType=mime)
    ET.SubElement(content, "Override", PartName="/xl/workbook.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml")
    for i, (name, title, subtitle, headers, data) in enumerate(specs, 1):
        _node(sh, "sheet", {"name": name, "sheetId": str(i), f"{{{R}}}id": f"rId{i}"})
        ET.SubElement(relationships, "Relationship", Id=f"rId{i}", Type=R + "/worksheet", Target=f"worksheets/sheet{i}.xml")
        ET.SubElement(content, "Override", PartName=f"/xl/worksheets/sheet{i}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
        ws = ET.Element(f"{{{S}}}worksheet")
        _node(_node(ws, "sheetPr"), "pageSetUpPr", {"fitToPage": "1"})
        _node(ws, "dimension", {"ref": f"A1:{_col(len(headers))}{max(4, len(data)+4)}"})
        view = _node(_node(ws, "sheetViews"), "sheetView", {"workbookViewId": "0", "showGridLines": "0", "tabSelected": "1" if i == 1 else "0"})
        _node(view, "pane", {"xSplit": "2" if len(headers) > 2 else "1", "ySplit": "4", "topLeftCell": "C5" if len(headers) > 2 else "B5", "activePane": "bottomRight", "state": "frozen"})
        _node(ws, "sheetFormatPr", {"defaultRowHeight": "20"})
        widths = [14, 24, 30, 17, 28, 21, 27, 28, 32, 12] if len(headers) == 10 else [22, 32, 32, 88, 24, 15] if len(headers) == 6 else [38, 112]
        cols = _node(ws, "cols")
        for k, width in enumerate(widths, 1): _node(cols, "col", {"min": str(k), "max": str(k), "width": str(width), "customWidth": "1"})
        body, links, rels = _node(ws, "sheetData"), [], ET.Element("Relationships", xmlns=P)
        for j, values in enumerate([[title], [subtitle], [], headers] + data, 1):
            height = 42 if j == 1 else 36 if j in (2, 4) else 12 if j == 3 else 112 if len(headers) == 10 else 100 if len(headers) == 6 else 54
            if j >= 5 and len(headers) == 10:
                lines = max(sum(max(1, (len(line) + max(8, width-3)-1) // max(8, width-3)) for line in _text(v[0] if isinstance(v, tuple) else v, 4000).split("\n")) for v, width in zip(values, widths))
                height = min(170, max(112, lines * 14 + 12))
            row = _node(body, "row", {"r": str(j), "ht": str(height), "customHeight": "1"})
            for k, value in enumerate(values + [""] * (len(headers) - len(values)), 1):
                ref, link, location = f"{_col(k)}{j}", None, None
                if isinstance(value, tuple): value, link = value
                if j >= 5 and k == 1 and i <= 3:
                    match = next((n + 5 for n, r in enumerate(specs[3][4]) if r[0] == value), None)
                    if match: location = f"'Детали'!A{match}"
                style = 1 if j == 1 else 2 if j == 4 else 6 if j == 2 else 5 if link or location else 3 if isinstance(value, datetime) else 4 if isinstance(value, Decimal) else 0
                if j >= 5 and j % 2 == 1: style += 7
                cell = _node(row, "c", {"r": ref, "s": str(style)})
                if isinstance(value, datetime):
                    value = Decimal(str((value.astimezone(MSK).replace(tzinfo=None) - datetime(1899, 12, 30)).total_seconds() / 86400))
                if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                    _node(cell, "v").text = str(value)
                else:
                    cell.set("t", "inlineStr")
                    _node(_node(cell, "is"), "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}).text = _text(value, 4000)
                if location: links.append({"ref": ref, "location": location, "display": _text(value)})
                if _url(link):
                    rid = f"rId{len(rels)+1}"
                    ET.SubElement(rels, "Relationship", Id=rid, Type=R + "/hyperlink", Target=link, TargetMode="External")
                    links.append({"ref": ref, f"{{{R}}}id": rid, "display": _text(value)})
        area = f"A4:{_col(len(headers))}{len(data)+4}"
        _node(ws, "autoFilter", {"ref": area})
        merges = _node(ws, "mergeCells", {"count": "2"})
        for row in (1, 2): _node(merges, "mergeCell", {"ref": f"A{row}:{_col(len(headers))}{row}"})
        if links:
            hyper = _node(ws, "hyperlinks")
            for link in links: _node(hyper, "hyperlink", link)
        _node(ws, "printOptions", {"horizontalCentered": "1"})
        _node(ws, "pageMargins", {"left": "0.25", "right": "0.25", "top": "0.35", "bottom": "0.35", "header": "0.15", "footer": "0.15"})
        _node(ws, "pageSetup", {"paperSize": "8", "orientation": "landscape", "fitToWidth": "1", "fitToHeight": "0"})
        if data:
            rid = f"rId{len(rels)+1}"
            ET.SubElement(rels, "Relationship", Id=rid, Type=R + "/table", Target=f"../tables/table{i}.xml")
            _node(_node(ws, "tableParts", {"count": "1"}), "tablePart", {f"{{{R}}}id": rid})
            table = ET.Element(f"{{{S}}}table", {"id": str(i), "name": f"Radar{i}", "displayName": f"Radar{i}", "ref": area, "totalsRowShown": "0"})
            _node(table, "autoFilter", {"ref": area})
            columns = _node(table, "tableColumns", {"count": str(len(headers))})
            for k, h in enumerate(headers, 1): _node(columns, "tableColumn", {"id": str(k), "name": h})
            _node(table, "tableStyleInfo", {"name": "TableStyleMedium2", "showFirstColumn": "0", "showLastColumn": "0", "showRowStripes": "1", "showColumnStripes": "0"})
            parts[f"xl/tables/table{i}.xml"] = _xml(table)
            ET.SubElement(content, "Override", PartName=f"/xl/tables/table{i}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml")
        parts[f"xl/worksheets/_rels/sheet{i}.xml.rels"] = _xml(rels)
        parts[f"xl/worksheets/sheet{i}.xml"] = _xml(ws)
    names = _node(wb, "definedNames")
    for i, spec in enumerate(specs):
        name, _, _, headers, data = spec
        _node(names, "definedName", {"name": "_xlnm.Print_Titles", "localSheetId": str(i)}).text = f"'{name}'!$1:$4"
        _node(names, "definedName", {"name": "_xlnm.Print_Area", "localSheetId": str(i)}).text = f"'{name}'!$A$1:${_col(len(headers))}${len(data)+4}"
    ET.SubElement(relationships, "Relationship", Id="rIdStyles", Type=R + "/styles", Target="styles.xml")
    ET.SubElement(content, "Override", PartName="/xl/styles.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml")
    parts["xl/workbook.xml"] = _xml(wb)
    parts["xl/_rels/workbook.xml.rels"] = _xml(relationships)
    parts["[Content_Types].xml"] = _xml(content)
    root = ET.Element("Relationships", xmlns=P)
    ET.SubElement(root, "Relationship", Id="rId1", Type=R + "/officeDocument", Target="xl/workbook.xml")
    parts["_rels/.rels"] = _xml(root)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    payload = stream.getvalue()
    if len(parts) > 200 or sum(len(p) for p in parts.values()) > 10_000_000 or len(payload) > 2_000_000:
        raise ValueError("XLSX exceeds safe report limits")
    return payload


def build_excel_report(prepared: dict, *, now: datetime | None = None) -> tuple[str, bytes, list[str]]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("aware report time required")
    stamp = _date(prepared.get("collected_at"))
    stale = bool(prepared.get("stale")) or stamp is None or (now - stamp).total_seconds() > 2700
    groups = {name: [] for name in ("Лиды", "Доработка", "История")}
    details, fingerprints = [], []
    totals = {"Без контакта": 0, "Финансы подтверждены": 0, "Документы не прочитаны": 0, "Разбор не завершён": 0, "Пробелы по файлам": 0}
    for item in prepared.get("cards", [])[:100]:
        card, a, o = item["card"], item.get("analysis") or {}, item.get("opportunity") or {}
        buyer, customer = o.get("buyer") or {}, o.get("customer_intelligence") or {}
        finance, contacts = customer.get("finance") or {}, _contacts(customer)
        deadline, status = _date(o.get("acceptance_end_date"), deadline=True), o.get("status")
        budget = o.get("budget") or {}
        decision = budget_decision(budget, minimum_rub=budget.get("minimum_rub", 5_000_000))
        reasons = []
        if stale: reasons.append("Данные устарели: обновление задержалось более 45 минут")
        if not hardware_signal(card): reasons.append("Не подтверждена профильная поставка физических серверов / СХД")
        if not contacts: reasons.append("Нет опубликованного рабочего контакта")
        if item.get("analysis_state") != "complete": reasons.append("Разбор не завершён: " + _text(item.get("analysis_state") or "неизвестно", 30))
        elif a.get("interesting") is not True: reasons.append("Повод для разговора не подтверждён разбором")
        if decision["decision"] == "below_threshold": reasons.append("Известный бюджет ниже порога")
        if status != "proposal": reasons.append("Активный приём предложений не подтверждён")
        if o.get("acceptance_end_date") and deadline is None: reasons.append("Срок приёма требует проверки")
        if o.get("deadline_conflict"): reasons.append("Срок в API и документах расходится — уточнить у заказчика")
        historical = status in ("ended", "summarizing", "completed", "closed", "cancelled") or (deadline is not None and deadline <= now)
        group = "История" if historical else "Доработка" if reasons else "Лиды"
        reason = "Исторический сигнал: " + ("срок приёма истёк" if deadline and deadline <= now else _text(status)) if historical else "; ".join(reasons)
        key, name = _text(card.get("id"), 150), _text(buyer.get("legal_name") or card.get("customer_name") or buyer.get("name") or "Заказчик не указан", 180)
        contact = contacts[0] if contacts else {}
        if finance.get("state") == "verified":
            compact = lambda field: ((format(_number(finance.get(field)) / 1_000_000, ".2f").rstrip("0").rstrip(".") + " млн ₽") if abs(_number(finance.get(field))) >= 1_000_000 else format(_number(finance.get(field)), ",.0f").replace(",", " ") + " ₽") if _number(finance.get(field)) is not None else "неизвестна"
            econ = f"{finance.get('year')}: Выручка {compact('revenue_rub')}; прибыль {compact('net_profit_rub')}. Не гарантия платёжеспособности"
        else:
            econ = "Отчётность неизвестна; не нулевая выручка"
        if finance.get("state") == "verified" and any((_number(finance.get(k)) or 0) < 0 for k in ("net_profit_rub", "equity_rub")):
            econ = "Проверить риски убытка / отрицательного капитала. " + econ
        price = _number(decision["amount"]) if decision["amount"] is not None else "Бюджет неизвестен"
        need = _text(card.get("title"), 180) + ("\nГипотеза: " + _text(a.get("summary"), 230) if a else "")
        action = reason or "Связаться и уточнить: " + _text((a.get("questions") or ["Объём поставки и совместимость?"])[0], 150)
        general = any(contact.get(field) == "general_business" for field in ("route_kind", "contact_scope", "scope"))
        route = _text(contact.get("name") or "Имя не опубликовано", 150) + "\n" + _text(contact.get("role") or "Роль не указана", 100)
        if general:
            route += "\nОбщий корпоративный маршрут, не закупщик / ИТ-ЛПР"
            if not reason: action = "Попросить соединить с закупками / ИТ по поставке серверов и СХД"
        economy = ("Бюджет: " + format(price, ",.0f").replace(",", " ") + " ₽" if isinstance(price, Decimal) else price) + "\n" + econ
        groups[group].append([key, name, need, deadline or "Не указан", route, contact.get("phone") or "Не найден", contact.get("email") or "Не найдена", action, economy, ("Процедура", _url(card.get("source_url")))])
        details += [[key, "Карточка", "ИНН из процедуры", _text(buyer.get("inn") or "Не указан", 30), "", ""],
                    [key, "Статус", group, reason or "Предварительный лид для первого обращения", "", ""],
                    [key, "Документы", "Технические тексты / пробелы", f"{item.get('texts', 0)} / {item.get('gaps', 0)}; " + ("ограниченные фрагменты" if item.get("excerpt_only") else "полнота не гарантируется"), "", ""]]
        details.append([key, "Карточка", "Название процедуры", _text(card.get("title"), 1000), ("Процедура", _url(card.get("source_url"))), ""])
        conflict = o.get("deadline_conflict")
        if conflict:
            details.append([key, "Расхождение сроков", "API", deadline or "Срок API неизвестен", "Уточнить актуальный срок у заказчика", ""])
            if isinstance(conflict, dict):
                for field in ("source", "quote", "document_source", "document_quote", "api_deadline", "document_deadline"):
                    if conflict.get(field): details.append([key, "Расхождение сроков", field, _text(conflict[field], 350), "Буквальные сведения переданы проверенным подготовленным пакетом", ""])
        if o.get("publication_date"): details.append([key, "Карточка", "Опубликовано (МСК)", _date(o["publication_date"]) or "Дата требует проверки", "", ""])
        details.append([key, "Бюджет закупки", _text(decision["basis"]), price, "Общая цена процедуры / договора, не выручка заказчика", "RUB"])
        if a: details.append([key, "Гипотеза интереса (не факт)", "Разбор", _text(a.get("summary"), 350), "", ""])
        for question in a.get("questions", [])[:2]: details.append([key, "Вопрос для квалификации", "Уточнить", _text(question, 200), "", ""])
        if not item.get("texts"): details.append([key, "Пробел документации", "Технические требования", "Документы не прочитаны / не предоставлены; это не доказательство их отсутствия", "", ""])
        for q in a.get("evidence", [])[:6]:
            if isinstance(q, dict): details.append([key, "Основание (цитата)", _text(q.get("source"), 100), _text(q.get("quote"), 250), "", ""])
        for finding in a.get("document_findings", [])[:2]:
            if any(q.get("source") != "card" and q.get("quote") == finding for q in a.get("evidence", []) if isinstance(q, dict)):
                details.append([key, "Требование (буквальная цитата)", "Из документа", _text(finding, 200), "", ""])
        details.append([key, "Что сказать (сценарий, не факт)", "Первое обращение", "Здравствуйте. В вашей процедуре указано: «" + _text((a.get("evidence") or [{}])[0].get("quote") or card.get("title"), 250) + "». Мы поставляем серверы и СХД. " + ("Планируются ли следующие этапы поставки?" if historical else "Соедините, пожалуйста, с закупками или ИТ, чтобы обсудить эту поставку." if general else "Можно уточнить совместимость и объём поставки?"), "", ""])
        for c in contacts:
            details.append([key, "Опубликованный контакт", _text(c.get("name") or "Имя не опубликовано", 150) + "; " + _text(c.get("role") or "роль не указана", 100), c["phone"] + " " + c["email"] + ("\nПериод источника: " + _text(c["source_period"], 80) if c.get("source_period") else ""), ("Источник контакта", c["source_url"]), ""])
        if finance.get("state") == "verified":
            for label, field in (("Выручка, RUB", "revenue_rub"), ("Чистая прибыль, RUB", "net_profit_rub"), ("Капитал, RUB", "equity_rub")):
                number = _number(finance.get(field))
                details.append([key, "Финансы: не текущая платёжеспособность", label, number if number is not None else "Неизвестно", ("Источник финансов", _url(finance.get("source_url"))) if _url(finance.get("source_url")) else "Источник неизвестен", finance.get("year") or "Год неизвестен"])
        for gap in customer.get("gaps", [])[:5]: details.append([key, "Пробел проверки", "Заказчик", _text(gap, 300), "", ""])
        fingerprints.append(item["fingerprint"])
        totals["Без контакта"] += not contacts and not historical
        totals["Финансы подтверждены"] += finance.get("state") == "verified"
        totals["Документы не прочитаны"] += not item.get("texts")
        totals["Разбор не завершён"] += item.get("analysis_state") != "complete"
        totals["Пробелы по файлам"] += item.get("gaps", 0) if isinstance(item.get("gaps", 0), int) else len(item.get("gaps", []))
    headers = ["ID / детали", "Заказчик", "Потребность / гипотеза", "Приём до (МСК)", "Контакт / маршрут", "Телефон", "Почта", "Следующее действие / причина", "Экономика / бюджет", "Ссылка"]
    subtitle = f"Данные получены: {stamp:%d.%m.%Y %H:%M} МСК" if stamp else "Время получения данных неизвестно"
    subtitle += " · " + " / ".join(f"{k}: {len(v)}" for k, v in groups.items())
    summary = [["Готовых к первому обращению", len(groups["Лиды"])], ["В доработке", len(groups["Доработка"])], ["Исторические сигналы", len(groups["История"])], *[[k, v] for k, v in totals.items()],
        ["Покрытие", "Последние 14 дней изменений по lastModified, не реестр всех активных процедур"],
        ["Граница источника", "Запас 5 минут; полнота этих 5 минут не заявляется"],
        ["Бюджет", "Неизвестный бюджет включается без оценки цены; известная общая цена проверяется по порогу"],
        ["Предупреждение", "Закупка — повод для квалификации, не утверждённая возможность продажи. Отчётность не гарантирует текущую платёжеспособность."],
        ["Свежесть", "Данные устарели; активные карточки сохранены в доработке" if stale else "Данные не старше 45 минут"]]
    coverage = prepared.get("coverage") or {}
    summary += [["Финансовые данные неизвестны", len(fingerprints) - totals["Финансы подтверждены"]],
        ["Накоплено / рассмотрено", f"{prepared.get('history_total', len(fingerprints))} / {prepared.get('considered', len(fingerprints))}"],
        ["Осталось вне разбора (лимит прохода)", max(0, prepared.get("history_total", len(fingerprints)) - prepared.get("considered", len(fingerprints)))],
        ["Разбор не завершён во всём проходе", prepared.get("analysis_failures", 0)],
        ["Текущий проход источника", f"{coverage.get('response_rows', 'Неизвестно')} строк; завершение: {coverage.get('termination', 'Неизвестно')}; начало: {coverage.get('from_date', 'Неизвестно')}"],
        ["Пропуск сбора", "Был перерыв / первичный сбор: полнота накопления не гарантируется" if prepared.get("source_gap_before_24h") else "Перерыв не отмечен; полное покрытие площадки не заявляется"]]
    specs = [(k, "СЕРВЕРЫ И СХД · " + k.upper(), subtitle, headers, v or ([["Готовых лидов нет", "", "Нет активных профильных карточек с завершённым разбором и опубликованным контактом; см. Доработка / История"]] if k == "Лиды" else [])) for k, v in groups.items()]
    specs += [("Детали", "ОСНОВАНИЯ И ПРОВЕРКА", subtitle, ["ID", "Раздел", "Поле / источник цитаты", "Значение / текст", "Источник", "Год"], details), ("Сводка", "СВОДКА ПОДБОРКИ", subtitle, ["Показатель", "Значение"], summary)]
    caption = f"Серверы и СХД · {now.astimezone(MSK):%d.%m.%Y %H:%M} МСК\nГотовых к первому обращению: {len(groups['Лиды'])}. В доработке: {len(groups['Доработка'])}. История: {len(groups['История'])}.\nExcel: контакты, основания, бюджет и отчётность с указанием года."
    if stale: caption += "\nОбновление задержалось; готовые лиды не заявляются."
    return caption, _workbook(specs), fingerprints

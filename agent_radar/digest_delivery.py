"""Single-response Excel/legacy Markdown delivery via the caller's existing bot.

Authorization and report preparation belong to the caller. No bot construction,
model invocation, poller, or automatic retry is performed by this module.
"""
from __future__ import annotations

import asyncio
import io
import re

from agent_radar.digest_store import DigestStore


def validate_fingerprints(values: list[str]) -> list[str]:
    """Validate journal-compatible fingerprints before any transport attempt."""
    if type(values) is not list or len(values) > 5000:
        raise ValueError("invalid report fingerprints")
    if any(type(value) is not str or not value or len(value) > 256 for value in values):
        raise ValueError("invalid report fingerprint")
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("report fingerprint contains invalid Unicode") from exc
    return sorted(set(values))


def validate_report(caption: str, report: str, fingerprints: list[str]) -> None:
    """Reject unsafe output; never truncate or strip the caller's exact report.

    Caption is limited to 1000 UTF-16 units; Markdown to 400000 UTF-8 bytes.
    Only fingerprints in this complete, single-response report may be marked seen.
    """
    if type(caption) is not str or type(report) is not str or not report.strip():
        raise ValueError("invalid report text")
    try:
        caption_units = len(caption.encode("utf-16-le")) // 2
        report_bytes = len(report.encode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("report contains invalid Unicode") from exc
    if caption_units > 1000:
        raise ValueError("report caption exceeds 1000 UTF-16 units")
    if report_bytes > 400000:
        raise ValueError("report exceeds 400000 UTF-8 bytes")
    markers = ("<|", "[inst]", "[/inst]", "<think", "</think", "<analysis", "</analysis",
               "<reasoning", "</reasoning", "[analysis]", "[final]", "<<sys>>", "<tool_call",
               "<function_call", "<scratchpad", "</scratchpad")
    if any(marker in text.lower() for text in (caption, report) for marker in markers):
        raise ValueError("report contains internal model markers")
    validate_fingerprints(fingerprints)


def _forbidden(error: Exception) -> bool:
    # Import the optional transport type only when handling a transport failure.
    try:
        from telegram.error import Forbidden
    except ImportError:
        return False
    return isinstance(error, Forbidden)


def validate_excel_report(caption:str,payload:bytes,fingerprints:list[str])->None:
    """Accept only the bounded, passive OOXML subset emitted by our renderer.

    Print ranges are the sole permitted defined names. Relationships cannot
    smuggle external data or local-file links around the formula/cell checks.
    This is deliberately not a validator for arbitrary user-uploaded workbooks.
    """
    import ipaddress
    import posixpath
    import zipfile
    from decimal import Decimal, InvalidOperation
    from urllib.parse import urlsplit
    from xml.etree import ElementTree as ET
    validate_report(caption,'Excel report',fingerprints)
    if type(payload) is not bytes or not 100<=len(payload)<=2_000_000:raise ValueError('invalid Excel report bytes')
    spreadsheet='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    office='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package='http://schemas.openxmlformats.org/package/2006/relationships'
    content='http://schemas.openxmlformats.org/package/2006/content-types'
    allowed_parts={
        '[Content_Types].xml':('Types',content,'Types Default Override'),
        '_rels/.rels':('Relationships',package,'Relationships Relationship'),
        'xl/workbook.xml':('workbook',spreadsheet,'workbook bookViews workbookView sheets sheet definedNames definedName'),
        'xl/_rels/workbook.xml.rels':('Relationships',package,'Relationships Relationship'),
        'xl/styles.xml':('styleSheet',spreadsheet,'styleSheet numFmts numFmt fonts font sz color name b u fills fill patternFill fgColor bgColor borders border left right top bottom diagonal cellStyleXfs xf alignment cellXfs cellStyles cellStyle dxfs tableStyles'),
    }
    for n in range(1,6):
        allowed_parts[f'xl/worksheets/sheet{n}.xml']=('worksheet',spreadsheet,'worksheet sheetPr pageSetUpPr dimension sheetViews sheetView pane sheetFormatPr cols col sheetData row c v is t autoFilter mergeCells mergeCell hyperlinks hyperlink printOptions pageMargins pageSetup tableParts tablePart')
        allowed_parts[f'xl/worksheets/_rels/sheet{n}.xml.rels']=('Relationships',package,'Relationships Relationship')
        allowed_parts[f'xl/tables/table{n}.xml']=('table',spreadsheet,'table autoFilter tableColumns tableColumn tableStyleInfo')
    mime_prefix='application/vnd.openxmlformats-officedocument.spreadsheetml.'
    mime_by_part={'xl/workbook.xml':mime_prefix+'sheet.main+xml','xl/styles.xml':mime_prefix+'styles+xml'}
    for n in range(1,6):
        mime_by_part[f'xl/worksheets/sheet{n}.xml']=mime_prefix+'worksheet+xml'
        mime_by_part[f'xl/tables/table{n}.xml']=mime_prefix+'table+xml'

    def safe_link(value):
        if not isinstance(value,str) or len(value)>1500 or re.search(r'[\s\x00-\x1f\\<>]',value):return False
        try:
            parsed=urlsplit(value);host=parsed.hostname or ''
            if (parsed.scheme!='https' or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port not in (None,443)
                    or not re.fullmatch(r'[a-zA-Z0-9.-]+',host) or '.' not in host or host.endswith(('.localhost','.local','.internal','.invalid','.test'))):return False
            try:ipaddress.ip_address(host);return False
            except ValueError:return True
        except ValueError:return False

    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            rows=archive.infolist();names=[row.filename for row in rows]
            if len(rows)>200 or len(names)!=len(set(names)) or sum(row.file_size for row in rows)>10_000_000:raise ValueError('Excel archive budget')
            if not {'[Content_Types].xml','xl/workbook.xml','xl/worksheets/sheet1.xml'}<=set(names):raise ValueError('not an Excel workbook')
            trees={}
            for row in rows:
                name=row.filename
                if name not in allowed_parts or row.flag_bits&1 or row.file_size>2_000_000:raise ValueError('unsafe Excel package part')
                data=archive.read(row)
                try:xml=data.decode('utf-8')
                except UnicodeError as error:raise ValueError('Excel XML must be UTF-8') from error
                if '\x00' in xml or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():raise ValueError('XML declaration forbidden')
                tree=ET.fromstring(xml);root,namespace,tags=allowed_parts[name]
                if tree.tag!='{'+namespace+'}'+root:raise ValueError('unexpected Excel XML root')
                permitted={'{'+namespace+'}'+tag for tag in tags.split()}
                if any(node.tag not in permitted for node in tree.iter()):raise ValueError('unexpected Excel XML element')
                if any(attr.startswith('{') and attr not in ('{'+office+'}id','{http://www.w3.org/XML/1998/namespace}space') for node in tree.iter() for attr in node.attrib):raise ValueError('unexpected Excel XML attribute namespace')
                trees[name]=tree
            workbook=trees['xl/workbook.xml']
            sheet_names=[node.get('name') for node in workbook.findall('{'+spreadsheet+'}sheets/{'+spreadsheet+'}sheet')]
            if not 1<=len(sheet_names)<=5 or any(not isinstance(value,str) or not value or len(value)>31 or re.search(r"[\\/*?:\[\]']",value) for value in sheet_names) or len(set(sheet_names))!=len(sheet_names):raise ValueError('invalid Excel sheet names')
            for node in workbook.iter('{'+spreadsheet+'}definedName'):
                kind=node.get('name');local=node.get('localSheetId','')
                if kind not in ('_xlnm.Print_Area','_xlnm.Print_Titles') or not re.fullmatch(r'[0-4]',local) or int(local)>=len(sheet_names):raise ValueError('Excel named formulas forbidden')
                expression=node.text or '';prefix="'"+sheet_names[int(local)]+"'!"
                suffix=expression[len(prefix):] if expression.startswith(prefix) else ''
                pattern=r'\$1:\$4' if kind=='_xlnm.Print_Titles' else r'\$A\$1:\$[A-Z]{1,3}\$[1-9][0-9]{0,5}'
                if set(node.attrib)-{'name','localSheetId'} or list(node) or not re.fullmatch(pattern,suffix):raise ValueError('invalid Excel print range')
            for name,tree in trees.items():
                if name=='[Content_Types].xml':
                    for node in tree:
                        if node.tag=='{'+content+'}Default':
                            expected={'xml':'application/xml','rels':'application/vnd.openxmlformats-package.relationships+xml'}
                            if set(node.attrib)!={'Extension','ContentType'} or node.get('ContentType')!=expected.get(node.get('Extension')):raise ValueError('unexpected Excel content type')
                        else:
                            part=node.get('PartName','');target=part[1:] if part.startswith('/') else ''
                            if set(node.attrib)!={'PartName','ContentType'} or target not in trees or node.get('ContentType')!=mime_by_part.get(target):raise ValueError('unexpected Excel content type')
                elif name.endswith('.rels'):
                    if name=='_rels/.rels':owner='';internal={'officeDocument':{'xl/workbook.xml'}}
                    elif name=='xl/_rels/workbook.xml.rels':owner='xl';internal={'worksheet':{f'xl/worksheets/sheet{n}.xml' for n in range(1,6)},'styles':{'xl/styles.xml'}}
                    else:
                        owner='xl/worksheets';number=re.search(r'sheet([1-5])\.xml',name).group(1)
                        internal={'table':{f'xl/tables/table{number}.xml'}}
                    ids=set()
                    for node in tree:
                        target=node.get('Target','');kind=node.get('Type','');mode=node.get('TargetMode','Internal');rid=node.get('Id')
                        if (node.tag!='{'+package+'}Relationship' or set(node.attrib)-{'Id','Type','Target','TargetMode'} or not rid or rid in ids or list(node)):raise ValueError('invalid Excel relationship')
                        ids.add(rid)
                        if mode=='External':
                            if owner!='xl/worksheets' or kind!=office+'/hyperlink' or not safe_link(target):raise ValueError('unsafe external Excel relationship')
                        elif mode=='Internal':
                            resolved=posixpath.normpath(posixpath.join(owner,target))
                            if ('\\' in target or target.startswith('/') or kind not in {office+'/'+value for value in internal}
                                    or resolved not in internal.get(kind.removeprefix(office+'/'),set()) or resolved not in trees):raise ValueError('unsafe internal Excel relationship')
                        else:raise ValueError('invalid Excel relationship mode')
                elif name.startswith('xl/worksheets/'):
                    for cell in tree.iter('{'+spreadsheet+'}c'):
                        children=list(cell);kind=cell.get('t','n')
                        if set(cell.attrib)-{'r','s','t'} or kind not in ('inlineStr','n'):raise ValueError('unexpected Excel cell type')
                        if kind=='inlineStr':
                            if len(children)!=1 or children[0].tag!='{'+spreadsheet+'}is' or children[0].attrib or len(children[0])!=1 or children[0][0].tag!='{'+spreadsheet+'}t' or list(children[0][0]):raise ValueError('unsafe inline Excel cell')
                        else:
                            if len(children)!=1 or children[0].tag!='{'+spreadsheet+'}v' or children[0].attrib or list(children[0]):raise ValueError('unsafe numeric Excel cell')
                            raw=children[0].text or ''
                            if not re.fullmatch(r'-?[0-9]{1,16}(?:\.[0-9]{1,20})?(?:[Ee][+-]?[0-9]{1,3})?',raw):raise ValueError('invalid Excel numeric cell')
                            try:
                                value=Decimal(raw)
                                if not value.is_finite() or abs(value)>10**15:raise ValueError('Excel numeric cell budget')
                            except InvalidOperation as error:raise ValueError('invalid Excel numeric cell') from error
                    for link in tree.iter('{'+spreadsheet+'}hyperlink'):
                        if link.get('location') and not re.fullmatch(r"'"+re.escape(sheet_names[3] if len(sheet_names)>3 else sheet_names[0])+r"'!A[1-9][0-9]{0,5}",link.get('location')):raise ValueError('unsafe internal Excel hyperlink')
            if archive.testzip() is not None:raise ValueError('Excel archive CRC mismatch')
    except (zipfile.BadZipFile,ET.ParseError,KeyError,RuntimeError,OSError) as error:raise ValueError('invalid Excel report package') from error


async def deliver_report(store: DigestStore, slot: str, recipients, render, bot,
                         *, sleep=asyncio.sleep) -> dict[str, int]:
    return await _deliver_document(store,slot,recipients,render,bot,sleep=sleep,excel=False)


async def deliver_excel_report(store: DigestStore,slot:str,recipients,render,bot,*,sleep=asyncio.sleep)->dict[str,int]:
    return await _deliver_document(store,slot,recipients,render,bot,sleep=sleep,excel=True)


async def _deliver_document(store: DigestStore,slot:str,recipients,render,bot,*,sleep,excel):
    """Send one document per unique private chat, reserving before transport.

    ``render(chat_id)`` returns ``(caption, report, fingerprints)`` synchronously.
    The caller supplies currently authorized recipients from the public journal.
    Counts contain neither user identifiers nor exception messages.
    """
    counts = {"sent": 0, "skipped": 0, "uncertain": 0, "blocked": 0}
    recipients = tuple(recipients)
    if any(type(chat) is not int or not 0 < chat <= 2**63 - 1 for chat in recipients):
        raise ValueError("report requires positive private chat ids")
    recipients = tuple(dict.fromkeys(recipients))
    for chat_id in recipients:
        if store.delivery_state(slot, chat_id) is not None:
            counts["skipped"] += 1
            continue
        caption, report, fingerprints = render(chat_id)
        if excel:validate_excel_report(caption,report,fingerprints)
        else:validate_report(caption,report,fingerprints)
        fingerprints = validate_fingerprints(fingerprints)
        # Rendering/preparation is not permission to send. Re-read current opt-outs
        # immediately before the public journal's durable reservation.
        if chat_id not in store.recipients(set(recipients)):
            counts["skipped"] += 1
            continue
        if not store.claim_delivery(slot, chat_id, require_subscription=True):
            counts["skipped"] += 1
            continue
        try:
            receipt = await bot.send_document(
                chat_id=chat_id, document=io.BytesIO(report if excel else report.encode("utf-8")),
                filename="servers-storage-report-" + re.sub(r"[^A-Za-z0-9-]", "-", slot) + (".xlsx" if excel else ".md"),
                caption=caption,
                parse_mode=None,
            )
            store.record_delivery(slot, chat_id, receipt.message_id, fingerprints)
            counts["sent"] += 1
        except asyncio.CancelledError:
            store.record_failure(slot, chat_id, "cancelled")
            raise
        except Exception as error:
            if _forbidden(error):
                store.record_failure(slot, chat_id, "forbidden")
                store.unsubscribe(chat_id)
                counts["blocked"] += 1
            else:
                store.record_failure(slot, chat_id, "uncertain")
                counts["uncertain"] += 1
        await sleep(0.2)
    return counts

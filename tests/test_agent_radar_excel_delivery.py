"""Binary Excel delivery preserves the existing durable at-most-once boundary."""
import io
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import zipfile
from agent_radar.digest_store import DigestStore


def harmless_xlsx():
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>')
        archive.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets><sheet name="Лиды" sheetId="1"/></sheets></workbook>')
        archive.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Поставка серверов</t></is></c></row></sheetData></worksheet>')
    return output.getvalue()


class ExcelDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_excel_bytes_sent_once_and_optout_is_still_atomic(self):
        from agent_radar.digest_delivery import deliver_excel_report
        with TemporaryDirectory() as root:
            store=DigestStore(Path(root)/'digests.sqlite3');store.seed({101,202})
            calls=[];payload=harmless_xlsx()
            class Bot:
                async def send_document(self,**kwargs):
                    selftest.assertEqual(store.delivery_state('excel-v1',kwargs['chat_id']),'claimed')
                    calls.append(kwargs);return SimpleNamespace(message_id=7)
            selftest=self
            async def sleep(_):pass
            def render(chat):
                if chat==202:store.unsubscribe(chat)
                return 'Готовые лиды: 1',payload,['procurement-ready-1']
            result=await deliver_excel_report(store,'excel-v1',[101,202],render,Bot(),sleep=sleep)
            self.assertEqual(result,{'sent':1,'skipped':1,'uncertain':0,'blocked':0})
            self.assertTrue(calls[0]['filename'].endswith('.xlsx'))
            self.assertEqual(calls[0]['document'].getvalue(),payload)
            self.assertEqual(store.unseen(101,['procurement-ready-1']),set())
            replay=await deliver_excel_report(store,'excel-v1',[101],lambda _:self.fail('No rerender on replay'),Bot(),sleep=sleep)
            self.assertEqual(replay['sent'],0)
            self.assertEqual(len(calls),1)

    async def test_formulas_embedded_objects_macros_and_nonworkbook_fail_before_claim(self):
        from agent_radar.digest_delivery import deliver_excel_report
        def unsafe(name,text):
            buffer=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(harmless_xlsx())) as original,zipfile.ZipFile(buffer,'w') as target:
                for item in original.infolist():target.writestr(item,original.read(item))
                target.writestr(name,text)
            return buffer.getvalue()
        with TemporaryDirectory() as root:
            store=DigestStore(Path(root)/'digests.sqlite3');store.seed({101})
            for payload in (b'not-excel',unsafe('xl/vbaProject.bin','macro'),unsafe('xl/embeddings/oleObject1.bin','object'),unsafe('xl/worksheets/sheet2.xml','<worksheet><c><f>WEBSERVICE("https://bad.test")</f></c></worksheet>')):
                with self.subTest(payload=payload[:12]),self.assertRaises(ValueError):
                    await deliver_excel_report(store,'invalid',[101],lambda _:('caption',payload,[]),SimpleNamespace())
                self.assertIsNone(store.delivery_state('invalid',101))

    async def test_unsafe_names_relationships_parts_and_cells_fail_before_reservation(self):
        from agent_radar.digest_delivery import deliver_excel_report
        from xml.etree import ElementTree as ET
        spreadsheet='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
        relationships='http://schemas.openxmlformats.org/package/2006/relationships'
        office='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        def modified(change):
            with zipfile.ZipFile(io.BytesIO(harmless_xlsx())) as archive:
                parts={name:archive.read(name) for name in archive.namelist()}
            change(parts)
            output=io.BytesIO()
            with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
                for name,data in parts.items():archive.writestr(name,data)
            return output.getvalue()
        def named(parts,expression,name='leak'):
            root=ET.fromstring(parts['xl/workbook.xml'])
            names=ET.SubElement(root,'{'+spreadsheet+'}definedNames')
            ET.SubElement(names,'{'+spreadsheet+'}definedName',name=name,localSheetId='0').text=expression
            parts['xl/workbook.xml']=ET.tostring(root)
        def relation(parts,target,kind='hyperlink',mode='External'):
            root=ET.Element('Relationships',xmlns=relationships)
            ET.SubElement(root,'Relationship',Id='rId1',Type=office+kind,Target=target,TargetMode=mode)
            parts['xl/worksheets/_rels/sheet1.xml.rels']=ET.tostring(root)
        def cell(parts,content):
            parts['xl/worksheets/sheet1.xml']=('<worksheet xmlns="'+spreadsheet+'"><sheetData><row r="1">'+content+'</row></sheetData></worksheet>').encode()
        mutations=[
            ('named formula',lambda p:named(p,'WEBSERVICE("https://evil.example/")')),
            ('print name formula',lambda p:named(p,'WEBSERVICE("https://evil.example/")','_xlnm.Print_Area')),
            ('unknown print sheet',lambda p:named(p,"'Other'!$A$1:$L$5",'_xlnm.Print_Area')),
            ('file link',lambda p:relation(p,'file:///C:/Windows/System32/calc.exe')),
            ('signed link',lambda p:relation(p,'https://example.org/?token=secret')),
            ('private link',lambda p:relation(p,'https://127.0.0.1/')),
            ('external data',lambda p:relation(p,'https://example.org/','worksheet')),
            ('internal escape',lambda p:relation(p,'../../../outside.xml','table','Internal')),
            ('unexpected part',lambda p:p.update({'xl/custom.xml':b'<payload/>'})),
            ('cell extension',lambda p:cell(p,'<c r="A1" t="inlineStr"><is><t>safe</t></is><extLst/></c>')),
            ('unexpected cell type',lambda p:cell(p,'<c r="A1" t="str"><v>WEBSERVICE()</v></c>')),
            ('nonnumeric numeric cell',lambda p:cell(p,'<c r="A1"><v>WEBSERVICE()</v></c>')),
            ('UTF16 entity declaration',lambda p:p.update({'xl/worksheets/sheet1.xml':('<?xml version="1.0" encoding="utf-16"?><!DOCTYPE worksheet [<!ENTITY text "unsafe">]><worksheet xmlns="'+spreadsheet+'"><sheetData><row><c t="inlineStr"><is><t>&text;</t></is></c></row></sheetData></worksheet>').encode('utf-16')})),
        ]
        with TemporaryDirectory() as root:
            store=DigestStore(Path(root)/'digests.sqlite3');store.seed({101})
            class Bot:
                async def send_document(self,**kwargs):
                    raise AssertionError('Unsafe workbook reached transport')
            for label,change in mutations:
                with self.subTest(label=label),self.assertRaises(ValueError):
                    await deliver_excel_report(store,'unsafe-'+label,[101],lambda _:('caption',modified(change),['unseen']),Bot())
                self.assertIsNone(store.delivery_state('unsafe-'+label,101))
                self.assertEqual(store.unseen(101,['unseen']),{'unseen'})

    def test_generated_report_print_ranges_safe_links_and_literal_formula_text_are_accepted(self):
        from datetime import datetime,timezone
        from agent_radar.excel_report import build_excel_report
        from agent_radar.digest_delivery import validate_excel_report
        now=datetime(2026,9,30,10,tzinfo=timezone.utc)
        item={'card':{'id':'safe','title':'Поставка серверов','source_url':'https://tenders.example.org/process/safe'},
              'fingerprint':'safe','analysis_state':'complete','texts':1,'gaps':0,
              'analysis':{'interesting':True,'summary':'=WEBSERVICE("https://evil.example/")','evidence':[], 'questions':[]},
              'opportunity':{'status':'proposal','acceptance_end_date':'2026-10-01T00:00:00Z','customer_intelligence':{'contacts':[{'email':'buy@example.org','source_kind':'procedure','source_url':'https://example.org/procurement/'}]}}}
        for cards in ([],[item]):
            with self.subTest(cards=len(cards)):
                caption,payload,ids=build_excel_report({'collected_at':now.isoformat(),'cards':cards},now=now)
                validate_excel_report(caption,payload,ids)
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertIn(b'_xlnm.Print_Area',archive.read('xl/workbook.xml'))

    async def test_uncertain_excel_is_not_retried_and_never_marks_seen(self):
        from agent_radar.digest_delivery import deliver_excel_report
        with TemporaryDirectory() as root:
            store=DigestStore(Path(root)/'digests.sqlite3');store.seed({101})
            class Bot:
                async def send_document(self,**kwargs):raise TimeoutError('Unknown remote outcome')
            async def sleep(_):pass
            render=lambda _:('caption',harmless_xlsx(),['not-seen'])
            result=await deliver_excel_report(store,'uncertain',[101],render,Bot(),sleep=sleep)
            self.assertEqual(result,{'sent':0,'skipped':0,'uncertain':1,'blocked':0})
            self.assertEqual(store.unseen(101,['not-seen']),{'not-seen'})
            self.assertEqual(store.delivery_state('uncertain',101),'uncertain')
            replay=await deliver_excel_report(store,'uncertain',[101],lambda _:self.fail('Uncertain workbook rerendered'),Bot(),sleep=sleep)
            self.assertEqual(replay['skipped'],1)


if __name__=='__main__':unittest.main()

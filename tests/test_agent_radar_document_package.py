"""Public offline document-package boundary: evidence and explicit gaps."""
from __future__ import annotations
import unittest
import io
import zipfile
import stat
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch


def zip_bytes(items):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in items:
            archive.writestr(name, data)
    return out.getvalue()

from agent_radar.document_package import extract_package


class DocumentPackageTest(unittest.TestCase):
    def test_text_document_is_available(self):
        self.assertEqual(extract_package('Поставка серверов\n'.encode(), 'spec.txt'), {
            'documents': [{'title': 'spec.txt', 'text': 'Поставка серверов'}],
            'gaps': [], 'status': 'available'})

    def test_zip_extracts_document_and_reports_nested_and_unsupported_members(self):
        result = extract_package(zip_bytes([
            ('docs/spec.txt', 'Storage 24 TB'.encode()),
            ('nested.zip', zip_bytes([('inner.txt', b'not read')])),
            ('macro.exe', b'not executed')]), 'rfp.zip')
        self.assertEqual(result['status'], 'retrieved_partial')
        self.assertEqual(result['documents'], [{'title': 'rfp.zip / docs/spec.txt', 'text': 'Storage 24 TB'}])
        self.assertEqual({g['reason'] for g in result['gaps']}, {'nested_archive_unsupported', 'unsupported_extension'})

    def test_zip_rejects_unsafe_metadata_before_any_evidence(self):
        for items in ([('../escape.txt', b'bad'), ('spec.txt', b'good')],
                      [('SPEC.txt', b'one'), ('spec.txt', b'two')],
                      [('bomb.txt', b'x' * 100000)]):
            with self.subTest(items=[i[0] for i in items]):
                result = extract_package(zip_bytes(items), 'bad.zip')
                self.assertEqual(result['status'], 'extraction_failed')
                self.assertEqual(result['documents'], [])
                self.assertTrue(result['gaps'])

    def test_xlsx_reads_shared_inline_numeric_cells_without_evaluating_formula(self):
        data = zip_bytes([
            ('[Content_Types].xml', b'<Types/>'),
            ('xl/workbook.xml', b'<workbook/>'),
            ('xl/sharedStrings.xml', '<sst><si><t>Сервер</t></si></sst>'.encode()),
            ('xl/worksheets/sheet1.xml', b'<worksheet><sheetData><row><c r="A1" t="s"><v>0</v></c><c r="B1"><v>24</v></c><c r="C1"><f>WEBSERVICE("https://private.invalid")</f></c></row></sheetData></worksheet>')])
        result = extract_package(data, 'spec.xlsx')
        self.assertEqual(result['status'], 'available')
        self.assertIn('Сервер', result['documents'][0]['text'])
        self.assertIn('B1: 24', result['documents'][0]['text'])
        self.assertIn('formula (not evaluated)', result['documents'][0]['text'])

    def test_xlsx_rejects_dtd_entities(self):
        data = zip_bytes([('[Content_Types].xml', b'<Types/>'), ('xl/workbook.xml', b'<workbook/>'),
            ('xl/worksheets/sheet1.xml', b'<!DOCTYPE foo [<!ENTITY x "secret">]><worksheet>&x;</worksheet>')])
        result = extract_package(data, 'bad.xlsx')
        self.assertEqual(result['status'], 'extraction_failed')
        self.assertIn('xml_dtd_not_allowed', str(result['gaps']))

    def test_7z_missing_backend_is_explicit_gap(self):
        with patch('shutil.which', return_value=None):
            result = extract_package(b'7z\xbc\xaf\x27\x1cgarbage', 'package.7z')
        self.assertEqual(result['status'], 'extraction_failed')
        self.assertIn('7zip_not_installed', str(result['gaps']))

    @unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), '7zip backend absent')
    def test_real_7z_package_extracts_without_archive_filesystem_writes(self):
        tool = shutil.which('7zz') or shutil.which('7z')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'spec.txt').write_text('Storage 24 TB', encoding='utf-8')
            subprocess.run([tool, 'a', '-t7z', '-bd', '-y', str(root / 'rfp.7z'), 'spec.txt'],
                           cwd=root, check=True, stdout=subprocess.DEVNULL, timeout=15)
            result = extract_package((root / 'rfp.7z').read_bytes(), 'rfp.7z')
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(result['documents'][0]['text'], 'Storage 24 TB')

    def test_legacy_doc_missing_backend_is_explicit(self):
        with patch('shutil.which', return_value=None):
            result = extract_package(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1data', 'old.doc')
        self.assertIn('antiword_not_installed', str(result['gaps']))

    def test_zip_links_and_exact_duplicates_are_rejected(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as archive:
            link = zipfile.ZipInfo('linked.txt')
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, b'/private/file')
            archive.writestr('spec.txt', b'good')
        result = extract_package(out.getvalue(), 'links.zip')
        self.assertEqual(result['documents'], [])
        self.assertIn('archive_link_or_special_member', str(result['gaps']))

    def test_document_count_cap_and_unsupported_entries_remain_explicit(self):
        result = extract_package(zip_bytes([(f'{i}.txt', b'Storage') for i in range(21)]), 'many.zip')
        self.assertEqual(len(result['documents']), 20)
        self.assertEqual(result['status'], 'retrieved_partial')
        self.assertEqual(result['gaps'][-1]['reason'], 'document_count_limit')

    def test_archive_inventory_limit_and_oversized_input_fail_closed(self):
        for data, reason in ((zip_bytes([(f'{i}.exe', b'x') for i in range(81)]), 'archive_member_limit'),
                             (b'x' * 12000001, 'document_too_large')):
            result = extract_package(data, 'many.zip')
            self.assertEqual(result['documents'], [])
            self.assertIn(reason, str(result['gaps']))

    def test_empty_archive_and_unsafe_input_filename_are_gaps(self):
        for data, name in ((zip_bytes([]), 'empty.zip'), (b'evidence', '../unsafe.txt')):
            result = extract_package(data, name)
            self.assertEqual(result['status'], 'extraction_failed')
            self.assertTrue(result['gaps'])

    def test_blank_xlsx_cells_are_not_extracted_evidence(self):
        data = zip_bytes([('[Content_Types].xml', b'<Types/>'), ('xl/workbook.xml', b'<workbook/>'),
            ('xl/worksheets/sheet1.xml', b'<worksheet><sheetData><row><c r="A1"/></row></sheetData></worksheet>')])
        result = extract_package(data, 'blank.xlsx')
        self.assertEqual(result['status'], 'extraction_failed')
        self.assertIn('no_extractable_text', str(result['gaps']))

    def test_aggregate_text_budget_never_truncates_evidence(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_STORED) as archive:
            for i in range(4):
                archive.writestr(f'{i}.txt', b'x' * 160000)
        result = extract_package(out.getvalue(), 'large.zip')
        self.assertEqual(len(result['documents']), 3)
        self.assertEqual(result['gaps'][-1]['reason'], 'package_text_limit')
        self.assertEqual(result['status'], 'retrieved_partial')
        self.assertEqual(len(result['documents'][0]['text']), 160000)

    def test_empty_directory_only_archive_records_gap(self):
        result = extract_package(zip_bytes([('docs/', b'')]), 'directories.zip')
        self.assertEqual(result['status'], 'extraction_failed')
        self.assertTrue(result['gaps'])

    @unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), '7zip backend absent')
    def test_real_7z_selector_names_reject_entire_inventory(self):
        tool = shutil.which('7zz') or shutil.which('7z')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'safe.txt').write_text('Storage', encoding='utf-8')
            (root / 'unsafe[1].txt').write_text('Never selected', encoding='utf-8')
            subprocess.run([tool, 'a', '-t7z', '-bd', '-y', '-spd', str(root / 'rfp.7z'), 'safe.txt', 'unsafe[1].txt'],
                           cwd=root, stdout=subprocess.DEVNULL, check=True, timeout=15)
            result = extract_package((root / 'rfp.7z').read_bytes(), 'rfp.7z')
        self.assertEqual(result['documents'], [], result)
        self.assertIn('unsafe_member_selector', str(result['gaps']))

    @unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), '7zip backend absent')
    def test_real_7z_encryption_and_nested_archive_gaps(self):
        tool = shutil.which('7zz') or shutil.which('7z')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'spec.txt').write_text('Storage', encoding='utf-8')
            (root / 'nested.zip').write_bytes(zip_bytes([('inner.txt', b'unread')]))
            subprocess.run([tool, 'a', '-t7z', '-bd', '-y', str(root / 'plain.7z'), 'spec.txt', 'nested.zip'],
                           cwd=root, stdout=subprocess.DEVNULL, check=True, timeout=15)
            partial = extract_package((root / 'plain.7z').read_bytes(), 'plain.7z')
            subprocess.run([tool, 'a', '-t7z', '-bd', '-y', '-psynthetic-test-password', str(root / 'locked.7z'), 'spec.txt'],
                           cwd=root, stdout=subprocess.DEVNULL, check=True, timeout=15)
            encrypted = extract_package((root / 'locked.7z').read_bytes(), 'locked.7z')
        self.assertEqual(partial['status'], 'retrieved_partial', partial)
        self.assertIn('nested_archive_unsupported', str(partial['gaps']))
        self.assertEqual(encrypted['documents'], [], encrypted)
        self.assertIn('encrypted_archive', str(encrypted['gaps']))

    @unittest.skipUnless(shutil.which('7zz') or shutil.which('7z'), '7zip backend absent')
    def test_real_7z_links_are_rejected(self):
        import os
        if os.name == 'nt':
            self.skipTest('symlink fixture requires Linux')
        tool = shutil.which('7zz') or shutil.which('7z')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'spec.txt').write_text('Storage', encoding='utf-8')
            (root / 'link.txt').symlink_to('spec.txt')
            subprocess.run([tool, 'a', '-t7z', '-snl', '-bd', '-y', str(root / 'links.7z'), 'link.txt', 'spec.txt'],
                           cwd=root, stdout=subprocess.DEVNULL, check=True, timeout=15)
            result = extract_package((root / 'links.7z').read_bytes(), 'links.7z')
        self.assertEqual(result['documents'], [], result)
        self.assertIn('archive_link_or_special_member', str(result['gaps']))

    def test_docx_inside_zip_is_a_document_not_nested_archive(self):
        xml = b'<document><body><p><t>Storage 24 TB</t></p></body></document>'
        docx = zip_bytes([('[Content_Types].xml', b'<Types/>'), ('word/document.xml', xml)])
        result = extract_package(zip_bytes([('technical.docx', docx)]), 'rfp.zip')
        self.assertEqual(result['status'], 'available', result)
        self.assertEqual(result['documents'][0]['text'], 'Storage 24 TB')

    def test_docx_embedded_objects_are_unread_gaps_not_lost_main_text(self):
        data = zip_bytes([('[Content_Types].xml', b'<Types/>'),
            ('word/document.xml', b'<document><body><p><t>ERP integration requirements</t></p></body></document>'),
            ('word/embeddings/oleObject1.bin', b'not executed'),
            ('word/attachments/package.zip', zip_bytes([('secret.txt', b'not read')]))])
        result = extract_package(data, 'technical.docx')
        self.assertEqual(result['status'], 'retrieved_partial', result)
        self.assertEqual(result['documents'], [{'title': 'technical.docx', 'text': 'ERP integration requirements'}])
        self.assertEqual(len(result['gaps']), 2)
        self.assertEqual({g['reason'] for g in result['gaps']}, {'embedded_object_not_read'})
        self.assertNotIn('not executed', str(result['documents']))

    def test_docx_embedding_filter_never_bypasses_unsafe_inventory_or_xml_guard(self):
        base = [('[Content_Types].xml', b'<Types/>'), ('word/embeddings/oleObject1.bin', b'ignored')]
        for extra in ([('word/document.xml', b'<!DOCTYPE x [<!ENTITY secret "bad">]><document/>')],
                      [('word/document.xml', b'<document><p><t>good</t></p></document>'), ('../escape.txt', b'bad')]):
            result = extract_package(zip_bytes(base + extra), 'bad.docx')
            self.assertEqual(result['documents'], [], result)
            self.assertEqual(result['status'], 'extraction_failed')
            self.assertTrue(any(g['reason'] != 'embedded_object_not_read' for g in result['gaps']))


if __name__ == '__main__':
    unittest.main()

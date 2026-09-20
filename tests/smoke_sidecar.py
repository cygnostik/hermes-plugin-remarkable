"""Opt-in live acceptance. Inert on import; default is read-only.

Writes are confined to a folder named 'Hermes Plugin QA ...'. No personal entry
is modified and nothing is permanently deleted. All created IDs remain in the
receipt; inspect the receipt before rerunning after an ambiguous network failure.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token-file', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--allow-writes', action='store_true')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--scratch-folder')
    group.add_argument('--create-scratch', action='store_true')
    return parser


def make_fixtures(directory):
    """Generate our own valid tiny PDF and EPUB; no private content is uploaded."""
    pdf = directory / 'fixture.pdf'
    stream = b'BT /F1 18 Tf 50 100 Td (Hermes reMarkable integration test) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
    data = bytearray(b'%PDF-1.4\n'); offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data)); data.extend(f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode())
    for offset in offsets[1:]: data.extend(f'{offset:010} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    pdf.write_bytes(data)
    epub = directory / 'fixture.epub'
    with zipfile.ZipFile(epub, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr('content.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">hermes-qa</dc:identifier><dc:title>Hermes QA</dc:title><dc:language>en</dc:language></metadata><manifest><item id="text" href="text.xhtml" media-type="application/xhtml+xml"/><item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest><spine toc="ncx"><itemref idref="text"/></spine></package>')
        archive.writestr('text.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Hermes QA</title></head><body><p>Integration fixture. No personal content.</p></body></html>')
        archive.writestr('toc.ncx', '<?xml version="1.0"?><ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head><meta name="dtb:uid" content="hermes-qa"/></head><docTitle><text>Hermes QA</text></docTitle><navMap><navPoint id="p1" playOrder="1"><navLabel><text>Test</text></navLabel><content src="text.xhtml"/></navPoint></navMap></ncx>')
    return {'pdf': pdf, 'epub': epub}


def run(args):
    spec = importlib.util.spec_from_file_location('remarkable_live_tools', ROOT / 'tools.py')
    tools = importlib.util.module_from_spec(spec); spec.loader.exec_module(tools)
    output = Path(args.output_dir).resolve(); output.mkdir(parents=True, exist_ok=True)
    receipt_path = output / 'live-receipt.json'
    if receipt_path.exists():
        raise ValueError('Receipt already exists. Inspect it and use a fresh output directory; never blindly repeat writes.')
    receipt = {'complete': False, 'writes': [], 'checks': []}
    cfg = {'token_file': args.token_file, 'state_dir': str(output / 'state')}
    def save(): tools._module('library').atomic_json(receipt_path, receipt)
    def call(name, payload, write=False):
        if write:
            receipt['writes'].append({'tool': name, 'request': payload, 'state': 'attempting'})
            save()
        result = json.loads(getattr(tools, 'remarkable_' + name)(payload, config=cfg))
        if write:
            receipt['writes'][-1].update(state='returned', result=result); save()
        if result.get('error') or result.get('complete') is False:
            receipt['failed_operation'] = {'tool': name, 'request': payload, 'result': result}
            save()
            raise RuntimeError(f'{name}: {result.get("code", "PARTIAL")} {result.get("error", "incomplete result")}')
        return result
    def check(name, condition):
        receipt['checks'].append({'check':name,'passed':bool(condition)}); save()
        if not condition: raise AssertionError(name)
        print('PASS ' + name)
    try:
        status = call('status', {})
        check('cloud-status', status.get('cloud') == 'ok')
        page = call('list', {'limit':2, 'offset':0, 'refresh':True})
        check('paginated-list', isinstance(page.get('entries'), list) and not page.get('walkErrors'))
        if not args.allow_writes:
            receipt['complete'] = True; receipt['mode'] = 'read-only'; save(); return receipt
        if not (args.scratch_folder or args.create_scratch):
            raise ValueError('--allow-writes also requires --scratch-folder or --create-scratch')
        if args.create_scratch:
            folder = call('mkdir', {'name':'Hermes Plugin QA ' + uuid.uuid4().hex[:10], 'parent':''}, write=True)
            folder_id = folder['id']
        else:
            folder_id = args.scratch_folder
        receipt['scratch_folder'] = folder_id; save()
        folders = call('list', {'parent':'', 'limit':200})
        matches = [e for e in folders['entries'] if e['id'] == folder_id and e.get('type') == 'folder' and e.get('name','').startswith('Hermes Plugin QA')]
        check('scratch-folder-exact-readback', len(matches) == 1)
        fixtures = make_fixtures(output)
        created = {}
        for kind, path in fixtures.items():
            document = call('upload', {'path':str(path),'name':'Hermes QA '+kind, 'kind':kind,'parent':folder_id}, write=True)
            created[kind] = document['id']
            entries = call('list', {'parent':folder_id,'limit':200})['entries']
            check(kind+'-folder-readback', any(e['id']==document['id'] and e['parent']==folder_id for e in entries))
            downloaded = output / ('roundtrip.'+kind)
            call('download', {'id':document['id'],'out_path':str(downloaded),'format':'original'})
            check(kind+'-exact-byte-roundtrip', downloaded.read_bytes() == path.read_bytes())
        nested = call('mkdir', {'name':'Nested QA','parent':folder_id}, write=True)['id']
        call('manage', {'id':created['pdf'],'action':'rename','name':'Renamed QA','confirm':True}, write=True)
        call('manage', {'id':created['pdf'],'action':'move','parent':nested,'confirm':True}, write=True)
        entries = call('list', {'parent':nested})['entries']
        check('rename-move-readback', any(e['id']==created['pdf'] and e['name']=='Renamed QA' for e in entries))
        backup = call('backup', {'destination':str(output/'backup'),'parent':folder_id})
        check('recursive-backup', backup['downloaded'] == len(created))
        repeated = call('backup', {'destination':str(output/'backup'),'parent':folder_id})
        check('incremental-backup', repeated['skipped'] == len(created))
        receipt['complete'] = True; save(); return receipt
    except Exception as exc:
        receipt['failure'] = str(exc); save(); raise


if __name__ == '__main__':
    import sys
    try:
        result = run(make_parser().parse_args())
        print(json.dumps({'complete':result['complete'],'checks':len(result['checks']),'scratch_folder':result.get('scratch_folder')}))
    except Exception as exc:
        print('FAIL ' + str(exc), file=sys.stderr)
        raise SystemExit(1)

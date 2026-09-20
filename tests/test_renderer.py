"""Offline renderer tests; all scenes are generated, never user documents."""
import importlib.util
import io
import json
from pathlib import Path
import zipfile

import pytest

rmscene = pytest.importorskip('rmscene', reason='Run renderer tests using the external renderer venv')

ROOT = Path(__file__).resolve().parents[1]


def renderer():
    spec = importlib.util.spec_from_file_location("remarkable_renderer", ROOT / "renderer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scene(text):
    buffer = io.BytesIO()
    rmscene.write_blocks(buffer, rmscene.simple_text_document(text))
    return buffer.getvalue()


def archive(tmp_path, content=None, members=None):
    path = tmp_path / "sample.rmdoc"
    if content is None:
        content = {"fileType": "notebook", "cPages": {"pages": [
            {"id": "second", "idx": {"value": "bb"}},
            {"id": "first", "idx": {"value": "ba"}},
        ]}}
    if members is None:
        members = {"doc/first.rm": scene("First typed page\nNative text, no OCR."),
                   "doc/second.rm": scene("Second typed page")}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.content", json.dumps(content))
        for name, data in members.items():
            z.writestr(name, data)
    return path


def test_extract_real_v6_cpages_order(tmp_path):
    result = renderer().extract_archive_text(archive(tmp_path))
    assert result["ok"] is True, result
    assert result['complete'] is True
    assert result["ocr"] is False
    assert [p["id"] for p in result["pages"]] == ["first", "second"]
    assert result["pages"][0]["text"] == "First typed page\nNative text, no OCR."
    assert result["page_count"] == 2
    assert result["text"].endswith("Second typed page")


def test_render_real_v6_pdf_and_png(tmp_path):
    import pymupdf
    from PIL import Image
    source = archive(tmp_path)
    mod = renderer()
    pdf = tmp_path / "out.pdf"
    result = mod.render_archive(source, pdf)
    assert result["ok"] is True, result
    assert result["renderer"] == "librm_lines"
    assert result["page_count"] == 2
    with pymupdf.open(pdf) as doc:
        assert len(doc) == 2
        pixels = doc[0].get_pixmap().samples
        assert min(pixels) < 100  # native text really rendered, not a blank PDF
        assert max(pixels) == 255
    png = tmp_path / "second.png"
    result = mod.render_archive(source, png, format="png", pages=[2])
    assert result["ok"] is True, result
    assert result["pages"] == [2]
    with Image.open(png) as image:
        assert image.size == (1404, 1872)
        assert image.convert("L").getextrema()[0] < 100


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', 'dir\\escape', 'doc/../escape'])
def test_reject_unsafe_zip_paths(tmp_path, name):
    source = archive(tmp_path, members={name: b'bad', 'doc/first.rm': scene('x'), 'doc/second.rm': scene('y')})
    # ZipInfo normalizes Windows separators when writing; exercise raw ZIP input.
    if '\\' in name:
        source.write_bytes(source.read_bytes().replace(name.replace('\\', '/').encode(), name.encode()))
    result = renderer().extract_archive_text(source)
    assert result['ok'] is False
    assert result['code'] == 'UNSAFE_ARCHIVE'


def test_zip_bomb_and_duplicate_names_rejected(tmp_path):
    mod = renderer()
    source = archive(tmp_path, members={'bomb': b'0' * 2_000_000})
    assert mod.extract_archive_text(source)['code'] == 'UNSAFE_ARCHIVE'
    source = archive(tmp_path)
    with zipfile.ZipFile(source, 'a') as z:
        with pytest.warns(UserWarning):
            z.writestr('doc.content', '{}')
    assert mod.extract_archive_text(source)['code'] == 'UNSAFE_ARCHIVE'


def test_limits_before_decompression(tmp_path):
    mod = renderer()
    source = archive(tmp_path)
    mod.MAX_MEMBER_BYTES = 10
    assert mod.extract_archive_text(source)['code'] == 'UNSAFE_ARCHIVE'


def test_missing_page_not_reported_as_success(tmp_path):
    result = renderer().extract_archive_text(archive(tmp_path, members={'doc/first.rm': scene('x')}))
    assert result['ok'] is False
    assert result['code'] == 'MISSING_PAGE'


def test_legacy_page_array_and_deleted_cpage(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['second', 'first']})
    result = renderer().extract_archive_text(source)
    assert [p['id'] for p in result['pages']] == ['second', 'first']
    source = archive(tmp_path, content={'fileType': 'notebook', 'cPages': {'pages': [
        {'id': 'first', 'idx': {'value': 'a'}},
        {'id': 'gone', 'idx': {'value': 'b'}, 'deleted': {'value': True}},
    ]}})
    result = renderer().extract_archive_text(source)
    assert result['ok'] is True, result
    assert result['page_count'] == 1


def test_unknown_v6_blocks_fail_closed(tmp_path):
    import struct
    data = scene('known') + struct.pack('<IBBBB', 0, 0, 1, 1, 250)
    result = renderer().extract_archive_text(archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': data}))
    assert result['ok'] is False
    assert result['code'] == 'UNSUPPORTED_SCENE'
    assert result['complete'] is False


@pytest.mark.parametrize('kwargs', [{'format': 'svg'}, {'format': 'png'}, {'pages': []}, {'pages': [0]}, {'pages': [3]}, {'pages': [True]}, {'pages': [1, 1]}, {'pages': '1'}])
def test_invalid_output_selection_never_writes(tmp_path, kwargs):
    target = tmp_path / 'out.pdf'
    result = renderer().render_archive(archive(tmp_path), target, **kwargs)
    assert result['ok'] is False
    assert result['code'] == 'BAD_ARGS'
    assert not target.exists()


def test_overwrite_preserves_existing_and_source(tmp_path):
    mod = renderer()
    source = archive(tmp_path)
    target = tmp_path / 'out.pdf'
    target.write_bytes(b'existing')
    assert mod.render_archive(source, target)['code'] == 'OUTPUT_EXISTS'
    assert target.read_bytes() == b'existing'
    assert mod.render_archive(source, source, overwrite=True)['code'] == 'BAD_ARGS'
    assert mod.render_archive(source, target, overwrite=True)['ok'] is True


def test_unknown_render_scene_does_not_replace_destination(tmp_path):
    import struct
    data = scene('known') + struct.pack('<IBBBB', 0, 0, 1, 1, 250)
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': data})
    target = tmp_path / 'out.pdf'
    target.write_bytes(b'original')
    result = renderer().render_archive(source, target, overwrite=True)
    assert result['code'] == 'UNSUPPORTED_SCENE'
    assert target.read_bytes() == b'original'


def stroke_scene(y=900, paper_size=None):
    si = rmscene.scene_items
    blocks = list(rmscene.simple_text_document(''))
    line = si.Line(si.PenColor.BLACK, si.Pen.FINELINER_2,
                   [si.Point(-300, y, 0, 0, 40, 255), si.Point(300, y, 0, 0, 40, 255)], 1.0, 0.0)
    blocks.append(rmscene.SceneLineItemBlock(parent_id=rmscene.CrdtId(0, 11), item=rmscene.CrdtSequenceItem(
        item_id=rmscene.CrdtId(1, 100), left_id=rmscene.CrdtId(0, 0), right_id=rmscene.CrdtId(0, 0), deleted_length=0, value=line)))
    if paper_size:
        blocks.append(rmscene.SceneInfo(rmscene.LwwValue(rmscene.CrdtId(1, 1), rmscene.CrdtId(0, 11)),
            rmscene.LwwValue(rmscene.CrdtId(1, 1), True), rmscene.LwwValue(rmscene.CrdtId(1, 1), True), paper_size))
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    return buf.getvalue()


def pdf_background():
    import pymupdf
    with pymupdf.open() as pdf:
        for label in ['BASE FIRST', 'BASE SECOND']:
            page = pdf.new_page(width=1404 * 72 / 226, height=1872 * 72 / 226)
            page.insert_text((80, 80), label, fontsize=24)
        return pdf.tobytes()


def test_pdf_background_redirects_and_real_ink(tmp_path):
    import pymupdf
    from PIL import Image
    content = {'fileType': 'pdf', 'cPages': {'pages': [
        {'id': 'first', 'idx': {'value': 'a'}, 'redir': {'value': 1}},
        {'id': 'second', 'idx': {'value': 'b'}, 'redir': {'value': 0}},
    ]}}
    source = archive(tmp_path, content=content, members={'doc.pdf': pdf_background(), 'doc/first.rm': stroke_scene()})
    mod = renderer()
    target = tmp_path / 'annotated.pdf'
    result = mod.render_archive(source, target)
    assert result['ok'] is True, result
    with pymupdf.open(target) as pdf:
        assert len(pdf) == 2
        assert 'BASE SECOND' in pdf[0].get_text()
        assert 'BASE FIRST' in pdf[1].get_text()
        pix = pdf[0].get_pixmap(dpi=226)
        image = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        assert image.crop((400, 895, 1000, 905)).convert('L').getextrema()[0] < 50
    text = mod.extract_archive_text(source)
    assert text['ok'] is True, text
    assert text['text'].strip() == ''  # excludes source PDF text, never OCR
    assert text['page_count'] == 2


def test_plain_pdf_without_cpages(tmp_path):
    source = archive(tmp_path, content={'fileType': 'pdf'}, members={'doc.pdf': pdf_background()})
    result = renderer().render_archive(source, tmp_path / 'base.pdf')
    assert result['ok'] is True, result
    assert result['page_count'] == 2


@pytest.mark.parametrize('content,code', [
    ({'fileType': 'notebook', 'pages': ['first'], 'transform': {'m11': 2}}, 'UNSUPPORTED_LAYOUT'),
    ({'fileType': 'notebook', 'pages': ['first'], 'orientation': 'sideways'}, 'UNSUPPORTED_LAYOUT'),
    ({'fileType': 'notebook', 'cPages': {'pages': [{'id': 'first', 'idx': {'value': 'a'}, 'template': {'value': 'P Lined'}}]}}, 'UNSUPPORTED_TEMPLATE'),
])
def test_unsupported_layout_fails_closed(tmp_path, content, code):
    source = archive(tmp_path, content=content, members={'doc/first.rm': stroke_scene()})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == code, result


def test_pagedata_template_never_silently_dropped(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': stroke_scene(), 'doc.pagedata': b'P Lined\n'})
    assert renderer().render_archive(source, tmp_path / 'out.pdf').get('code') == 'UNSUPPORTED_TEMPLATE'


def test_native_paper_size_and_ink_are_preserved(tmp_path):
    from PIL import Image
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': stroke_scene(paper_size=(1620, 2160))})
    target = tmp_path / 'large.png'
    result = renderer().render_archive(source, target, format='png')
    assert result['ok'] is True, result
    with Image.open(target) as image:
        assert image.size == (1620, 2160)
        assert image.crop((510, 895, 1110, 905)).convert('L').getextrema()[0] < 50


def test_infinite_page_is_not_silently_cropped(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': stroke_scene(y=2500)})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == 'UNSUPPORTED_LAYOUT', result


def test_oversized_paper_rejected_before_native_allocation(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': stroke_scene(paper_size=(50000, 50000))})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == 'RESOURCE_LIMIT', result


def test_cli_real_render_and_text(tmp_path):
    import subprocess
    import sys
    source = archive(tmp_path)
    for request in [
        {'op': 'render', 'source': str(source), 'out_path': str(tmp_path / 'cli.pdf'), 'format': 'pdf'},
        {'op': 'extract_text', 'source': str(source)},
    ]:
        proc = subprocess.run([sys.executable, str(ROOT / 'renderer.py')], input=json.dumps(request),
                              text=True, encoding='utf-8', capture_output=True, timeout=60)
        result = json.loads(proc.stdout)
        assert proc.returncode == 0, (result, proc.stderr)
        assert result['ok'] is True
        assert result['page_count'] == 2


def test_missing_native_dependency_has_install_hint(tmp_path, monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, 'rm_lines_sys', None)
    result = renderer().render_archive(archive(tmp_path), tmp_path / 'out.pdf')
    assert result.get('code') == 'DEPENDENCY_MISSING', result
    assert 'requirements-render.txt' in result['error']


def test_invalid_native_abi_has_actionable_error(tmp_path, monkeypatch):
    import sys
    import types
    monkeypatch.setitem(sys.modules, 'rm_lines_sys', types.SimpleNamespace(lib=None))
    result = renderer().render_archive(archive(tmp_path), tmp_path / 'out.pdf')
    assert result.get('code') == 'DEPENDENCY_PLATFORM', result
    assert 'x64' in result['error']


def test_long_text_does_not_succeed_if_cropped(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': scene('one two three four five six seven eight nine ten ' * 100)})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == 'UNSUPPORTED_LAYOUT', result


def test_hidden_layer_not_rendered_as_visible(tmp_path):
    data = stroke_scene()
    blocks = list(rmscene.read_blocks(io.BytesIO(data)))
    for block in blocks:
        if isinstance(block, rmscene.TreeNodeBlock) and block.group.node_id == rmscene.CrdtId(0, 11):
            block.group.visible = rmscene.LwwValue(rmscene.CrdtId(1, 1000), False)
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': buf.getvalue()})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == 'UNSUPPORTED_LAYOUT', result


def test_nonfinite_stroke_rejected(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': stroke_scene(y=float('nan'))})
    result = renderer().extract_archive_text(source)
    assert result.get('code') == 'UNSUPPORTED_SCENE', result


@pytest.mark.parametrize('data', [b'not a scene', scene('cut off')[:-1]])
def test_invalid_or_truncated_scene(tmp_path, data):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']}, members={'doc/first.rm': data})
    assert renderer().extract_archive_text(source)['code'] == 'UNSUPPORTED_SCENE'


@pytest.mark.parametrize('template', ['P Grid small', 'P Grid medium', 'P Grid large', 'P Grid margin med', 'P Grid margin large'])
def test_native_grid_template_is_present(tmp_path, template):
    from PIL import Image
    source = archive(tmp_path, content={'fileType': 'notebook', 'cPages': {'pages': [
        {'id': 'first', 'idx': {'value': 'a'}, 'template': {'value': template}}]}},
        members={'doc/first.rm': stroke_scene()})
    target = tmp_path / 'grid.png'
    result = renderer().render_archive(source, target, format='png')
    assert result['ok'] is True, result
    with Image.open(target) as image:
        assert image.crop((300, 300, 700, 700)).convert('L').getextrema()[0] < 250


def test_huge_pdf_png_checked_before_rasterization(tmp_path, monkeypatch):
    import pymupdf
    with pymupdf.open() as pdf:
        pdf.new_page(width=10000, height=10000)
        background = pdf.tobytes()
    source = archive(tmp_path, content={'fileType': 'pdf'}, members={'doc.pdf': background})
    monkeypatch.setattr(pymupdf.Page, 'get_pixmap', lambda *a, **kw: pytest.fail('Unbounded raster allocation'))
    result = renderer().render_archive(source, tmp_path / 'out.png', format='png')
    assert result.get('code') == 'RESOURCE_LIMIT', result


def legacy_scene(version=5, pen=17, points=None):
    """Synthetic legacy .lines bytes; no private notebook content."""
    import struct
    if points is None:
        points = [(402.0, 900.0, 10.0, 0.5, 10.0, 0.75),
                  (1002.0, 900.0, 10.0, 0.5, 10.0, 0.75)]
    header = f'reMarkable .lines file, version={version}'.encode().ljust(43, b' ')
    stroke = struct.pack('<IIIf', pen, 0, 0, 2.0)
    if version == 5:
        stroke += struct.pack('<I', 0)
    return (header + struct.pack('<II', 1, 1) + stroke + struct.pack('<I', len(points))
            + b''.join(struct.pack('<ffffff', *p) for p in points))


@pytest.mark.parametrize('version', [3, 5])
def test_legacy_native_typed_text_is_explicitly_empty(tmp_path, version):
    # Unknown pen still cannot contain typed text: extraction is not rendering.
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first', 'second']},
                     members={'doc/first.rm': legacy_scene(version, pen=999),
                              'doc/second.rm': scene('Native v6 text')})
    result = renderer().extract_archive_text(source)
    assert result['ok'] is True, result
    assert result['complete'] is True
    assert result['ocr'] is False
    assert result['pages'][0]['text'] == ''
    assert result['pages'][1]['text'] == 'Native v6 text'
    assert any('v3/v5' in warning and 'render' in warning for warning in result['warnings'])


@pytest.mark.parametrize('version,pen', [(3, 4), (5, 17)])
def test_legacy_fineliner_converts_to_native_ink_without_shift(tmp_path, version, pen):
    from PIL import Image
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': legacy_scene(version, pen)})
    target = tmp_path / 'legacy.png'
    result = renderer().render_archive(source, target, format='png')
    assert result['ok'] is True, result
    assert result['renderer'] == 'librm_lines'
    assert any('legacy' in w.lower() and 'v6' in w for w in result['warnings'])
    with Image.open(target) as image:
        gray = image.convert('L')
        assert image.size == (1404, 1872)
        assert gray.getpixel((500, 900)) < 50
        assert gray.getpixel((500, 880)) == 255
        assert gray.getpixel((200, 900)) == 255
        assert gray.getpixel((1200, 900)) == 255
        dark = gray.point(lambda p: 255 if p < 50 else 0)
        left, top, right, bottom = dark.getbbox()
        assert 395 <= left <= 402 and 1002 <= right <= 1010
        assert 893 <= top <= 899 and 901 <= bottom <= 907


def test_unselected_unsupported_template_does_not_block_selected_page(tmp_path):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first', 'second']},
                     members={'doc/first.rm': stroke_scene(), 'doc/second.rm': stroke_scene(),
                              'doc.pagedata': b'CUSTOM UNKNOWN\nP Grid small\n'})
    mod = renderer()
    result = mod.render_archive(source, tmp_path / 'selected.png', format='png', pages=[2])
    assert result['ok'] is True, result
    assert result['pages'] == [2]
    assert mod.render_archive(source, tmp_path / 'all.pdf')['code'] == 'UNSUPPORTED_TEMPLATE'


@pytest.mark.parametrize('metadata_name', ['doc/first-metadata.json', 'doc/first.metadata'])
def test_legacy_external_layer_metadata_is_not_silently_ignored(tmp_path, metadata_name):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': legacy_scene(),
                              metadata_name: json.dumps({'layers': [{'visible': False}]})})
    target = tmp_path / 'hidden.pdf'
    result = renderer().render_archive(source, target)
    assert result.get('code') == 'UNSUPPORTED_LAYOUT', result
    assert not target.exists()


@pytest.mark.parametrize('mutation,code', [
    ('truncated', 'UNSUPPORTED_SCENE'), ('trailing', 'UNSUPPORTED_SCENE'),
    ('layers', 'RESOURCE_LIMIT'), ('strokes', 'RESOURCE_LIMIT'), ('points', 'RESOURCE_LIMIT'),
    ('nan', 'UNSUPPORTED_SCENE'), ('invalid-header', 'UNSUPPORTED_SCENE'),
])
def test_legacy_decoder_rejects_malformed_before_native(tmp_path, mutation, code):
    import struct
    data = bytearray(legacy_scene())
    if mutation == 'truncated':
        data = data[:-1]
    elif mutation == 'trailing':
        data += b'unknown'
    elif mutation == 'layers':
        struct.pack_into('<I', data, 43, 4097)
    elif mutation == 'strokes':
        struct.pack_into('<I', data, 47, 100001)
    elif mutation == 'points':
        struct.pack_into('<I', data, 71, 500001)
    elif mutation == 'nan':
        struct.pack_into('<f', data, 75, float('nan'))
    else:
        data[31] = ord('4')
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': data})
    mod = renderer()
    assert mod.extract_archive_text(source).get('code') == code
    target = tmp_path / 'invalid.pdf'
    assert mod.render_archive(source, target).get('code') == code
    assert not target.exists()


@pytest.mark.parametrize('pen', [0, 1, 2, 3, 5, 6, 7, 8, 12, 13, 14, 15, 16, 18, 999])
def test_legacy_unqualified_pen_never_becomes_rendered_success(tmp_path, pen):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': legacy_scene(pen=pen)})
    target = tmp_path / 'out.pdf'
    target.write_bytes(b'keep original')
    mod = renderer()
    result = mod.render_archive(source, target, overwrite=True)
    assert result.get('code') == 'UNSUPPORTED_SCENE', result
    assert target.read_bytes() == b'keep original'
    text = mod.extract_archive_text(source)
    assert text['ok'] and text['text'] == ''


def test_legacy_float_point_conversion_preserves_native_fields():
    import struct
    point = (123.125, 345.25, 12.375, 0.75, 4.125, 0.625)
    data = renderer()._legacy_to_v6(legacy_scene(points=[point, point]))
    # v6 block version 1 keeps original float payloads, rather than rounding
    # to the newer uint16/uint8 point layout. Only the x origin changes.
    assert struct.pack('<ffffff', point[0] - 702, *point[1:]) in data
    lines = [b for b in rmscene.read_blocks(io.BytesIO(data)) if isinstance(b, rmscene.SceneLineItemBlock)]
    assert len(lines) == 1 and len(lines[0].item.value.points) == 2


@pytest.mark.parametrize('template', ['P Lines small', 'P Lines medium', 'P Lines large'])
def test_ruled_template_reports_upstream_blocker_instead_of_blank(tmp_path, template):
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': stroke_scene(), 'doc.pagedata': template.encode() + b'\n'})
    target = tmp_path / 'ruled.pdf'
    result = renderer().render_archive(source, target)
    assert result.get('code') == 'UNSUPPORTED_TEMPLATE', result
    assert 'LIB rMLines 1.4.7' in result['error']
    assert 'ruled' in result['error']
    assert not target.exists()


@pytest.mark.parametrize('case,code', [
    ('multilayer', 'UNSUPPORTED_SCENE'), ('reserved', 'UNSUPPORTED_SCENE'),
    ('extra', 'UNSUPPORTED_SCENE'), ('short', 'UNSUPPORTED_SCENE'),
    ('pressure', 'UNSUPPORTED_SCENE'), ('landscape', 'UNSUPPORTED_LAYOUT'),
    ('off-page', 'UNSUPPORTED_LAYOUT'), ('pdf-overlay', 'UNSUPPORTED_LAYOUT'),
])
def test_legacy_unqualified_geometry_and_fields_fail_closed(tmp_path, case, code):
    import struct
    data = bytearray(legacy_scene())
    content = {'fileType': 'notebook', 'pages': ['first']}
    members = {}
    if case == 'multilayer':
        struct.pack_into('<I', data, 43, 2)
        data += struct.pack('<I', 0)
    elif case in ('reserved', 'extra'):
        struct.pack_into('<I', data, 59 if case == 'reserved' else 67, 1)
    elif case == 'short':
        data = legacy_scene(points=[(402, 900, 10, .5, 10, .75)])
    elif case == 'pressure':
        struct.pack_into('<f', data, 95, -1)
    elif case == 'landscape':
        content['orientation'] = 'landscape'
    elif case == 'off-page':
        data = legacy_scene(points=[(402, 2500, 10, .5, 10, .75), (1002, 2500, 10, .5, 10, .75)])
    else:
        content['fileType'] = 'pdf'
        members['doc.pdf'] = pdf_background()
    members['doc/first.rm'] = data
    source = archive(tmp_path, content=content, members=members)
    target = tmp_path / 'out.pdf'
    result = renderer().render_archive(source, target)
    assert result.get('code') == code, result
    assert not target.exists()


def scene_info_extension_scene(fields=6, rect=(0, 0, 0, 0), size=(1404, 1872), tail=b''):
    import struct
    from rmscene.tagged_block_writer import TaggedBlockWriter
    blocks = list(rmscene.read_blocks(io.BytesIO(stroke_scene(paper_size=(1404, 1872)))))
    buf = io.BytesIO()
    writer = TaggedBlockWriter(buf)
    with writer.write_subblock(6):
        writer.write_id(1, rmscene.CrdtId(1, 20))
        with writer.write_subblock(2):
            writer.data.write_bytes(struct.pack('<dddd', *rect))
    if fields >= 7:
        with writer.write_subblock(7):
            writer.data.write_bytes(struct.pack('<dd', *size))
    if fields >= 8:
        with writer.write_subblock(8):
            writer.write_id(1, rmscene.CrdtId(1, 21))
            with writer.write_subblock(2):
                writer.data.write_bytes(struct.pack('<dd', *size))
    for block in blocks:
        if isinstance(block, rmscene.SceneInfo):
            block.extra_data = buf.getvalue() + tail
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    return buf.getvalue()


@pytest.mark.parametrize('fields', [6, 7, 8])
def test_redundant_native_scene_geometry_is_preserved(tmp_path, fields):
    from PIL import Image
    mod = renderer()
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': scene_info_extension_scene(fields)})
    target = tmp_path / 'modern-scene.png'
    result = mod.render_archive(source, target, format='png')
    assert result['ok'] is True, result
    assert result['renderer'] == 'librm_lines'
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': stroke_scene(paper_size=(1404, 1872))})
    reference = tmp_path / 'reference.png'
    assert mod.render_archive(source, reference, format='png')['ok'] is True
    with Image.open(target) as actual, Image.open(reference) as expected:
        assert actual.tobytes() == expected.tobytes()
        assert actual.convert('L').getextrema()[0] < 50


@pytest.mark.parametrize('case', ['rect', 'rect-nan', 'size', 'size-nan', 'lww-size',
                                 'preferred-layout', 'unknown', 'truncated', 'inner-extra', 'outer-extra'])
def test_unqualified_scene_geometry_still_fails_closed(tmp_path, case):
    import struct
    kwargs = {'fields': 8}
    if case in ('rect', 'rect-nan'):
        kwargs['rect'] = (0, 0, 1 if case == 'rect' else float('nan'), 0)
    elif case in ('size', 'size-nan'):
        kwargs['size'] = (1404, 1873 if case == 'size' else float('nan'))
    elif case == 'preferred-layout':
        # Valid native field 9/LWW byte-sub, but its visual semantics are unqualified.
        kwargs['tail'] = bytes.fromhex('9c090000001f01142c0100000001')
    elif case == 'unknown':
        kwargs['tail'] = b'unknown'
    blocks = list(rmscene.read_blocks(io.BytesIO(scene_info_extension_scene(**kwargs))))
    for block in blocks:
        if not isinstance(block, rmscene.SceneInfo):
            continue
        data = bytearray(block.extra_data)
        if case == 'truncated':
            data = data[:-1]
        elif case == 'lww-size':
            struct.pack_into('<d', data, len(data) - 8, 1873)
        elif case in ('inner-extra', 'outer-extra'):
            # Grow only the first rectangle subblock, retaining later fields.
            end = 5 + struct.unpack_from('<I', data, 1)[0]
            data[end:end] = b'\x00'
            struct.pack_into('<I', data, 1, end - 4)
            if case == 'inner-extra':
                struct.pack_into('<I', data, 9, 33)
        block.extra_data = bytes(data)
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': buf.getvalue()})
    mod = renderer()
    target = tmp_path / 'out.pdf'
    target.write_bytes(b'original')
    for result in (mod.extract_archive_text(source), mod.render_archive(source, target, overwrite=True)):
        assert result['code'] == 'UNSUPPORTED_SCENE', result
        assert result['ok'] is False and result['complete'] is False
    assert target.read_bytes() == b'original'


def modern_width_scene(width=700.0, extra=b''):
    """Synthetic native 1.4.7 RootTextBlock field 5/1 LWW width."""
    from rmscene.tagged_block_writer import TaggedBlockWriter
    blocks = list(rmscene.simple_text_document('Native width wrapping example ' * 6))
    buf = io.BytesIO()
    writer = TaggedBlockWriter(buf)
    with writer.write_subblock(5):
        writer.write_lww_float(1, rmscene.LwwValue(rmscene.CrdtId(1, 20), width))
    for block in blocks:
        if isinstance(block, rmscene.RootTextBlock):
            block.extra_data = buf.getvalue() + extra
    output = io.BytesIO()
    rmscene.write_blocks(output, blocks)
    return output.getvalue()


def test_native_modern_text_width_is_preserved(tmp_path):
    from PIL import Image
    mod = renderer()
    data = modern_width_scene()
    tree = mod._parse_scene(data)
    assert tree.root_text.width == 700.0
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': data})
    target = tmp_path / 'modern.png'
    result = mod.render_archive(source, target, format='png')
    assert result['ok'] is True, result
    assert result['renderer'] == 'librm_lines'
    assert mod.extract_archive_text(source)['text'] == 'Native width wrapping example ' * 6
    # Same native engine, equivalent traditional width: identical rendered pixels.
    blocks = list(rmscene.read_blocks(io.BytesIO(data)))
    for block in blocks:
        if isinstance(block, rmscene.RootTextBlock):
            block.extra_data = b''
            block.value.width = 700.0
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': buf.getvalue()})
    reference = tmp_path / 'reference.png'
    assert mod.render_archive(source, reference, format='png')['ok'] is True
    with Image.open(target) as actual, Image.open(reference) as expected:
        assert actual.tobytes() == expected.tobytes()
        assert actual.convert('L').getextrema()[0] < 100


@pytest.mark.parametrize('mutation', ['tag', 'length', 'truncated', 'trailing', 'inner-trailing',
                                        'outer-trailing', 'nan', 'infinite', 'zero', 'negative', 'huge'])
def test_modern_width_malformed_fails_closed(tmp_path, mutation):
    import struct
    width = {'nan': float('nan'), 'infinite': float('inf'), 'zero': 0,
             'negative': -1, 'huge': 1_000_001}.get(mutation, 700)
    blocks = list(rmscene.read_blocks(io.BytesIO(modern_width_scene(width))))
    for block in blocks:
        if not isinstance(block, rmscene.RootTextBlock):
            continue
        payload = bytearray(block.extra_data)
        if mutation == 'tag':
            payload[0] = 0x6c
        elif mutation == 'length':
            struct.pack_into('<I', payload, 1, 1)
        elif mutation == 'truncated':
            payload = payload[:-1]
        elif mutation in ('trailing', 'inner-trailing', 'outer-trailing'):
            payload += b'\x00'
            if mutation != 'trailing':
                struct.pack_into('<I', payload, 1, struct.unpack_from('<I', payload, 1)[0] + 1)
            if mutation == 'inner-trailing':
                struct.pack_into('<I', payload, 6, struct.unpack_from('<I', payload, 6)[0] + 1)
        block.extra_data = bytes(payload)
    buf = io.BytesIO()
    rmscene.write_blocks(buf, blocks)
    source = archive(tmp_path, content={'fileType': 'notebook', 'pages': ['first']},
                     members={'doc/first.rm': buf.getvalue()})
    target = tmp_path / 'invalid.pdf'
    target.write_bytes(b'original')
    mod = renderer()
    for result in (mod.extract_archive_text(source), mod.render_archive(source, target, overwrite=True)):
        assert result['ok'] is False and result['complete'] is False
        assert result['code'] == 'UNSUPPORTED_SCENE', result
    assert target.read_bytes() == b'original'


def test_cropped_pdf_annotation_geometry_rejected(tmp_path):
    import pymupdf
    width, height = 1404 * 72 / 226, 1872 * 72 / 226
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=width * 2, height=height * 2)
        page.set_cropbox(pymupdf.Rect(10, 10, 10 + width, 10 + height))
        background = pdf.tobytes()
    source = archive(tmp_path, content={'fileType': 'pdf', 'pages': ['first']},
                     members={'doc.pdf': background, 'doc/first.rm': stroke_scene()})
    result = renderer().render_archive(source, tmp_path / 'out.pdf')
    assert result.get('code') == 'UNSUPPORTED_LAYOUT', result

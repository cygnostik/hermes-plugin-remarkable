"""Local-only reMarkable archive rendering. No cloud, credentials, or OCR.

Public functions return JSON-serializable dictionaries, including errors.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
import zipfile
import ctypes
import tempfile
import re
import stat
import struct
import os
import math

MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 4096
MAX_PAGES = 500
MAX_COMPRESSION_RATIO = 200


class ArchiveError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _error(exc):
    if isinstance(exc, ImportError):
        return {'ok': False, 'complete': False, 'code': 'DEPENDENCY_MISSING', 'error':
                'Renderer dependency unavailable. Create an external Python 3.11 renderer venv and run '
                'uv pip install --python <external-renderer-venv-python> -r requirements-render.txt. '
                'Do not install into Hermes or bundle the venv inside the plugin. ' + str(exc)}
    return {'ok': False, 'complete': False, 'code': getattr(exc, 'code', 'RENDER_ERROR'), 'error': str(exc)}


def _value(value):
    return value.get('value') if isinstance(value, dict) else value


def _native_image(data, folder, landscape=False, parsed=None, template='Blank', document=False):
    from importlib.metadata import version
    try:
        from rm_lines_sys import lib
    except OSError as exc:
        raise ArchiveError('DEPENDENCY_PLATFORM', 'Cannot load librm_lines DLL/shared library. Use a matching x64/ARM64 Python and official rm-lines-sys wheel (Linux requires glibc 2.39+, macOS 15+). ' + str(exc)) from exc
    if lib is None or version('rm-lines-sys') != '1.4.7':
        raise ArchiveError('DEPENDENCY_PLATFORM', 'Use the verified rm-lines-sys==1.4.7 official wheel matching Python x64/ARM64; reinstall requirements-render.txt in the isolated venv')
    from PIL import Image
    parsed = parsed or _parse_scene(data)
    width, height = (parsed.scene_info.paper_size if parsed.scene_info and parsed.scene_info.paper_size else (1404, 1872))
    if landscape:
        width, height = height, width
    _check_pixels(width, height)
    if any(not group.visible.value for group in _groups(parsed)):
        raise ArchiveError('UNSUPPORTED_LAYOUT', 'Hidden layers are not verified by this native renderer; export the intended visible view on tablet')
    if parsed.scene_info and any(value is not None and not value.value for value in
                                  (parsed.scene_info.background_visible, parsed.scene_info.root_document_visible)):
        raise ArchiveError('UNSUPPORTED_LAYOUT', 'Hidden scene background/root view is not verified; export PDF on tablet')
    path = Path(folder) / 'page.rm'
    path.write_bytes(data)
    tree = lib.buildTree(str(path).encode('utf-8'))
    if not tree:
        raise ValueError('librm_lines could not parse this v6 scene')
    rid = None
    try:
        rid = lib.makeRenderer(tree, int(document), landscape)
        if not rid:
            raise ValueError('librm_lines could not create renderer')
        lib.setTemplate(rid, b'Blank')
        layers = json.loads(lib.getLayers(rid))
        for layer_id in ['7:1'] + [layer['groupId'] for layer in layers]:
            bounds = json.loads(lib.getSizeTracker(rid, layer_id.encode()))
            if bounds['l'] < 0 or bounds['t'] < 0 or bounds['r'] > width or bounds['b'] > height:
                raise ArchiveError('UNSUPPORTED_LAYOUT', 'Expanded/infinite page would be cropped; export full page PDF on the tablet')
        # Native size trackers miss text glyph extents. A guard band detects
        # ink/text crossing the page edge instead of returning a cropped success.
        guard = 128
        bw, bh = width + 2 * guard, height + 2 * guard
        _check_pixels(bw, bh)
        buffer = (ctypes.c_uint32 * (bw * bh))()
        lib.getFrame(rid, buffer, len(buffer) * 4, -guard, -guard, bw, bh, bw, bh, True)
        image = Image.frombytes('RGBA', (bw, bh), bytes(buffer))
        bands = [(0, 0, bw, guard), (0, bh - guard, bw, bh),
                 (0, guard, guard, bh - guard), (bw - guard, guard, bw, bh - guard)]
        if any(image.crop(box).getchannel('A').getbbox() for box in bands):
            raise ArchiveError('UNSUPPORTED_LAYOUT', 'Text/ink extends beyond page edges; refusing to crop. Export full page PDF on tablet')
        if template != 'Blank':
            lib.setTemplate(rid, template.encode())
            buffer = (ctypes.c_uint32 * (width * height))()
            lib.getFrame(rid, buffer, len(buffer) * 4, 0, 0, width, height, width, height, True)
            return Image.frombytes('RGBA', (width, height), bytes(buffer))
        return image.crop((guard, guard, bw - guard, bh - guard))
    finally:
        if rid:
            lib.destroyRenderer(rid)
        lib.destroyTree(tree)


def _check_pixels(width, height):
    if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 16_000_000):
        raise ArchiveError('RESOURCE_LIMIT', 'Page exceeds 8192 pixels per side or 16 megapixels; export a smaller PDF')


def _layout(z, stem, content, ordered, selected):
    if content.get('orientation', 'portrait') not in ('portrait', 'landscape'):
        raise ArchiveError('UNSUPPORTED_LAYOUT', 'Unknown page orientation')
    identity = {f'm{i}{j}': int(i == j) for i in range(1, 4) for j in range(1, 4)}
    if content.get('transform', identity) != identity:
        raise ArchiveError('UNSUPPORTED_LAYOUT', 'Custom transforms are not verified; export PDF on tablet')
    if content.get('zoomMode', 'bestFit') not in ('bestFit', None) or content.get('customZoomScale', 1) != 1:
        raise ArchiveError('UNSUPPORTED_LAYOUT', 'Custom zoom/crop is not verified; export PDF on tablet')
    pagedata = z.read(f'{stem}.pagedata').decode('utf-8').splitlines() if f'{stem}.pagedata' in z.namelist() else []
    for i, page in enumerate(ordered):
        if i + 1 not in selected:
            continue
        template = _value(page.get('template', pagedata[i] if i < len(pagedata) else 'Blank'))
        if template in ('P Lines small', 'P Lines medium', 'P Lines large'):
            raise ArchiveError('UNSUPPORTED_TEMPLATE', 'LIB rMLines 1.4.7 does not implement standard ruled templates; export PDF on tablet with its template background. Refusing a blank or guessed replacement')
        if template not in ('Blank', 'P Grid small', 'P Grid medium', 'P Grid large', 'P Grid margin med', 'P Grid margin large'):
            raise ArchiveError('UNSUPPORTED_TEMPLATE', f'Template {template!r} is not verified; export PDF on tablet (it will not be silently replaced by blank)')
        page['_template'] = template


def render_archive(source, out_path, format='pdf', overwrite=False, pages=None) -> dict:
    """Render with librm_lines. `pages` is unique 1-based numbers; PNG needs one.

    Outputs are staged then atomically published, never written over the source.
    Run in an isolated subprocess with a timeout when handling untrusted scenes.
    """
    try:
        if format not in ('pdf', 'png') or type(overwrite) is not bool:
            raise ArchiveError('BAD_ARGS', 'format must be pdf or png; overwrite must be boolean')
        target = Path(out_path).absolute()
        source = Path(source).resolve()
        if target.is_symlink() or target.resolve() == source or (target.exists() and target.samefile(source)):
            raise ArchiveError('BAD_ARGS', 'Output must not be the source archive or a symlink')
        if target.exists() and not overwrite:
            raise ArchiveError('OUTPUT_EXISTS', 'Output exists; explicitly set overwrite=True to replace it')
        import pymupdf
        z, stem, content, ordered = _document(source)
        with z:
            selected = pages if pages is not None else list(range(1, len(ordered) + 1))
            if (not isinstance(selected, (list, tuple)) or not selected
                    or any(type(i) is not int or not 1 <= i <= len(ordered) for i in selected)
                    or len(set(selected)) != len(selected) or (format == 'png' and len(selected) != 1)):
                raise ArchiveError('BAD_ARGS', 'pages must be unique 1-based page numbers; PNG requires exactly one selected page')
            _layout(z, stem, content, ordered, selected)
            # Private staging beside the output makes publication same-filesystem and atomic.
            with tempfile.TemporaryDirectory(prefix='.rm-render-', dir=target.parent) as folder:
                staged = Path(folder) / ('output.' + format)
                with pymupdf.open() as pdf, _open_background(z, stem, content) as base:
                    for i in selected:
                        item = ordered[i - 1]
                        data = _scene_data(z, stem, item)
                        image = None
                        if data is not None:
                            if _legacy_version(data):
                                if any(f"{stem}/{item['id']}{suffix}" in z.namelist()
                                       for suffix in ('-metadata.json', '.metadata')):
                                    raise ArchiveError('UNSUPPORTED_LAYOUT', 'Legacy external layer metadata is not qualified; export PDF on tablet')
                                if content.get('orientation', 'portrait') != 'portrait' or item['_redir'] >= 0:
                                    raise ArchiveError('UNSUPPORTED_LAYOUT', 'Legacy conversion is qualified only for portrait notebooks, not PDF overlays')
                                data = _legacy_to_v6(data)
                            parsed = _parse_scene(data)
                            image = _native_image(data, folder, content.get('orientation') == 'landscape',
                                                  parsed, item['_template'], item['_redir'] >= 0)
                        if item['_redir'] >= 0:
                            bp = base[item['_redir']]
                            if image and (abs(bp.rect.width * 226 / 72 - image.width) > 1
                                          or abs(bp.rect.height * 226 / 72 - image.height) > 1
                                          or bp.rotation or bp.cropbox != bp.mediabox):
                                raise ArchiveError('UNSUPPORTED_LAYOUT', 'Annotated PDF dimensions/rotation/crop are not verified for this scene; export annotated PDF on tablet')
                            pdf.insert_pdf(base, from_page=item['_redir'], to_page=item['_redir'])
                            page = pdf[-1]
                        else:
                            page = pdf.new_page(width=image.width * 72 / 226, height=image.height * 72 / 226)
                        if image:
                            buffer = io.BytesIO()
                            image.save(buffer, 'PNG')
                            page.insert_image(page.rect, stream=buffer.getvalue())
                        if format == 'png':
                            _check_pixels(math.ceil(page.rect.width * 226 / 72), math.ceil(page.rect.height * 226 / 72))
                            page.get_pixmap(dpi=226, alpha=False).save(staged)
                    if format == 'pdf':
                        pdf.save(staged, deflate=True)
                if overwrite:
                    os.replace(staged, target)
                else:
                    # Hard-link creation fails atomically if another writer won the race.
                    try:
                        os.link(staged, target)
                    except FileExistsError:
                        raise ArchiveError('OUTPUT_EXISTS', 'Output appeared during rendering; not overwritten')
        return {'ok': True, 'renderer': 'librm_lines', 'format': format,
                'path': str(target.resolve()), 'page_count': len(selected),
                'pages': list(selected), 'warnings': ['Notebook ink/text is rasterized at 226 dpi; PDF backgrounds remain vector. Use extract_archive_text for native typed text.',
                                                      'Supported legacy v3/v5 fineliner pages are decoded to v6 and rendered by LIB rMLines; other legacy pens/layouts fail closed.']}
    except Exception as exc:
        return _error(exc)


def _open_background(z, stem, content):
    import pymupdf
    if content.get('fileType') not in ('pdf', 'epub'):
        return pymupdf.open()
    name = f'{stem}.pdf'
    if name not in z.namelist():
        raise ArchiveError('MISSING_BACKGROUND', 'Archive lacks rendered PDF background; re-download .rmdoc or export PDF from tablet (EPUB conversion is not enabled)')
    pdf = pymupdf.open(stream=z.read(name), filetype='pdf')
    if pdf.needs_pass or not 0 < len(pdf) <= MAX_PAGES:
        pdf.close()
        raise ArchiveError('UNSUPPORTED_DOCUMENT', 'Encrypted, empty, or oversized PDF background')
    return pdf


def _scene_data(z, stem, page):
    name = f"{stem}/{page['id']}.rm"
    return z.read(name) if name in z.namelist() else None


def _document(source):
    source = Path(source)
    if source.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ArchiveError('UNSAFE_ARCHIVE', 'Archive exceeds 256 MiB compressed limit')
    z = zipfile.ZipFile(source)
    try:
        entries = z.infolist()
        if len(entries) > MAX_ENTRIES:
            raise ArchiveError('UNSAFE_ARCHIVE', 'Too many ZIP entries')
        names, total = set(), 0
        for entry in entries:
            name = entry.filename
            parts = name.rstrip('/').split('/')
            mode = entry.external_attr >> 16
            if (name != entry.orig_filename or name.startswith('/') or '\\' in name or ':' in name
                    or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts)
                    or any(ord(c) < 32 for c in name) or name.casefold() in names
                    or stat.S_ISLNK(mode) or entry.flag_bits & 1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ArchiveError('UNSAFE_ARCHIVE', 'Unsafe, duplicate, encrypted, or unsupported ZIP entry')
            names.add(name.casefold())
            total += entry.file_size
            if (entry.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES
                    or entry.file_size > MAX_COMPRESSION_RATIO * max(entry.compress_size, 1)):
                raise ArchiveError('UNSAFE_ARCHIVE', 'ZIP expansion exceeds member, total, or compression-ratio limit')
        contents = [n for n in z.namelist() if n.endswith('.content')]
        if len(contents) != 1:
            raise ArchiveError('BAD_ARCHIVE', 'Archive must contain exactly one .content document')
        content_name = contents[0]
        content = json.loads(z.read(content_name))
        stem = content_name[:-8]
        kind = content.get('fileType', 'notebook')
        if kind not in ('notebook', 'pdf', 'epub'):
            raise ArchiveError('UNSUPPORTED_DOCUMENT', 'Unsupported fileType; export PDF on the tablet')
        base_count = 0
        if kind in ('pdf', 'epub'):
            with _open_background(z, stem, content) as base:
                base_count = len(base)
        elif f'{stem}.pdf' in z.namelist():
            raise ArchiveError('UNSUPPORTED_DOCUMENT', 'Notebook with unexpected PDF background; export PDF on the tablet')
        pages = content.get('cPages', {}).get('pages')
        if pages is not None:
            pages = [p for p in pages if not _value(p.get('deleted', False))]
            keys = [_value(p.get('idx')) for p in pages]
            if any(not isinstance(k, str) or not k for k in keys) or len(set(keys)) != len(keys):
                raise ArchiveError('BAD_ARCHIVE', 'Missing or ambiguous cPages idx ordering')
            pages = sorted(pages, key=lambda p: _value(p['idx']))
        else:
            legacy = content.get('pages', [])
            redirects = content.get('redirectionPageMap')
            if redirects is not None and len(redirects) != len(legacy):
                raise ArchiveError('BAD_ARCHIVE', 'redirectionPageMap length does not match pages')
            pages = [{'id': p, 'redir': redirects[i] if redirects is not None else (i if base_count else -1)}
                     for i, p in enumerate(legacy)]
            if not pages and base_count:
                if any(n.endswith('.rm') for n in z.namelist()):
                    raise ArchiveError('BAD_ARCHIVE', 'Unordered annotations present; cannot infer PDF page order')
                pages = [{'id': f'pdf-{i + 1}', 'redir': i} for i in range(base_count)]
        if not pages or len(pages) > MAX_PAGES:
            raise ArchiveError('BAD_ARCHIVE', 'Expected 1..500 explicitly ordered pages')
        ids = [p.get('id') for p in pages]
        if any(not isinstance(p, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', p) for p in ids) or len(set(ids)) != len(ids):
            raise ArchiveError('BAD_ARCHIVE', 'Invalid or duplicate page identifiers')
        stem = content_name[:-8]
        for page in pages:
            redir = _value(page.get('redir', -1))
            if type(redir) is not int or redir < -1 or (redir >= 0 and redir >= base_count):
                raise ArchiveError('BAD_ARCHIVE', 'PDF page redirect is invalid or background is missing')
            page['_redir'] = redir
            if f"{stem}/{page['id']}.rm" not in z.namelist() and redir == -1:
                raise ArchiveError('MISSING_PAGE', f"Page {page['id']} has no .rm scene; re-download the complete document")
        return z, stem, content, pages
    except Exception:
        z.close()
        raise


def _groups(tree):
    from rmscene.scene_items import Group
    stack, seen = [(tree.root, 0)], set()
    while stack:
        group, depth = stack.pop()
        if group.node_id in seen or depth > 64 or len(seen) > 4096:
            raise ArchiveError('UNSUPPORTED_SCENE', 'Cyclic/shared or oversized scene group graph')
        seen.add(group.node_id)
        yield group
        stack.extend((child, depth + 1) for child in group.children.values() if isinstance(child, Group))


def _validate_native_scene_geometry(block):
    """Accept only redundant geometry in native 1.4.7 SceneInfo fields 6..8.

    Native blocks.cpp decodes these fields, but renderer.cpp uses integer
    paperSize. A zero extendedContentRect and identical floating sizes add no
    geometry. Nonempty rectangles, differing sizes, preferredLayout (field 9)
    and all unknown nested/trailing data remain unsupported, not discarded.
    Original bytes are always passed to LIB rMLines unchanged.
    """
    from rmscene.tagged_block_reader import TaggedBlockReader, BlockOverflowError
    from rmscene.tagged_block_common import UnexpectedBlockError
    stream = io.BytesIO(block.extra_data)
    reader = TaggedBlockReader(stream)
    try:
        with reader.read_subblock(6) as outer:
            reader.read_id(1)
            with reader.read_subblock(2) as inner:
                rect = struct.unpack('<dddd', reader.data.read_bytes(32))
        if outer.extra_data or inner.extra_data or rect != (0, 0, 0, 0):
            raise ValueError('Nonempty or unknown extended content rectangle')
        if stream.tell() < len(block.extra_data):
            with reader.read_subblock(7) as pair:
                size = struct.unpack('<dd', reader.data.read_bytes(16))
            if pair.extra_data or size != block.paper_size:
                raise ValueError('Floating paper size differs from native integer paper size')
        if stream.tell() < len(block.extra_data):
            with reader.read_subblock(8) as outer:
                reader.read_id(1)
                with reader.read_subblock(2) as inner:
                    size = struct.unpack('<dd', reader.data.read_bytes(16))
            if outer.extra_data or inner.extra_data or size != block.paper_size:
                raise ValueError('LWW paper size differs from native integer paper size')
        if stream.tell() != len(block.extra_data):
            raise ValueError('Unqualified preferred layout or unknown scene geometry')
    except (ValueError, EOFError, AssertionError, UnexpectedBlockError, BlockOverflowError) as exc:
        raise ArchiveError('UNSUPPORTED_SCENE', 'Extended scene geometry/layout is not qualified; export PDF from the tablet') from exc


def _read_native_text_width(block):
    """Validate native 1.4.7 RootTextBlock field 5/1, absent in rmscene 0.8.

    Source: librm_lines tag 1.4.7, common/blocks.cpp RootTextBlock::read.
    Preserve the original scene bytes for native rendering; only update the
    Python validation model to the same effective width used by the native ABI.
    """
    from rmscene.tagged_block_reader import TaggedBlockReader, BlockOverflowError
    from rmscene.tagged_block_common import UnexpectedBlockError
    stream = io.BytesIO(block.extra_data)
    reader = TaggedBlockReader(stream)
    try:
        with reader.read_subblock(5) as outer:
            with reader.read_subblock(1) as inner:
                reader.read_id(1)  # LWW timestamp, not a drawing coordinate
                width = reader.read_float(2)
        if (outer.extra_data or inner.extra_data or stream.tell() != len(block.extra_data)
                or not math.isfinite(width) or not 0 < width <= 1_000_000):
            raise ValueError('Invalid or extended modern text width')
    except (ValueError, EOFError, AssertionError, UnexpectedBlockError, BlockOverflowError) as exc:
        raise ArchiveError('UNSUPPORTED_SCENE', 'Unknown or invalid native text-width fields; export PDF from the tablet') from exc
    block.value.width = width


def _parse_scene(data):
    import rmscene
    if not data.startswith(rmscene.HEADER_V6):
        raise ArchiveError('UNSUPPORTED_SCENE', 'Only v6 .rm scenes are supported; export older notebooks as PDF on the tablet')
    offset, blocks_count = len(rmscene.HEADER_V6), 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ArchiveError('UNSUPPORTED_SCENE', 'Truncated v6 block header')
        size = struct.unpack_from('<I', data, offset)[0]
        offset += 8 + size
        blocks_count += 1
        if offset > len(data) or blocks_count > 100000:
            raise ArchiveError('UNSUPPORTED_SCENE', 'Truncated or oversized v6 scene')
    blocks = list(rmscene.read_blocks(io.BytesIO(data)))
    for block in blocks:
        if isinstance(block, (rmscene.UnreadableBlock, rmscene.SceneTextItemBlock)):
            raise ArchiveError('UNSUPPORTED_SCENE', 'Unknown v6 blocks/fields (including newer images) cannot be preserved; export PDF from the tablet')
        if block.extra_data:
            if isinstance(block, rmscene.RootTextBlock):
                _read_native_text_width(block)
            elif isinstance(block, rmscene.SceneInfo):
                _validate_native_scene_geometry(block)
            else:
                raise ArchiveError('UNSUPPORTED_SCENE', 'Unknown v6 blocks/fields (including newer images) cannot be preserved; export PDF from the tablet')
    tree = rmscene.SceneTree()
    rmscene.build_tree(tree, blocks)
    list(_groups(tree))  # detect cycles before walk/native recursion
    point_count = 0
    for item in tree.walk():
        if isinstance(item, rmscene.scene_items.Line):
            point_count += len(item.points)
            numbers = [item.thickness_scale, item.starting_length]
            numbers.extend(v for p in item.points for v in (p.x, p.y, p.width, p.pressure, p.speed, p.direction))
            if not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in numbers):
                raise ArchiveError('UNSUPPORTED_SCENE', 'Nonfinite or extreme stroke coordinates')
    if point_count > 500000:
        raise ArchiveError('RESOURCE_LIMIT', 'Scene exceeds 500000 stroke points')
    if tree.root_text:
        text = tree.root_text
        if not all(math.isfinite(v) and abs(v) <= 1_000_000 for v in (text.pos_x, text.pos_y, text.width)):
            raise ArchiveError('UNSUPPORTED_SCENE', 'Nonfinite or extreme text coordinates')
        if sum(len(item.value) for item in text.items.sequence_items() if isinstance(item.value, str)) > 100000:
            raise ArchiveError('RESOURCE_LIMIT', 'Scene exceeds 100000 typed characters')
    return tree


def _legacy_version(data):
    for version in (3, 5):
        if data.startswith(f'reMarkable .lines file, version={version}'.encode().ljust(43, b' ')):
            return version
    return None


def _legacy_layers(data):
    """Bounded v3/v5 structural decoder; these formats have no typed text.

    Layout: 43-byte header, uint32 layer/stroke counts, IIIf[I]I strokes,
    and six float32 values per point. Never hand legacy bytes to the native
    v5 reader: LIB rMLines 1.4.7 readBlockInfo() is a stub.
    """
    version = _legacy_version(data)
    if version is None:
        raise ArchiveError('UNSUPPORTED_SCENE', 'Expected a v3 or v5 lines header')
    offset = 43

    def read(fmt):
        nonlocal offset
        size = struct.calcsize(fmt)
        if offset + size > len(data):
            raise ArchiveError('UNSUPPORTED_SCENE', 'Truncated legacy scene')
        values = struct.unpack_from(fmt, data, offset)
        offset += size
        return values

    count, = read('<I')
    if count > 4096:
        raise ArchiveError('RESOURCE_LIMIT', 'Legacy scene exceeds 4096 layers')
    layers, strokes, points = [], 0, 0
    for _ in range(count):
        count_strokes, = read('<I')
        strokes += count_strokes
        if strokes > 100000:
            raise ArchiveError('RESOURCE_LIMIT', 'Legacy scene exceeds 100000 strokes')
        layer = []
        for _ in range(count_strokes):
            pen, color, reserved, width = read('<IIIf')
            extra = read('<I')[0] if version == 5 else 0
            n, = read('<I')
            points += n
            if points > 500000:
                raise ArchiveError('RESOURCE_LIMIT', 'Scene exceeds 500000 stroke points')
            if not math.isfinite(width) or not 0 <= width <= 10000:
                raise ArchiveError('UNSUPPORTED_SCENE', 'Invalid legacy stroke width')
            if offset + n * 24 > len(data):
                raise ArchiveError('UNSUPPORTED_SCENE', 'Truncated legacy stroke points')
            values = [read('<ffffff') for _ in range(n)]
            if any(not math.isfinite(v) or abs(v) > 1_000_000 for p in values for v in p):
                raise ArchiveError('UNSUPPORTED_SCENE', 'Nonfinite or extreme legacy stroke coordinates')
            layer.append((pen, color, reserved, width, extra, values))
        layers.append(layer)
    if offset != len(data):
        raise ArchiveError('UNSUPPORTED_SCENE', 'Unknown trailing legacy scene data')
    return layers


def _legacy_to_v6(data):
    """Convert only qualified, single-layer v3/v5 fineliner ink.

    Legacy points and v6 version-1 line points have the same six-float
    layout. Translate top-left x to v6 center-origin x (x - 702); retain
    float fields via rmscene's version-1 writer, not its quantized v2 writer.
    Rendering still uses LIB rMLines, never an alternate ink renderer.
    """
    import rmscene
    layers = _legacy_layers(data)
    if len(layers) != 1:
        raise ArchiveError('UNSUPPORTED_SCENE', 'Legacy rendering is qualified only for one fineliner layer; export PDF on tablet')
    blocks = list(rmscene.simple_text_document(''))
    previous = rmscene.CrdtId(0, 0)
    for i, (pen, color, reserved, width, extra, values) in enumerate(layers[0]):
        if pen not in (4, 17) or color not in (0, 1, 2) or reserved or extra:
            raise ArchiveError('UNSUPPORTED_SCENE', 'Legacy pen/color/fields are not qualified (only fineliner with standard colors and zero reserved fields); export PDF on tablet')
        if len(values) < 2 or any(not (0 <= speed <= 16383 and 0 <= direction <= math.tau
                                     and 0 < w <= 16383 and 0 <= pressure <= 1)
                                  for x, y, speed, direction, w, pressure in values):
            raise ArchiveError('UNSUPPORTED_SCENE', 'Legacy point attributes are outside the qualified native range')
        points = [rmscene.scene_items.Point(x - 702, y, speed * 4, direction * 255 / math.tau,
                                          w * 4, pressure * 255)
                  for x, y, speed, direction, w, pressure in values]
        line = rmscene.scene_items.Line(rmscene.scene_items.PenColor(color),
                                       rmscene.scene_items.Pen(pen), points, width, 0.0)
        current = rmscene.CrdtId(1, 100 + i)
        blocks.append(rmscene.SceneLineItemBlock(parent_id=rmscene.CrdtId(0, 11), item=rmscene.CrdtSequenceItem(
            item_id=current, left_id=previous, right_id=rmscene.CrdtId(0, 0), deleted_length=0, value=line)))
        previous = current
    output = io.BytesIO()
    rmscene.write_blocks(output, blocks, options={'version': '3.0'})
    return output.getvalue()


def _text(data):
    if _legacy_version(data):
        _legacy_layers(data)  # validate the entire file, not merely its header
        return ''
    from rmscene.text import TextDocument
    tree = _parse_scene(data)
    if tree.root_text is None:
        return ''
    return '\n'.join(str(p) for p in TextDocument.from_scene_item(tree.root_text).contents)


def extract_archive_text(source) -> dict:
    """Extract only native typed text, in document page order; never run OCR."""
    try:
        z, stem, content, pages = _document(source)
        with z:
            result = []
            for i, p in enumerate(pages):
                data = _scene_data(z, stem, p)
                result.append({'id': p['id'], 'page': i + 1, 'text': _text(data) if data is not None else ''})
        return {'ok': True, 'complete': True, 'ocr': False, 'kind': 'typed_text', 'page_count': len(result), 'pages': result,
                'text': '\n\n'.join(p['text'] for p in result),
                'warnings': ['Native typed text only; handwriting and source PDF text are not transcribed. No OCR was run.',
                             'Legacy v3/v5 pages contain no typed text and return empty strings; this does not certify their ink can be rendered.']}
    except Exception as exc:
        return _error(exc)


def _main():
    """One JSON request on stdin, one JSON result on stdout; exit 0/2.

    {"op":"render", "source":"file.rmdoc", "out_path":"file.pdf", ...}
    {"op":"extract_text", "source":"file.rmdoc"}
    Native diagnostics are redirected to stderr, not mixed with the protocol.
    """
    import sys
    saved_stdout = os.dup(1)
    try:
        sys.stdout.flush()
        os.dup2(2, 1)
        try:
            raw = sys.stdin.buffer.read(65537)
            if len(raw) > 65536:
                raise ArchiveError('BAD_ARGS', 'Request exceeds 64 KiB')
            request = json.loads(raw)
            op = request.pop('op')
            if op == 'render':
                result = render_archive(**request)
            elif op == 'extract_text':
                result = extract_archive_text(**request)
            else:
                raise ArchiveError('BAD_ARGS', 'op must be render or extract_text')
        except Exception as exc:
            result = _error(exc)
        sys.stdout.flush()
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get('ok') else 2


if __name__ == '__main__':
    raise SystemExit(_main())

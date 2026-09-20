"""Regenerate public synthetic renderer evidence using the external test venv.

No downloads, credentials, or user notebooks. All v6 data is written by rmscene.
"""
from pathlib import Path
import importlib.util
import io
import json
import shutil
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('renderer_tests', ROOT / 'tests/test_renderer.py')
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
mod = t.renderer()


def main():
    scratch = Path.home() / 'AppData/Local/hermes/cache/scratch'
    scratch.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='rm-render-fixture-', dir=scratch) as folder:
        folder = Path(folder)
        ink_blocks = list(t.rmscene.read_blocks(io.BytesIO(t.stroke_scene())))
        text_blocks = list(t.rmscene.simple_text_document('Second typed page\nNative ink and grid below.'))
        text_blocks.extend(b for b in ink_blocks if isinstance(b, t.rmscene.SceneLineItemBlock))
        buffer = io.BytesIO()
        t.rmscene.write_blocks(buffer, text_blocks)
        content = {'fileType': 'notebook', 'formatVersion': 2, 'orientation': 'portrait',
                   'cPages': {'pages': [
                       {'id': 'second', 'idx': {'value': 'bb'}, 'template': {'value': 'P Grid medium'}},
                       {'id': 'first', 'idx': {'value': 'ba'}, 'template': {'value': 'Blank'}},
                   ]}}
        source = t.archive(folder, content=content, members={
            'doc/first.rm': t.scene('First typed page\nNative text, no OCR.'),
            'doc/second.rm': buffer.getvalue(),
        })
        fixture = OUT / 'render-notebook.rmdoc'
        shutil.copyfile(source, fixture)
        results.append(mod.render_archive(fixture, OUT / 'render-notebook.pdf', overwrite=True))
        results.append(mod.render_archive(fixture, OUT / 'render-page.png', format='png', pages=[2], overwrite=True))
        results.append(mod.extract_archive_text(fixture))
        content = {'fileType': 'pdf', 'cPages': {'pages': [
            {'id': 'first', 'idx': {'value': 'a'}, 'redir': {'value': 1}},
            {'id': 'second', 'idx': {'value': 'b'}, 'redir': {'value': 0}},
        ]}}
        source = t.archive(folder, content=content, members={'doc.pdf': t.pdf_background(), 'doc/first.rm': t.stroke_scene()})
        fixture = OUT / 'render-annotated.rmdoc'
        shutil.copyfile(source, fixture)
        results.append(mod.render_archive(fixture, OUT / 'render-annotated.pdf', overwrite=True))
        results.append(mod.render_archive(fixture, OUT / 'render-annotated.png', format='png', pages=[1], overwrite=True))
    assert all(r['ok'] for r in results), results
    import pymupdf
    with pymupdf.open(OUT / 'render-notebook.pdf') as pdf:
        assert len(pdf) == 2
        pdf[0].get_pixmap(dpi=120).save(OUT / 'render-notebook-preview.png')
    with pymupdf.open(OUT / 'render-annotated.pdf') as pdf:
        assert 'BASE SECOND' in pdf[0].get_text()
        assert 'BASE FIRST' in pdf[1].get_text()
    (OUT / 'render-results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()

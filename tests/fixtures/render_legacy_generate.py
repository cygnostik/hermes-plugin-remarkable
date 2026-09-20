"""Generate synthetic-only legacy native renderer evidence; no private inputs."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('render_tests', HERE.parent / 'test_renderer.py')
tests = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tests)
mod = tests.renderer()
with tempfile.TemporaryDirectory(prefix='rm-legacy-fixture-', dir=os.environ['TMPDIR']) as folder:
    source = tests.archive(Path(folder), content={'fileType': 'notebook', 'pages': ['first']},
                           members={'doc/first.rm': tests.legacy_scene(5, 17)})
    fixture = HERE / 'render-legacy-v5.rmdoc'
    fixture.write_bytes(source.read_bytes())
    results = {}
    for fmt in ('pdf', 'png'):
        results[fmt] = mod.render_archive(fixture, HERE / ('render-legacy-v5.' + fmt), format=fmt, overwrite=True)
        assert results[fmt]['ok'], results[fmt]
    results['typed_text'] = mod.extract_archive_text(fixture)
    assert results['typed_text']['ok'] and results['typed_text']['text'] == ''
    import pymupdf
    from PIL import Image
    with pymupdf.open(HERE / 'render-legacy-v5.pdf') as pdf:
        assert len(pdf) == 1
        pix = pdf[0].get_pixmap(dpi=120)
        pix.save(HERE / 'render-legacy-v5-preview.png')
    with Image.open(HERE / 'render-legacy-v5.png') as image:
        assert image.size == (1404, 1872)
        assert image.convert('L').getpixel((500, 900)) < 50
    (HERE / 'render-legacy-results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
print('Generated synthetic v5 archive, native PDF/PNG, preview and typed-text evidence; PDF reopened and ink checked.')
